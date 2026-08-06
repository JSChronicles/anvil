from __future__ import annotations

import importlib
import importlib.util
import json
import re
import sys
from collections import defaultdict, deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from functools import lru_cache
from importlib.metadata import entry_points
from types import MappingProxyType

from anvil._components import (
    ComponentCatalog,
    ComponentDescriptor as CatalogDescriptor,
    ComponentKind,
    ComponentOrigin,
    ComponentResolver,
    ComponentSource,
    DiscoveryIssue,
    PackageComponentSource,
    source_from_entry_point,
)

UNIVERSAL_TASK_PACKAGE = "anvil.providers.tasks"
PROVIDER_TASK_PACKAGE_PREFIX = "anvil.providers"
UNIVERSAL_TASK_ENTRY_POINT_GROUP = "anvil.providers.tasks"
PROVIDER_TASK_ENTRY_POINT_GROUP_PREFIX = "anvil.providers"

# ============================================================================
# Models
# ============================================================================


class TaskScope(StrEnum):
    """Frequency at which a task is invoked for one execution target."""

    REGION = "region"
    TARGET = "target"
    CONFIGURED_TARGET = "configured_target"


@dataclass(frozen=True, slots=True)
class ResolvedTask:
    name: str
    run: Callable
    depends_on: list[str]
    scope: TaskScope = TaskScope.REGION
    id: str = ""
    always_run: bool = False
    metadata: dict[str, object] = field(default_factory=dict)
    dependency_data: dict[str, dict[str, str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Default an omitted invocation ID to the component name."""

        if not self.id:
            object.__setattr__(self, "id", self.name)


@dataclass(frozen=True, slots=True)
class TaskSpec:
    """Immutable normalized task declaration."""

    id: str
    name: str
    depends_on: tuple[str, ...]
    always_run: bool
    metadata_json: str
    dependency_data: tuple[tuple[str, str, str | None], ...]


TaskSpecKey = tuple[TaskSpec, ...]
CachedOrderedTask = tuple[
    tuple[
        str,
        str,
        Callable,
        tuple[str, ...],
        bool,
        str,
        tuple[tuple[str, str, str | None], ...],
        TaskScope,
    ],
    ...,
]
CachedAdjacency = tuple[tuple[str, tuple[str, ...]], ...]


class TaskConfigError(RuntimeError):
    pass


TaskDescriptor = CatalogDescriptor[Callable]


@dataclass(frozen=True, slots=True)
class TaskDiscoveryResult:
    """Discovered tasks and non-fatal discovery issues."""

    tasks: list[TaskDescriptor]
    issues: list[DiscoveryIssue]
    provider_catalogs: Mapping[str, ComponentCatalog[Callable]]


@dataclass(frozen=True, slots=True)
class ResolvedExecution:
    ordered: list[ResolvedTask]
    adjacency: dict[str, list[str]]


# ============================================================================
# Internal helpers
# ============================================================================


def _load_package_task(*, task_name: str, package_name: str) -> Callable:
    module_name = f"{package_name}.{task_name}"
    try:
        module = importlib.import_module(module_name)
    except Exception as error:
        raise TaskConfigError(
            f"Task '{task_name}' in '{package_name}' failed during import: {error}"
        ) from error
    run = getattr(module, "run", None)
    if not callable(run):
        raise TaskConfigError(
            f"Task '{task_name}' in '{package_name}' must define callable run(...)"
        )
    return run


def _load_task_component(
    package_name: str, task_name: str, source: ComponentSource
) -> Callable:
    return _load_package_task(task_name=task_name, package_name=package_name)


@lru_cache(maxsize=512)
def _load_provider_task_callable(*, provider_name: str, task_name: str) -> Callable:
    catalog = _provider_task_catalog(provider_name)
    descriptors = catalog.inventory.get(task_name, ())
    if not descriptors:
        issue_detail = ""
        if catalog.issues:
            issue_lines = "; ".join(
                f"{issue.name} ({issue.source}): {issue.error}"
                for issue in catalog.issues
            )
            issue_detail = (
                f" Discovery of one or more task sources failed: {issue_lines}"
            )
        raise TaskConfigError(
            f"Task '{task_name}' is not available for provider '{provider_name}'. "
            "Tasks must be provided by universal package 'anvil.providers.tasks' "
            f"or provider package 'anvil.providers.{provider_name}.tasks'."
            f"{issue_detail}"
        )

    return ComponentResolver(
        kind=ComponentKind.TASK,
        catalog=catalog,
        error_type=TaskConfigError,
        context=f"for provider '{provider_name}'",
    ).load(task_name)


def _iter_package_task_descriptors(
    *, package_name: str, source_label: str, provider_name: str | None
) -> list[TaskDescriptor]:
    try:
        package_spec = importlib.util.find_spec(package_name)
    except ModuleNotFoundError as error:
        missing_package = error.name
        if missing_package is not None and (
            package_name == missing_package
            or package_name.startswith(f"{missing_package}.")
        ):
            return []
        raise TaskConfigError(
            f"Unable to inspect task package '{package_name}': {error}"
        ) from error

    if package_spec is None:
        return []

    component_source = ComponentSource(
        origin=ComponentOrigin.STOCK,
        package=package_name,
        label=source_label,
        provider=provider_name,
    )
    descriptors, issues = PackageComponentSource(
        package_name=package_name,
        source=component_source,
        component_loader=_load_task_component,
    ).discover()
    if issues:
        issue = issues[0]
        raise TaskConfigError(f"{issue.name} ({issue.source}): {issue.error}")
    return list(descriptors)


def _iter_plugin_task_descriptors(
    *, entry_point_group: str, label_prefix: str, provider_name: str | None
) -> tuple[list[TaskDescriptor], list[DiscoveryIssue]]:
    descriptors: list[TaskDescriptor] = []
    issues: list[DiscoveryIssue] = []

    for entry_point in entry_points(group=entry_point_group):
        package_name = entry_point.value.split(":", maxsplit=1)[0]
        component_source = source_from_entry_point(
            entry_point=entry_point,
            package=package_name,
            label_prefix=label_prefix,
            provider=provider_name,
        )
        discovered, source_issues = PackageComponentSource(
            package_name=package_name,
            source=component_source,
            component_loader=_load_task_component,
        ).discover(issue_name=entry_point.name)
        descriptors.extend(discovered)
        issues.extend(source_issues)

    return descriptors, issues


@lru_cache(maxsize=1)
def _universal_task_catalog() -> ComponentCatalog[Callable]:
    descriptors: list[CatalogDescriptor[Callable]] = []
    issues: list[DiscoveryIssue] = []

    descriptors.extend(
        _iter_package_task_descriptors(
            package_name=UNIVERSAL_TASK_PACKAGE,
            source_label="universal",
            provider_name=None,
        )
    )
    plugin_descriptors, plugin_issues = _iter_plugin_task_descriptors(
        entry_point_group=UNIVERSAL_TASK_ENTRY_POINT_GROUP,
        label_prefix="universal plugin:",
        provider_name=None,
    )
    descriptors.extend(plugin_descriptors)
    issues.extend(plugin_issues)
    return ComponentCatalog.build(descriptors, issues)


@lru_cache(maxsize=16)
def _provider_specific_task_catalog(provider_name: str) -> ComponentCatalog[Callable]:
    descriptors: list[CatalogDescriptor[Callable]] = []
    issues: list[DiscoveryIssue] = []

    descriptors.extend(
        _iter_package_task_descriptors(
            package_name=f"{PROVIDER_TASK_PACKAGE_PREFIX}.{provider_name}.tasks",
            source_label=provider_name,
            provider_name=provider_name,
        )
    )
    plugin_descriptors, plugin_issues = _iter_plugin_task_descriptors(
        entry_point_group=(
            f"{PROVIDER_TASK_ENTRY_POINT_GROUP_PREFIX}.{provider_name}.tasks"
        ),
        label_prefix=f"{provider_name} plugin:",
        provider_name=provider_name,
    )
    descriptors.extend(plugin_descriptors)
    issues.extend(plugin_issues)
    return ComponentCatalog.build(descriptors, issues)


@lru_cache(maxsize=16)
def _provider_task_catalog(provider_name: str) -> ComponentCatalog[Callable]:
    universal_catalog = _universal_task_catalog()
    provider_catalog = _provider_specific_task_catalog(provider_name)
    return ComponentCatalog.build(
        (*universal_catalog.descriptors, *provider_catalog.descriptors),
        (*universal_catalog.issues, *provider_catalog.issues),
    )


def provider_task_descriptor_index(
    *, provider_name: str
) -> dict[str, list[TaskDescriptor]]:
    """Return cached provider-aware task descriptors by task name."""

    return {
        name: list(descriptors)
        for name, descriptors in _provider_task_catalog(provider_name).inventory.items()
    }


def _clear_task_caches() -> None:
    _load_provider_task_callable.cache_clear()
    _resolve_tasks_cached.cache_clear()
    _provider_task_catalog.cache_clear()
    _provider_specific_task_catalog.cache_clear()
    _universal_task_catalog.cache_clear()


# ============================================================================
# Public API
# ============================================================================


TaskSpecInput = Mapping[str, object]
DEPENDENCY_PATH_PATTERN = re.compile(
    r"^(?:result(?:\.[A-Za-z_][A-Za-z0-9_]*)*|status|error|actions)$"
)


def _metadata_json(*, task_id: str, value: object) -> str:
    """Return a deterministic cache representation of task metadata."""

    if not isinstance(value, dict):
        raise TaskConfigError(f"Task '{task_id}' metadata must be a mapping")
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        raise TaskConfigError(
            f"Task '{task_id}' metadata must contain JSON-serializable values"
        ) from error


def _normalize_dependency_data(
    *, task_id: str, value: object
) -> tuple[tuple[str, str, str | None], ...]:
    """Validate dependency-data references and return a stable cache value."""

    if not isinstance(value, dict):
        raise TaskConfigError(f"Task '{task_id}' dependency_data must be a mapping")

    normalized: list[tuple[str, str, str | None]] = []
    for local_name, raw_reference in value.items():
        if not isinstance(local_name, str) or not local_name:
            raise TaskConfigError(
                f"Task '{task_id}' dependency_data names must be non-empty strings"
            )
        if not isinstance(raw_reference, dict):
            raise TaskConfigError(
                f"Task '{task_id}' dependency_data.{local_name} must be a mapping"
            )
        unknown_properties = set(raw_reference) - {"task_id", "path"}
        if unknown_properties:
            unknown_display = ", ".join(
                sorted(str(name) for name in unknown_properties)
            )
            raise TaskConfigError(
                f"Task '{task_id}' dependency_data.{local_name} has unknown "
                f"properties: {unknown_display}"
            )

        producer_id = raw_reference.get("task_id")
        if not isinstance(producer_id, str) or not producer_id:
            raise TaskConfigError(
                f"Task '{task_id}' dependency_data.{local_name}.task_id "
                "must be a non-empty string"
            )
        path = raw_reference.get("path")
        if path is not None and (
            not isinstance(path, str) or DEPENDENCY_PATH_PATTERN.fullmatch(path) is None
        ):
            raise TaskConfigError(
                f"Task '{task_id}' dependency_data.{local_name}.path must be "
                "result, a dotted result field, status, error, or actions"
            )
        normalized.append((local_name, producer_id, path))

    return tuple(sorted(normalized))


def _normalize_task_specs(task_specs: Sequence[TaskSpecInput]) -> TaskSpecKey:
    """Validate task declarations once while preserving declaration order."""

    normalized_specs: list[TaskSpec] = []
    seen_ids: set[str] = set()
    duplicate_ids: set[str] = set()
    component_occurrences: dict[str, list[bool]] = defaultdict(list)

    for spec in task_specs:
        name = spec.get("name")
        if not isinstance(name, str) or not name:
            raise TaskConfigError("task name must be a non-empty string")

        explicit_id = "id" in spec
        raw_id = spec.get("id", name)
        if not isinstance(raw_id, str) or not raw_id:
            raise TaskConfigError("task id must be a non-empty string")
        task_id = raw_id
        if task_id in seen_ids:
            duplicate_ids.add(task_id)
        seen_ids.add(task_id)
        component_occurrences[name].append(explicit_id)

        raw_depends_on = spec.get("depends_on", [])
        if not isinstance(raw_depends_on, list):
            raise TaskConfigError(
                f"Task '{task_id}' depends_on must be a list of strings"
            )
        depends_on: list[str] = []
        for dependency in raw_depends_on:
            if not isinstance(dependency, str) or not dependency:
                raise TaskConfigError(
                    f"Task '{task_id}' depends_on must be a list of non-empty strings"
                )
            depends_on.append(dependency)
        if len(set(depends_on)) != len(depends_on):
            raise TaskConfigError(
                f"Task '{task_id}' depends_on must not contain duplicates"
            )

        always_run = spec.get("always_run", False)
        if not isinstance(always_run, bool):
            raise TaskConfigError(f"Task '{task_id}' always_run must be a boolean")
        if always_run and not depends_on:
            raise TaskConfigError(
                f"Task '{task_id}' sets always_run but has no dependencies"
            )

        metadata_json = _metadata_json(task_id=task_id, value=spec.get("metadata", {}))
        dependency_data = _normalize_dependency_data(
            task_id=task_id, value=spec.get("dependency_data", {})
        )

        normalized_specs.append(
            TaskSpec(
                id=task_id,
                name=name,
                depends_on=tuple(depends_on),
                always_run=always_run,
                metadata_json=metadata_json,
                dependency_data=dependency_data,
            )
        )

    repeated_without_explicit_ids = [
        name
        for name, explicit_ids in component_occurrences.items()
        if len(explicit_ids) > 1 and not all(explicit_ids)
    ]
    if repeated_without_explicit_ids:
        names = ", ".join(sorted(repeated_without_explicit_ids))
        raise TaskConfigError(
            f"Duplicate task name detected: '{names}'. When a component name is "
            "configured more than once, every occurrence must have an explicit "
            "unique ID"
        )
    if duplicate_ids:
        duplicates = ", ".join(sorted(duplicate_ids))
        raise TaskConfigError(f"Duplicate task ID detected: '{duplicates}'")

    return tuple(normalized_specs)


def _task_scope(*, task_name: str, run: Callable) -> TaskScope:
    """Load and validate static scope metadata from a task module."""

    module = sys.modules.get(run.__module__)
    if module is None:
        module = importlib.import_module(run.__module__)
    raw_scope = getattr(module, "TASK_SCOPE", TaskScope.REGION)
    try:
        return TaskScope(raw_scope)
    except (TypeError, ValueError) as error:
        raise TaskConfigError(
            f"Task '{task_name}' has invalid TASK_SCOPE {raw_scope!r}; "
            "expected 'configured_target', 'target', or 'region'"
        ) from error


def _normalize_supported_task_scopes(
    supported_task_scopes: frozenset[str | TaskScope],
) -> tuple[TaskScope, ...]:
    """Normalize provider scope capabilities into a stable cache key."""

    try:
        return tuple(
            sorted(
                {TaskScope(scope) for scope in supported_task_scopes},
                key=lambda scope: scope.value,
            )
        )
    except (TypeError, ValueError) as error:
        raise TaskConfigError(
            "supported_task_scopes must contain only 'configured_target', "
            "'target', or 'region'"
        ) from error


def _build_resolved_execution(
    ordered: CachedOrderedTask, adjacency: CachedAdjacency
) -> ResolvedExecution:
    return ResolvedExecution(
        ordered=[
            ResolvedTask(
                id=task_id,
                name=name,
                run=run,
                depends_on=list(depends_on),
                always_run=always_run,
                metadata=json.loads(metadata_json),
                dependency_data={
                    local_name: {
                        "task_id": producer_id,
                        **({"path": path} if path is not None else {}),
                    }
                    for local_name, producer_id, path in dependency_data
                },
                scope=scope,
            )
            for (
                task_id,
                name,
                run,
                depends_on,
                always_run,
                metadata_json,
                dependency_data,
                scope,
            ) in ordered
        ],
        adjacency={name: list(children) for name, children in adjacency},
    )


@lru_cache(maxsize=128)
def _resolve_tasks_cached(
    provider_name: str,
    task_specs: TaskSpecKey,
    supported_task_scopes: tuple[TaskScope, ...],
) -> tuple[CachedOrderedTask, CachedAdjacency]:
    spec_by_id = {spec.id: spec for spec in task_specs}

    _validate_dependencies(spec_by_id)

    ordered_ids, adjacency = _topological_sort(spec_by_id)

    loaded_tasks: dict[str, tuple[Callable, TaskScope]] = {}
    supported_scope_set = frozenset(supported_task_scopes)
    for task_id in ordered_ids:
        component_name = spec_by_id[task_id].name
        run = _load_provider_task_callable(
            provider_name=provider_name, task_name=component_name
        )
        scope = _task_scope(task_name=component_name, run=run)
        if scope not in supported_scope_set:
            raise TaskConfigError(
                f"Task '{task_id}' component '{component_name}' declares scope "
                f"'{scope.value}', which provider '{provider_name}' does not support"
            )
        loaded_tasks[task_id] = (run, scope)

    ordered: CachedOrderedTask = tuple(
        (
            task_id,
            spec_by_id[task_id].name,
            loaded_tasks[task_id][0],
            tuple(spec_by_id[task_id].depends_on),
            spec_by_id[task_id].always_run,
            spec_by_id[task_id].metadata_json,
            spec_by_id[task_id].dependency_data,
            loaded_tasks[task_id][1],
        )
        for task_id in ordered_ids
    )
    frozen_adjacency: CachedAdjacency = tuple(
        (name, tuple(children)) for name, children in adjacency.items()
    )

    return ordered, frozen_adjacency


def _validate_dependencies(spec_by_id: dict[str, TaskSpec]) -> None:
    task_ids = set(spec_by_id)

    for task_id, spec in spec_by_id.items():
        for dependency in spec.depends_on:
            if dependency not in task_ids:
                raise TaskConfigError(
                    f"Task '{task_id}' depends on unknown task ID '{dependency}'"
                )

            if dependency == task_id:
                raise TaskConfigError(f"Task '{task_id}' cannot depend on itself")

        direct_dependencies = set(spec.depends_on)
        for local_name, producer_id, _ in spec.dependency_data:
            if producer_id not in task_ids:
                raise TaskConfigError(
                    f"Task '{task_id}' dependency_data.{local_name} references "
                    f"unknown task ID '{producer_id}'"
                )
            if producer_id not in direct_dependencies:
                raise TaskConfigError(
                    f"Task '{task_id}' dependency_data.{local_name} references "
                    f"'{producer_id}', which must appear directly in depends_on"
                )


def _topological_sort(
    spec_by_id: dict[str, TaskSpec],
) -> tuple[list[str], dict[str, list[str]]]:

    task_ids = list(spec_by_id)
    graph: dict[str, list[str]] = defaultdict(list)
    indegree: dict[str, int] = {task_id: 0 for task_id in task_ids}

    for task_id, spec in spec_by_id.items():
        for dependency in spec.depends_on:
            graph[dependency].append(task_id)
            indegree[task_id] += 1

    queue = deque(task_id for task_id in task_ids if indegree[task_id] == 0)
    ordered: list[str] = []

    while queue:
        node = queue.popleft()
        ordered.append(node)

        for child in graph[node]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)

    if len(ordered) != len(task_ids):
        cycle_ids = ", ".join(task_id for task_id in task_ids if indegree[task_id] > 0)
        raise TaskConfigError(
            f"Cycle detected in task dependencies involving: {cycle_ids}"
        )

    return ordered, dict(graph)


def resolve_tasks(
    *,
    task_specs: Sequence[TaskSpecInput],
    provider_name: str,
    supported_task_scopes: frozenset[str | TaskScope],
) -> ResolvedExecution:
    normalized_specs = _normalize_task_specs(task_specs)
    normalized_scopes = _normalize_supported_task_scopes(supported_task_scopes)
    ordered, adjacency = _resolve_tasks_cached(
        provider_name, normalized_specs, normalized_scopes
    )
    return _build_resolved_execution(ordered, adjacency)


def discover_tasks() -> TaskDiscoveryResult:
    """Discover built-in and plugin provider-aware tasks."""
    from anvil.provider_loader import list_providers

    tasks: set[TaskDescriptor] = set()
    issues: set[DiscoveryIssue] = set()
    provider_catalogs: dict[str, ComponentCatalog[Callable]] = {}
    for provider_name in sorted({provider.name for provider in list_providers()}):
        catalog = _provider_task_catalog(provider_name)
        provider_catalogs[provider_name] = catalog
        tasks.update(catalog.descriptors)
        issues.update(catalog.issues)

    return TaskDiscoveryResult(
        tasks=sorted(tasks, key=lambda task: (str(task.source), task.name)),
        issues=sorted(
            issues, key=lambda issue: (str(issue.source), issue.name, issue.error)
        ),
        provider_catalogs=MappingProxyType(provider_catalogs),
    )


def list_tasks() -> list[TaskDescriptor]:
    return sorted(
        discover_tasks().tasks, key=lambda task: (str(task.source), task.name)
    )
