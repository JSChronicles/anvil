from __future__ import annotations

import logging
from collections import defaultdict, deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache

from anvil._loader_utils import (
    DiscoveryIssue,
    discover_plugin_modules,
    iter_stock_modules,
    load_plugin_callable,
    load_stock_callable,
)

__LOGGER__ = logging.getLogger(__name__)

TASK_ENTRY_POINT_GROUP = "anvil.tasks"
UNIVERSAL_TASK_PACKAGE = "anvil.providers.tasks"
PROVIDER_TASK_PACKAGE_PREFIX = "anvil.providers"

# ============================================================================
# Models
# ============================================================================


@dataclass(frozen=True, slots=True)
class ResolvedTask:
    name: str
    run: Callable
    depends_on: list[str]
    optional: bool


@dataclass(slots=True)
class _TaskSpec:
    depends_on: list[str]
    optional: bool


TaskSpecKey = tuple[tuple[str, tuple[str, ...], bool], ...]
CachedOrderedTask = tuple[tuple[str, Callable, tuple[str, ...], bool], ...]
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


def _load_core_task(task_name: str) -> Callable:
    return load_stock_callable(
        name=task_name,
        kind="task",
        package_name="anvil.tasks",
        error_type=TaskConfigError,
    )


def _load_plugin_task(task_name: str) -> Callable:
    return load_plugin_callable(
        name=task_name,
        kind="task",
        entry_point_group=TASK_ENTRY_POINT_GROUP,
        error_type=TaskConfigError,
        logger=__LOGGER__,
        import_failure_log_label="plugin package",
        import_issue_log_label=task_name,
    )


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
    if descriptors:
        return descriptors[0].load()

    if provider_name == "aws":
        try:
            return _load_core_task(task_name)
        except TaskConfigError:
            return _load_plugin_task(task_name)

    raise TaskConfigError(
        f"Task '{task_name}' is not available for provider '{provider_name}'. "
        "Legacy anvil.tasks plugin entry points are AWS-compatible only; use "
        "universal or provider-specific task packages for non-AWS providers."
    )


def _provider_task_packages(provider_name: str) -> tuple[tuple[str, str], ...]:
    return (
        ("universal", UNIVERSAL_TASK_PACKAGE),
        (provider_name, f"{PROVIDER_TASK_PACKAGE_PREFIX}.{provider_name}.tasks"),
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


def _legacy_task_descriptors() -> tuple[list[TaskDescriptor], list[DiscoveryIssue]]:
    tasks: dict[str, TaskDescriptor] = {}

    for module in iter_stock_modules(package_name="anvil.tasks", load=_load_core_task):
        name = module.name
        tasks[name] = TaskDescriptor(name=name, load=module.load, source=module.source)

    plugin_result = discover_plugin_modules(
        entry_point_group=TASK_ENTRY_POINT_GROUP,
        load=_load_plugin_task,
        logger=__LOGGER__,
        skip_log_label="plugin",
    )
    for module in plugin_result.modules:
        if module.name in tasks:
            continue

        tasks[module.name] = TaskDescriptor(
            name=module.name, load=module.load, source=module.source
        )

    return list(tasks.values()), plugin_result.issues


@lru_cache(maxsize=16)
def _provider_task_descriptor_index(
    provider_name: str,
) -> dict[str, tuple[TaskDescriptor, ...]]:
    descriptors_by_name: dict[str, list[TaskDescriptor]] = defaultdict(list)

    legacy_descriptors, _ = _legacy_task_descriptors()
    if provider_name == "aws":
        for descriptor in legacy_descriptors:
            descriptors_by_name[descriptor.name].append(descriptor)

    for source, package_name in _provider_task_packages(provider_name):
        for descriptor in _iter_package_task_descriptors(
            package_name=package_name, source=source
        ):
            descriptors_by_name[descriptor.name].append(descriptor)

    return {
        name: tuple(descriptors)
        for name, descriptors in sorted(descriptors_by_name.items())
    }


def provider_task_descriptor_index(
    *, provider_name: str
) -> dict[str, list[TaskDescriptor]]:
    """Return cached provider-aware task descriptors by task name."""

    return {
        name: list(descriptors)
        for name, descriptors in _provider_task_descriptor_index(provider_name).items()
    }


@lru_cache(maxsize=128)
def _load_task_callable_cached(task_name: str) -> Callable:
    return _load_provider_task_callable(provider_name="aws", task_name=task_name)


def _load_task_callable(task_name: str) -> Callable:
    return _load_task_callable_cached(task_name)


def _clear_task_callable_cache() -> None:
    _load_task_callable_cached.cache_clear()
    _load_provider_task_callable.cache_clear()
    _provider_task_descriptor_index.cache_clear()


_load_task_callable.cache_clear = _clear_task_callable_cache


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


def _build_resolved_execution(
    ordered: CachedOrderedTask, adjacency: CachedAdjacency
) -> ResolvedExecution:
    return ResolvedExecution(
        ordered=[
            ResolvedTask(
                name=name, run=run, depends_on=list(depends_on), optional=optional
            )
            for name, run, depends_on, optional in ordered
        ],
        adjacency={name: list(children) for name, children in adjacency},
    )


@lru_cache(maxsize=128)
def _resolve_tasks_cached(
    provider_name: str, task_specs_key: TaskSpecKey
) -> tuple[CachedOrderedTask, CachedAdjacency]:
    task_specs: list[dict[str, object]] = [
        {"name": name, "depends_on": list(depends_on), "optional": optional}
        for name, depends_on, optional in task_specs_key
    ]

    spec_by_name = _parse_task_specs(task_specs)

    _validate_dependencies(spec_by_name)

    ordered_names, adjacency = _topological_sort(spec_by_name)

    ordered: CachedOrderedTask = tuple(
        (
            name,
            (
                _load_task_callable(name)
                if provider_name == "aws"
                else _load_provider_task_callable(
                    provider_name=provider_name, task_name=name
                )
            ),
            tuple(spec_by_name[name].depends_on),
            spec_by_name[name].optional,
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
    *, task_specs: Sequence[TaskSpecInput], provider_name: str = "aws"
) -> ResolvedExecution:
    task_specs_key = _freeze_task_specs(task_specs)
    ordered, adjacency = _resolve_tasks_cached(provider_name, task_specs_key)
    return _build_resolved_execution(ordered, adjacency)


def discover_tasks() -> TaskDiscoveryResult:
    """Discover tasks and report plugin packages that cannot be inspected."""
    tasks, issues = _legacy_task_descriptors()
    return TaskDiscoveryResult(tasks=tasks, issues=issues)


def list_tasks() -> list[TaskDescriptor]:
    return sorted(discover_tasks().tasks, key=lambda task: (task.source, task.name))
