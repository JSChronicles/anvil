from __future__ import annotations

import importlib
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


@dataclass(slots=True)
class _TaskSpec:
    depends_on: list[str]
    optional: bool


TaskSpecKey = tuple[tuple[str, tuple[str, ...], bool], ...]
CachedOrderedTask = tuple[tuple[str, Callable, tuple[str, ...], bool, TaskScope], ...]
CachedAdjacency = tuple[tuple[str, tuple[str, ...]], ...]


class TaskConfigError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TaskDescriptor:
    """Discovered task and lazy loader for its run callable."""

    name: str
    load: Callable[[], Callable]
    source: str


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
    descriptors = _provider_task_descriptor_index(provider_name).get(task_name, [])
    if not descriptors:
        raise TaskConfigError(
            f"Task '{task_name}' is not available for provider '{provider_name}'. "
            "Tasks must be provided by universal package 'anvil.providers.tasks' "
            f"or provider package 'anvil.providers.{provider_name}.tasks'."
        )

    catalog_descriptors = [
        CatalogDescriptor(
            name=descriptor.name,
            source=ComponentSource(
                origin=(
                    ComponentOrigin.PLUGIN
                    if "plugin:" in descriptor.source
                    else ComponentOrigin.STOCK
                ),
                package="",
                label=descriptor.source,
                provider=provider_name,
            ),
            load=descriptor.load,
        )
        for descriptor in descriptors
    ]
    return ComponentResolver(
        kind=ComponentKind.TASK,
        catalog=ComponentCatalog(descriptors=tuple(catalog_descriptors)),
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
        if "No module named" in issue.error and package_name in issue.error:
            return []
        raise TaskConfigError(f"{issue.name} ({issue.source}): {issue.error}")
    return [
        TaskDescriptor(
            name=descriptor.name, load=descriptor.load, source=str(descriptor.source)
        )
        for descriptor in descriptors
    ]


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
        descriptors.extend(
            TaskDescriptor(
                name=descriptor.name,
                load=descriptor.load,
                source=str(descriptor.source),
            )
            for descriptor in discovered
        )
        issues.extend(source_issues)

    return descriptors, issues


@lru_cache(maxsize=16)
def _provider_task_discovery(
    provider_name: str,
) -> tuple[dict[str, tuple[TaskDescriptor, ...]], tuple[DiscoveryIssue, ...]]:
    catalog_descriptors: list[CatalogDescriptor[Callable]] = []
    public_descriptors: dict[int, TaskDescriptor] = {}
    issues: list[DiscoveryIssue] = []

    for source, package_name in _provider_task_packages(provider_name):
        for descriptor in _iter_package_task_descriptors(
            package_name=package_name, source=source
        ):
            catalog_descriptor = CatalogDescriptor(
                name=descriptor.name,
                source=ComponentSource(
                    origin=ComponentOrigin.STOCK,
                    package=package_name,
                    label=descriptor.source,
                    provider=None if source == "universal" else provider_name,
                ),
                load=descriptor.load,
            )
            catalog_descriptors.append(catalog_descriptor)
            public_descriptors[id(catalog_descriptor)] = descriptor

    for source, entry_point_group in _provider_task_entry_point_groups(provider_name):
        plugin_descriptors, plugin_issues = _iter_plugin_task_descriptors(
            entry_point_group=entry_point_group, source_prefix=f"{source} plugin:"
        )
        issues.extend(plugin_issues)
        for descriptor in plugin_descriptors:
            catalog_descriptor = CatalogDescriptor(
                name=descriptor.name,
                source=ComponentSource(
                    origin=ComponentOrigin.PLUGIN,
                    package="",
                    label=descriptor.source,
                    entry_point_group=entry_point_group,
                    provider=None if source == "universal" else provider_name,
                ),
                load=descriptor.load,
            )
            catalog_descriptors.append(catalog_descriptor)
            public_descriptors[id(catalog_descriptor)] = descriptor

    catalog = ComponentCatalog(descriptors=tuple(catalog_descriptors))
    index = {
        name: tuple(public_descriptors[id(descriptor)] for descriptor in descriptors)
        for name, descriptors in catalog.inventory.items()
    }
    return index, tuple(issues)


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
    cache_clear = getattr(_provider_task_discovery, "cache_clear", None)
    if cache_clear is not None:
        cache_clear()


# ============================================================================
# Public API
# ============================================================================


TaskSpecInput = Mapping[str, object]


def _freeze_task_specs(task_specs: Sequence[TaskSpecInput]) -> TaskSpecKey:
    frozen_specs: list[tuple[str, tuple[str, ...], bool]] = []

    for spec in task_specs:
        name = spec["name"]
        if not isinstance(name, str):
            raise TaskConfigError("task name must be a string")

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

        frozen_specs.append(
            (name, tuple(depends_on), bool(spec.get("optional", False)))
        )

    return tuple(frozen_specs)


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
    task_specs_key: TaskSpecKey,
    supported_task_scopes: tuple[TaskScope, ...],
) -> tuple[CachedOrderedTask, CachedAdjacency]:
    task_specs: list[dict[str, object]] = [
        {"name": name, "depends_on": list(depends_on), "optional": optional}
        for name, depends_on, optional in task_specs_key
    ]

    spec_by_name = _parse_task_specs(task_specs)

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


def _parse_task_specs(task_specs: Sequence[TaskSpecInput]) -> dict[str, _TaskSpec]:
    spec_by_name: dict[str, _TaskSpec] = {}

    for spec in task_specs:
        name = spec["name"]
        if not isinstance(name, str):
            raise TaskConfigError("task name must be a string")

        if name in spec_by_name:
            raise TaskConfigError(f"Duplicate task name detected: '{name}'")

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

        spec_by_name[name] = _TaskSpec(
            depends_on=depends_on, optional=bool(spec.get("optional", False))
        )

    return spec_by_name


def _validate_dependencies(spec_by_name: dict[str, _TaskSpec]) -> None:
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
    spec_by_name: dict[str, _TaskSpec],
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
    provider_name: str = "aws",
    supported_task_scopes: frozenset[str | TaskScope] = frozenset({"region"}),
) -> ResolvedExecution:
    task_specs_key = _freeze_task_specs(task_specs)
    normalized_scopes = _normalize_supported_task_scopes(supported_task_scopes)
    ordered, adjacency = _resolve_tasks_cached(
        provider_name, task_specs_key, normalized_scopes
    )
    return _build_resolved_execution(ordered, adjacency)


def discover_tasks() -> TaskDiscoveryResult:
    """Discover built-in and plugin provider-aware tasks."""
    from anvil.provider_loader import list_providers

    tasks: list[TaskDescriptor] = []
    task_keys: set[tuple[str, str, int]] = set()
    issue_keys: set[tuple[str, ComponentSource, str]] = set()
    issues: list[DiscoveryIssue] = []
    for provider_name in sorted({provider.name for provider in list_providers()}):
        index, provider_issues = _provider_task_discovery(provider_name)
        for name, descriptors in index.items():
            source_counts: dict[tuple[str, str], int] = defaultdict(int)
            for descriptor in descriptors:
                source_key = (descriptor.source, name)
                ordinal = source_counts[source_key]
                source_counts[source_key] += 1
                task_key = (descriptor.source, name, ordinal)
                if task_key in task_keys:
                    continue
                task_keys.add(task_key)
                tasks.append(descriptor)
        for issue in provider_issues:
            key = (issue.name, issue.source, issue.error)
            if key in issue_keys:
                continue
            issue_keys.add(key)
            issues.append(issue)

    return TaskDiscoveryResult(tasks=tasks, issues=issues)


def list_tasks() -> list[TaskDescriptor]:
    return sorted(discover_tasks().tasks, key=lambda task: (task.source, task.name))
