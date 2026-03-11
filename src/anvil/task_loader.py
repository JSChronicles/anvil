from __future__ import annotations

import importlib
import logging
import pkgutil
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import entry_points

__LOGGER__ = logging.getLogger(__name__)

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
    module_name = f"anvil.tasks.{task_name}"

    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError:
        raise TaskConfigError(f"Core task '{task_name}' not found")

    run = getattr(module, "run", None)
    if not callable(run):
        raise TaskConfigError(
            f"Core task '{task_name}' must define a callable run(...)"
        )

    return run


def _load_plugin_task(task_name: str) -> Callable:
    eps = entry_points(group="anvil.tasks")

    discovered_plugins: list[str] = []
    import_failures: list[str] = []

    for entry_point in eps:
        discovered_plugins.append(entry_point.name)

        # Import plugin package
        try:
            pkg = importlib.import_module(entry_point.value)
        except Exception as exc:
            __LOGGER__.debug(
                f"Failed importing plugin package '{entry_point.value}' "
                f"(entry point '{entry_point.name}'): {exc}"
            )
            import_failures.append(f"{entry_point.name}: package import failed ({exc})")
            continue

        # Try loading task module inside plugin
        try:
            module = importlib.import_module(f"{pkg.__name__}.{task_name}")
        except ModuleNotFoundError:
            # Plugin simply doesn't provide this task
            continue
        except Exception as exc:
            raise TaskConfigError(
                f"Plugin task '{task_name}' in plugin "
                f"'{entry_point.name}' failed during import: {exc}"
            ) from exc

        run = getattr(module, "run", None)
        if not callable(run):
            raise TaskConfigError(
                f"Plugin task '{task_name}' in plugin "
                f"'{entry_point.name}' must define callable run(...)"
            )

        return run

    if import_failures:
        __LOGGER__.debug(
            f"Plugin import issues encountered while resolving '{task_name}': "
            f"{import_failures}"
        )

    raise TaskConfigError(
        f"Plugin task '{task_name}' not found in registered entry points: "
        f"{discovered_plugins}"
    )


def _load_task_callable(task_name: str) -> Callable:
    try:
        return _load_core_task(task_name)
    except TaskConfigError:
        return _load_plugin_task(task_name)


# ============================================================================
# Public API
# ============================================================================


def _parse_task_specs(task_specs: list[dict[str, object]]) -> dict[str, _TaskSpec]:
    spec_by_name: dict[str, _TaskSpec] = {}

    for spec in task_specs:
        name = spec["name"]

        if name in spec_by_name:
            raise TaskConfigError(f"Duplicate task name detected: '{name}'")

        spec_by_name[name] = _TaskSpec(
            depends_on=spec.get("depends_on", []), optional=spec.get("optional", False)
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


def resolve_tasks(*, task_specs: list[dict[str, object]]) -> ResolvedExecution:
    spec_by_name = _parse_task_specs(task_specs)

    _validate_dependencies(spec_by_name)

    ordered_names, adjacency = _topological_sort(spec_by_name)

    resolved = [
        ResolvedTask(
            name=name,
            run=_load_task_callable(name),
            depends_on=spec_by_name[name].depends_on,
            optional=spec_by_name[name].optional,
        )
        for name in ordered_names
    ]

    return ResolvedExecution(ordered=resolved, adjacency=adjacency)


def discover_tasks() -> list[TaskDescriptor]:
    tasks: dict[str, TaskDescriptor] = {}

    # Core tasks
    import anvil.tasks

    for module_info in pkgutil.iter_modules(anvil.tasks.__path__):
        name = module_info.name
        if name.startswith("_"):
            continue

        tasks[name] = TaskDescriptor(
            name=name, run=lambda n=name: _load_core_task(n), source="stock"
        )

    # Plugin tasks (package scan, no imports)
    for entry_point in entry_points(group="anvil.tasks"):
        try:
            pkg = importlib.import_module(entry_point.value)
        except Exception as exc:
            __LOGGER__.debug(
                f"Skipping plugin '{entry_point.name}' due to import error: {exc}"
            )
            continue

        for module_info in pkgutil.iter_modules(pkg.__path__):
            name = module_info.name
            if name.startswith("_") or name in tasks:
                continue

            source = (
                f"plugin: {entry_point.dist.name}"
                if entry_point.dist is not None
                else "plugin (unpackaged)"
            )

            tasks[name] = TaskDescriptor(
                name=name, run=lambda n=name: _load_plugin_task(n), source=source
            )

    return list(tasks.values())


def list_tasks() -> list[str]:
    tasks = discover_tasks()
    return [
        f"{task.name} [{task.source}]" for task in sorted(tasks, key=lambda t: t.name)
    ]
