from __future__ import annotations

import importlib
import importlib.util
import sys
from collections import defaultdict, deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from importlib.metadata import entry_points

from anvil._components import (
    ComponentCatalog,
    ComponentDescriptor as CatalogDescriptor,
    ComponentKind,
    ComponentOrigin,
    ComponentResolver,
    ComponentSource,
    DiscoveryIssue,
    PackageComponentSource,
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


@dataclass(frozen=True, slots=True)
class ResolvedTask:
    name: str
    run: Callable
    depends_on: list[str]
    optional: bool
    scope: TaskScope = TaskScope.REGION


@dataclass(frozen=True, slots=True)
class TaskSpec:
    """Immutable normalized task declaration."""

    name: str
    depends_on: tuple[str, ...]
    optional: bool


TaskSpecKey = tuple[TaskSpec, ...]
CachedOrderedTask = tuple[tuple[str, Callable, tuple[str, ...], bool, TaskScope], ...]
CachedAdjacency = tuple[tuple[str, tuple[str, ...]], ...]


class TaskConfigError(RuntimeError):
    pass


TaskDescriptor = CatalogDescriptor[Callable]


@dataclass(frozen=True, slots=True)
class TaskDiscoveryResult:
    """Discovered tasks and non-fatal discovery issues."""

    tasks: list[TaskDescriptor]
    issues: list[DiscoveryIssue]


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
    descriptor_index, discovery_issues = _provider_task_discovery(provider_name)
    descriptors = descriptor_index.get(task_name, [])
    if not descriptors:
        issue_detail = ""
        if discovery_issues:
            issue_lines = "; ".join(
                f"{issue.name} ({issue.source}): {issue.error}"
                for issue in discovery_issues
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
        catalog=ComponentCatalog(descriptors=tuple(descriptors)),
        error_type=TaskConfigError,
        context=f"for provider '{provider_name}'",
    ).load(task_name)


def _provider_task_packages(provider_name: str) -> tuple[tuple[str, str], ...]:
    return (
        ("universal", UNIVERSAL_TASK_PACKAGE),
        (provider_name, f"{PROVIDER_TASK_PACKAGE_PREFIX}.{provider_name}.tasks"),
    )


def _provider_task_entry_point_groups(
    provider_name: str,
) -> tuple[tuple[str, str], ...]:
    return (
        ("universal", UNIVERSAL_TASK_ENTRY_POINT_GROUP),
        (
            provider_name,
            f"{PROVIDER_TASK_ENTRY_POINT_GROUP_PREFIX}.{provider_name}.tasks",
        ),
    )


def _iter_package_task_descriptors(
    *, package_name: str, source: str
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
        label=source,
        provider=None if source == "universal" else source,
    )
    descriptors, issues = PackageComponentSource(
        kind=ComponentKind.TASK,
        package_name=package_name,
        source=component_source,
        component_loader=_load_task_component,
    ).discover()
    if issues:
        issue = issues[0]
        raise TaskConfigError(f"{issue.name} ({issue.source}): {issue.error}")
    return list(descriptors)


def _iter_plugin_task_descriptors(
    *, entry_point_group: str, source_prefix: str
) -> tuple[list[TaskDescriptor], list[DiscoveryIssue]]:
    descriptors: list[TaskDescriptor] = []
    issues: list[DiscoveryIssue] = []

    for entry_point in entry_points(group=entry_point_group):
        distribution = entry_point.dist.name if entry_point.dist is not None else None
        source = f"{source_prefix} {distribution or 'unpackaged'}"
        package_name = entry_point.value.split(":", maxsplit=1)[0]
        component_source = ComponentSource(
            origin=ComponentOrigin.PLUGIN,
            package=package_name,
            label=source,
            distribution=(
                entry_point.dist.name if entry_point.dist is not None else None
            ),
            entry_point_group=entry_point_group,
            entry_point_name=entry_point.name,
            provider=None if source_prefix.startswith("universal") else source_prefix,
        )
        discovered, source_issues = PackageComponentSource(
            kind=ComponentKind.TASK,
            package_name=package_name,
            source=component_source,
            component_loader=_load_task_component,
        ).discover(issue_name=entry_point.name)
        descriptors.extend(discovered)
        issues.extend(source_issues)

    return descriptors, issues


@lru_cache(maxsize=16)
def _provider_task_discovery(
    provider_name: str,
) -> tuple[dict[str, tuple[TaskDescriptor, ...]], tuple[DiscoveryIssue, ...]]:
    catalog_descriptors: list[CatalogDescriptor[Callable]] = []
    issues: list[DiscoveryIssue] = []

    for source, package_name in _provider_task_packages(provider_name):
        catalog_descriptors.extend(
            _iter_package_task_descriptors(package_name=package_name, source=source)
        )

    for source, entry_point_group in _provider_task_entry_point_groups(provider_name):
        plugin_descriptors, plugin_issues = _iter_plugin_task_descriptors(
            entry_point_group=entry_point_group, source_prefix=f"{source} plugin:"
        )
        issues.extend(plugin_issues)
        catalog_descriptors.extend(plugin_descriptors)

    catalog = ComponentCatalog.build(catalog_descriptors, issues)
    return dict(catalog.inventory), catalog.issues


def _provider_task_descriptor_index(
    provider_name: str,
) -> dict[str, tuple[TaskDescriptor, ...]]:
    return _provider_task_discovery(provider_name)[0]


def provider_task_descriptor_index(
    *, provider_name: str
) -> dict[str, list[TaskDescriptor]]:
    """Return cached provider-aware task descriptors by task name."""

    return {
        name: list(descriptors)
        for name, descriptors in _provider_task_descriptor_index(provider_name).items()
    }


def _clear_task_caches() -> None:
    _load_provider_task_callable.cache_clear()
    _resolve_tasks_cached.cache_clear()
    cache_clear = getattr(_provider_task_discovery, "cache_clear", None)
    if cache_clear is not None:
        cache_clear()


# ============================================================================
# Public API
# ============================================================================


TaskSpecInput = Mapping[str, object]


def _normalize_task_specs(task_specs: Sequence[TaskSpecInput]) -> TaskSpecKey:
    """Validate task declarations once while preserving declaration order."""

    normalized_specs: list[TaskSpec] = []
    seen_names: set[str] = set()

    for spec in task_specs:
        name = spec.get("name")
        if not isinstance(name, str) or not name:
            raise TaskConfigError("task name must be a non-empty string")
        if name in seen_names:
            raise TaskConfigError(f"Duplicate task name detected: '{name}'")
        seen_names.add(name)

        raw_depends_on = spec.get("depends_on", [])
        if not isinstance(raw_depends_on, list):
            raise TaskConfigError(f"Task '{name}' depends_on must be a list of strings")
        depends_on: list[str] = []
        for dependency in raw_depends_on:
            if not isinstance(dependency, str):
                raise TaskConfigError(
                    f"Task '{name}' depends_on must be a list of strings"
                )
            depends_on.append(dependency)
        if len(set(depends_on)) != len(depends_on):
            raise TaskConfigError(
                f"Task '{name}' depends_on must not contain duplicates"
            )

        optional = spec.get("optional", False)
        if not isinstance(optional, bool):
            raise TaskConfigError(f"Task '{name}' optional must be a boolean")

        normalized_specs.append(
            TaskSpec(name=name, depends_on=tuple(depends_on), optional=optional)
        )

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
            "expected 'region' or 'target'"
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
            "supported_task_scopes must contain only 'region' or 'target'"
        ) from error


def _build_resolved_execution(
    ordered: CachedOrderedTask, adjacency: CachedAdjacency
) -> ResolvedExecution:
    return ResolvedExecution(
        ordered=[
            ResolvedTask(
                name=name,
                run=run,
                depends_on=list(depends_on),
                optional=optional,
                scope=scope,
            )
            for name, run, depends_on, optional, scope in ordered
        ],
        adjacency={name: list(children) for name, children in adjacency},
    )


@lru_cache(maxsize=128)
def _resolve_tasks_cached(
    provider_name: str,
    task_specs: TaskSpecKey,
    supported_task_scopes: tuple[TaskScope, ...],
) -> tuple[CachedOrderedTask, CachedAdjacency]:
    spec_by_name = {spec.name: spec for spec in task_specs}

    _validate_dependencies(spec_by_name)

    ordered_names, adjacency = _topological_sort(spec_by_name)

    loaded_tasks: dict[str, tuple[Callable, TaskScope]] = {}
    supported_scope_set = frozenset(supported_task_scopes)
    for name in ordered_names:
        run = _load_provider_task_callable(provider_name=provider_name, task_name=name)
        scope = _task_scope(task_name=name, run=run)
        if scope not in supported_scope_set:
            raise TaskConfigError(
                f"Task '{name}' declares scope '{scope.value}', which provider "
                f"'{provider_name}' does not support"
            )
        loaded_tasks[name] = (run, scope)

    for name, spec in spec_by_name.items():
        if loaded_tasks[name][1] is not TaskScope.TARGET:
            continue
        regional_dependencies = [
            dependency
            for dependency in spec.depends_on
            if loaded_tasks[dependency][1] is TaskScope.REGION
        ]
        if regional_dependencies:
            dependencies = ", ".join(regional_dependencies)
            raise TaskConfigError(
                f"TARGET task '{name}' cannot depend on REGION task(s): "
                f"{dependencies}. TARGET tasks execute before regional fan-out"
            )

    ordered: CachedOrderedTask = tuple(
        (
            name,
            loaded_tasks[name][0],
            tuple(spec_by_name[name].depends_on),
            spec_by_name[name].optional,
            loaded_tasks[name][1],
        )
        for name in ordered_names
    )
    frozen_adjacency: CachedAdjacency = tuple(
        (name, tuple(children)) for name, children in adjacency.items()
    )

    return ordered, frozen_adjacency


def _validate_dependencies(spec_by_name: dict[str, TaskSpec]) -> None:
    names = set(spec_by_name.keys())

    for task_name, spec in spec_by_name.items():
        for dep in spec.depends_on:
            if dep not in names:
                raise TaskConfigError(
                    f"Task '{task_name}' depends on unknown task '{dep}'"
                )

            if dep == task_name:
                raise TaskConfigError(f"Task '{task_name}' cannot depend on itself")


def _topological_sort(
    spec_by_name: dict[str, TaskSpec],
) -> tuple[list[str], dict[str, list[str]]]:

    names = list(spec_by_name.keys())
    graph: dict[str, list[str]] = defaultdict(list)
    indegree: dict[str, int] = {name: 0 for name in names}

    for name, spec in spec_by_name.items():
        for dep in spec.depends_on:
            graph[dep].append(name)
            indegree[name] += 1

    queue = deque(name for name in names if indegree[name] == 0)
    ordered: list[str] = []

    while queue:
        node = queue.popleft()
        ordered.append(node)

        for child in graph[node]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)

    if len(ordered) != len(names):
        raise TaskConfigError("Cycle detected in task dependencies")

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
    for provider_name in sorted({provider.name for provider in list_providers()}):
        index, provider_issues = _provider_task_discovery(provider_name)
        for descriptors in index.values():
            tasks.update(descriptors)
        issues.update(provider_issues)

    return TaskDiscoveryResult(
        tasks=sorted(tasks, key=lambda task: (str(task.source), task.name)),
        issues=sorted(
            issues, key=lambda issue: (str(issue.source), issue.name, issue.error)
        ),
    )


def list_tasks() -> list[TaskDescriptor]:
    return sorted(
        discover_tasks().tasks, key=lambda task: (str(task.source), task.name)
    )
