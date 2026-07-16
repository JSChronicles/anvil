from __future__ import annotations

import importlib
import logging
import pkgutil
import sys
from collections import defaultdict, deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from importlib.metadata import EntryPoint, entry_points

from anvil._loader_utils import (
    DiscoveryIssue,
    iter_stock_modules,
    load_stock_callable,
    plugin_source,
)

__LOGGER__ = logging.getLogger(__name__)

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
    return load_stock_callable(
        name=task_name,
        kind="task",
        package_name=package_name,
        error_type=TaskConfigError,
    )


@lru_cache(maxsize=512)
def _load_provider_task_callable(*, provider_name: str, task_name: str) -> Callable:
    descriptors = _provider_task_descriptor_index(provider_name).get(task_name, [])
    if len(descriptors) == 1:
        return descriptors[0].load()
    if len(descriptors) > 1:
        sources = ", ".join(descriptor.source for descriptor in descriptors)
        raise TaskConfigError(
            f"Task '{task_name}' is ambiguous for provider '{provider_name}'; "
            f"found in multiple applicable task packages: {sources}."
        )

    raise TaskConfigError(
        f"Task '{task_name}' is not available for provider '{provider_name}'. "
        "Tasks must be provided by universal package 'anvil.providers.tasks' "
        f"or provider package 'anvil.providers.{provider_name}.tasks'."
    )


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
        modules = list(
            iter_stock_modules(
                package_name=package_name,
                load=lambda name: _load_package_task(
                    task_name=name, package_name=package_name
                ),
            )
        )
    except ModuleNotFoundError as error:
        if error.name == package_name:
            return []
        raise

    return [
        TaskDescriptor(name=module.name, load=module.load, source=source)
        for module in modules
    ]


def _load_plugin_task_callable(
    *, entry_point: EntryPoint, task_name: str, source: str
) -> Callable:
    try:
        package = importlib.import_module(entry_point.value)
    except Exception as exc:
        raise TaskConfigError(
            f"Plugin task package '{entry_point.name}' ({source}) failed during "
            f"import: {exc}"
        ) from exc

    module_name = f"{package.__name__}.{task_name}"
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name == module_name:
            raise TaskConfigError(
                f"Plugin task '{task_name}' not found in plugin "
                f"'{entry_point.name}' ({source})"
            ) from exc
        raise TaskConfigError(
            f"Plugin task '{task_name}' in plugin '{entry_point.name}' "
            f"({source}) failed during import: {exc}"
        ) from exc
    except Exception as exc:
        raise TaskConfigError(
            f"Plugin task '{task_name}' in plugin '{entry_point.name}' "
            f"({source}) failed during import: {exc}"
        ) from exc

    run = getattr(module, "run", None)
    if not callable(run):
        raise TaskConfigError(
            f"Plugin task '{task_name}' in plugin '{entry_point.name}' "
            f"({source}) must define callable run(...)"
        )

    return run


def _iter_plugin_task_descriptors(
    *, entry_point_group: str, source_prefix: str
) -> tuple[list[TaskDescriptor], list[DiscoveryIssue]]:
    descriptors: list[TaskDescriptor] = []
    issues: list[DiscoveryIssue] = []

    for entry_point in entry_points(group=entry_point_group):
        plugin_source_label = plugin_source(entry_point)
        if plugin_source_label.startswith("plugin: "):
            plugin_source_label = plugin_source_label.removeprefix("plugin: ")
        source = f"{source_prefix} {plugin_source_label}"
        try:
            package = importlib.import_module(entry_point.value)
        except Exception as exc:
            __LOGGER__.debug(
                f"Skipping task plugin '{entry_point.name}' from group "
                f"'{entry_point_group}' due to import error: {exc}"
            )
            issues.append(
                DiscoveryIssue(
                    name=entry_point.name,
                    source=source,
                    error=f"package import failed ({exc})",
                )
            )
            continue

        package_path = getattr(package, "__path__", None)
        if package_path is None:
            issues.append(
                DiscoveryIssue(
                    name=entry_point.name,
                    source=source,
                    error="entry point must reference a package",
                )
            )
            continue

        for module_info in pkgutil.iter_modules(package_path):
            name = module_info.name
            if name.startswith("_"):
                continue

            descriptors.append(
                TaskDescriptor(
                    name=name,
                    load=lambda ep=entry_point, n=name, s=source: (
                        _load_plugin_task_callable(
                            entry_point=ep, task_name=n, source=s
                        )
                    ),
                    source=source,
                )
            )

    return descriptors, issues


@lru_cache(maxsize=16)
def _provider_task_discovery(
    provider_name: str,
) -> tuple[dict[str, tuple[TaskDescriptor, ...]], tuple[DiscoveryIssue, ...]]:
    descriptors_by_name: dict[str, list[TaskDescriptor]] = defaultdict(list)
    issues: list[DiscoveryIssue] = []

    for source, package_name in _provider_task_packages(provider_name):
        for descriptor in _iter_package_task_descriptors(
            package_name=package_name, source=source
        ):
            descriptors_by_name[descriptor.name].append(descriptor)

    for source, entry_point_group in _provider_task_entry_point_groups(provider_name):
        plugin_descriptors, plugin_issues = _iter_plugin_task_descriptors(
            entry_point_group=entry_point_group, source_prefix=f"{source} plugin:"
        )
        issues.extend(plugin_issues)
        for descriptor in plugin_descriptors:
            descriptors_by_name[descriptor.name].append(descriptor)

    index = {
        name: tuple(descriptors)
        for name, descriptors in sorted(descriptors_by_name.items())
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
    tasks: list[TaskDescriptor] = []
    task_keys: set[tuple[str, str, int]] = set()
    issue_keys: set[tuple[str, str, str]] = set()
    issues: list[DiscoveryIssue] = []
    for provider_name in ("aws", "azure", "gcp", "github"):
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
