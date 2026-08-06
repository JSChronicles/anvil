"""Pure expansion of resolved tasks into deterministic execution instances."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from anvil.providers.base import ExecutionTarget
from anvil.task_loader import ResolvedTask, TaskScope


class TaskPlanningError(ValueError):
    """Raised when tasks cannot be expanded over an execution topology."""


@dataclass(frozen=True, slots=True)
class TaskInstanceKey:
    """Stable identity for one planned task invocation."""

    task_id: str
    scope: TaskScope
    execution_target_id: str
    region: str


@dataclass(frozen=True, slots=True)
class TaskInstance:
    """One task invocation and the instances that must settle before it."""

    key: TaskInstanceKey
    task: ResolvedTask
    execution_target: ExecutionTarget
    region: str
    dependencies: tuple[TaskInstanceKey, ...]


@dataclass(frozen=True, slots=True)
class TaskInstancePlan:
    """Deterministically ordered task instances and their outgoing edges."""

    instances: tuple[TaskInstance, ...]
    adjacency: dict[TaskInstanceKey, tuple[TaskInstanceKey, ...]]


def plan_task_instances(
    *,
    tasks: Sequence[ResolvedTask],
    execution_targets: Sequence[ExecutionTarget],
    configured_target: ExecutionTarget | None,
) -> TaskInstancePlan:
    """Expand task declarations over provider-owned execution topology.

    Args:
        tasks: Resolved tasks in dependency order.
        execution_targets: Selected targets in provider-defined order.
        configured_target: Concrete identity of the provider configuration owner.

    Returns:
        A pure, deterministic execution-instance plan.

    Raises:
        TaskPlanningError: If task references or target topology are invalid.
    """

    ordered_tasks = tuple(tasks)
    ordered_targets = tuple(execution_targets)
    _validate_tasks(ordered_tasks)
    _validate_execution_targets(ordered_targets)
    if configured_target is not None:
        _validate_target(configured_target, label="configured target")

    if (
        any(task.scope is TaskScope.CONFIGURED_TARGET for task in ordered_tasks)
        and configured_target is None
    ):
        raise TaskPlanningError(
            "configured-target tasks require a concrete configured-target identity"
        )

    shells_by_task_id: dict[
        str, list[tuple[TaskInstanceKey, ResolvedTask, ExecutionTarget, str]]
    ] = {}
    for task in ordered_tasks:
        shells_by_task_id[task.id] = _expand_task(
            task=task,
            execution_targets=ordered_targets,
            configured_target=configured_target,
        )

    instances: list[TaskInstance] = []
    for task in ordered_tasks:
        for key, resolved_task, target, region in shells_by_task_id[task.id]:
            dependencies: list[TaskInstanceKey] = []
            for dependency_id in task.depends_on:
                dependency_shells = shells_by_task_id[dependency_id]
                matches = [
                    dependency_key
                    for dependency_key, _task, _target, _region in dependency_shells
                    if _dependency_applies(producer=dependency_key, consumer=key)
                ]
                if not matches:
                    raise TaskPlanningError(
                        f"task '{task.id}' has no matching instance of dependency "
                        f"'{dependency_id}' for target '{key.execution_target_id}' "
                        f"and region '{key.region}'"
                    )
                dependencies.extend(matches)

            instances.append(
                TaskInstance(
                    key=key,
                    task=resolved_task,
                    execution_target=target,
                    region=region,
                    dependencies=tuple(dependencies),
                )
            )

    adjacency_lists = {instance.key: [] for instance in instances}
    for instance in instances:
        for dependency in instance.dependencies:
            adjacency_lists[dependency].append(instance.key)

    return TaskInstancePlan(
        instances=tuple(instances),
        adjacency={key: tuple(children) for key, children in adjacency_lists.items()},
    )


def _validate_tasks(tasks: Sequence[ResolvedTask]) -> None:
    task_ids: set[str] = set()
    for task in tasks:
        if not task.id:
            raise TaskPlanningError("task IDs must not be empty")
        if task.id in task_ids:
            raise TaskPlanningError(f"duplicate task ID '{task.id}'")
        task_ids.add(task.id)

    for task in tasks:
        for dependency_id in task.depends_on:
            if dependency_id not in task_ids:
                raise TaskPlanningError(
                    f"task '{task.id}' depends on unknown task ID '{dependency_id}'"
                )


def _validate_execution_targets(execution_targets: Sequence[ExecutionTarget]) -> None:
    target_ids: set[str] = set()
    for target in execution_targets:
        _validate_target(target, label="execution target")
        if target.id in target_ids:
            raise TaskPlanningError(f"duplicate execution target ID '{target.id}'")
        target_ids.add(target.id)


def _validate_target(target: ExecutionTarget, *, label: str) -> None:
    if not target.id:
        raise TaskPlanningError(f"{label} ID must not be empty")
    if not target.regions:
        raise TaskPlanningError(
            f"{label} '{target.id}' must define at least one region"
        )

    regions: set[str] = set()
    for region in target.regions:
        if not region:
            raise TaskPlanningError(
                f"{label} '{target.id}' region names must not be empty"
            )
        if region in regions:
            raise TaskPlanningError(
                f"{label} '{target.id}' has duplicate region '{region}'"
            )
        regions.add(region)


def _expand_task(
    *,
    task: ResolvedTask,
    execution_targets: Sequence[ExecutionTarget],
    configured_target: ExecutionTarget | None,
) -> list[tuple[TaskInstanceKey, ResolvedTask, ExecutionTarget, str]]:
    if task.scope is TaskScope.CONFIGURED_TARGET:
        if configured_target is None:
            raise TaskPlanningError(
                "configured-target tasks require a concrete configured-target identity"
            )
        return [_task_shell(task, configured_target, configured_target.regions[0])]

    if task.scope is TaskScope.TARGET:
        return [
            _task_shell(task, target, target.regions[0]) for target in execution_targets
        ]

    if task.scope is TaskScope.REGION:
        return [
            _task_shell(task, target, region)
            for target in execution_targets
            for region in target.regions
        ]

    raise TaskPlanningError(f"task '{task.id}' has unsupported scope {task.scope!r}")


def _task_shell(
    task: ResolvedTask, target: ExecutionTarget, region: str
) -> tuple[TaskInstanceKey, ResolvedTask, ExecutionTarget, str]:
    return (
        TaskInstanceKey(
            task_id=task.id,
            scope=task.scope,
            execution_target_id=target.id,
            region=region,
        ),
        task,
        target,
        region,
    )


def _dependency_applies(
    *, producer: TaskInstanceKey, consumer: TaskInstanceKey
) -> bool:
    if producer.scope is TaskScope.CONFIGURED_TARGET:
        return True
    if consumer.scope is TaskScope.CONFIGURED_TARGET:
        return True
    if producer.execution_target_id != consumer.execution_target_id:
        return False
    if producer.scope is TaskScope.TARGET:
        return True
    if consumer.scope is TaskScope.TARGET:
        return True
    return producer.region == consumer.region
