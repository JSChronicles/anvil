from __future__ import annotations

import logging
from collections import defaultdict, deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache

from anvil._loader_utils import (
    iter_plugin_modules,
    iter_stock_modules,
    load_plugin_callable,
    load_stock_callable,
)

__LOGGER__ = logging.getLogger(__name__)

TASK_ENTRY_POINT_GROUP = "anvil.tasks"

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
    name: str
    run: Callable
    source: str


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


@lru_cache(maxsize=128)
def _load_task_callable(task_name: str) -> Callable:
    try:
        return _load_core_task(task_name)
    except TaskConfigError:
        return _load_plugin_task(task_name)


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
    task_specs_key: TaskSpecKey,
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
            _load_task_callable(name),
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


def resolve_tasks(*, task_specs: Sequence[TaskSpecInput]) -> ResolvedExecution:
    task_specs_key = _freeze_task_specs(task_specs)
    ordered, adjacency = _resolve_tasks_cached(task_specs_key)
    return _build_resolved_execution(ordered, adjacency)


def discover_tasks() -> list[TaskDescriptor]:
    tasks: dict[str, TaskDescriptor] = {}

    for module in iter_stock_modules(package_name="anvil.tasks", load=_load_core_task):
        name = module.name
        tasks[name] = TaskDescriptor(name=name, run=module.load, source=module.source)

    for module in iter_plugin_modules(
        entry_point_group=TASK_ENTRY_POINT_GROUP,
        load=_load_plugin_task,
        logger=__LOGGER__,
        skip_log_label="plugin",
    ):
        if module.name in tasks:
            continue

        tasks[module.name] = TaskDescriptor(
            name=module.name, run=module.load, source=module.source
        )

    return list(tasks.values())


def list_tasks() -> list[TaskDescriptor]:
    return sorted(discover_tasks(), key=lambda task: (task.source, task.name))
