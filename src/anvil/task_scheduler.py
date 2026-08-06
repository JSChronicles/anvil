"""Bounded scheduling and settlement for precomputed task-instance graphs."""

from __future__ import annotations

import datetime
import heapq
import threading
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass

from anvil.results import ExecutionStatus, TaskResult
from anvil.task_errors import TaskExecutionError
from anvil.task_loader import ResolvedTask
from anvil.task_planner import TaskInstance, TaskInstanceKey, TaskInstancePlan

DependencyResults = Mapping[str, TaskResult | tuple[TaskResult, ...]]
TaskInstanceExecutor = Callable[[TaskInstance, DependencyResults], TaskResult]
TaskInstanceSettled = Callable[[TaskInstance, TaskResult], None]


@dataclass(frozen=True, slots=True)
class TaskInstanceEligibility:
    """Shared execution and finalizer-activation decision."""

    should_run: bool
    skip_reason: str | None = None
    chain_activated: bool = False


@dataclass(frozen=True, slots=True)
class ScheduledTaskResult:
    """Terminal result associated with its planned invocation identity."""

    key: TaskInstanceKey
    result: TaskResult


@dataclass(frozen=True, slots=True)
class TaskInstanceSchedule:
    """Terminal task results in deterministic plan order."""

    results: tuple[ScheduledTaskResult, ...]


def task_instance_eligibility(
    *,
    instance: TaskInstance,
    dependency_results: Mapping[TaskInstanceKey, TaskResult],
    activated_keys: set[TaskInstanceKey],
    stop_reason: str | None,
) -> TaskInstanceEligibility:
    """Decide whether one ready instance runs or settles as skipped.

    Args:
        instance: Ready planned invocation.
        dependency_results: Terminal direct dependency results.
        activated_keys: Instances whose dependency chains began.
        stop_reason: Scheduler stop reason, if ordinary work must not start.

    Returns:
        The shared execution and finalizer-activation decision.
    """

    missing_dependencies = [
        key for key in instance.dependencies if key not in dependency_results
    ]
    if missing_dependencies:
        missing = ", ".join(key.task_id for key in missing_dependencies)
        raise RuntimeError(
            f"Task instance '{instance.key.task_id}' dependencies have not "
            f"settled: {missing}"
        )

    chain_activated = not instance.dependencies or any(
        key in activated_keys for key in instance.dependencies
    )
    return task_dependency_eligibility(
        task=instance.task,
        dependency_results=tuple(dependency_results.values()),
        chain_activated=chain_activated,
        stop_reason=stop_reason,
    )


def task_dependency_eligibility(
    *,
    task: ResolvedTask,
    dependency_results: Sequence[TaskResult],
    chain_activated: bool,
    stop_reason: str | None,
) -> TaskInstanceEligibility:
    """Apply the one dependency gate used by schedulers and finalizers.

    Args:
        task: Resolved task declaration.
        dependency_results: Terminal results of every direct dependency.
        chain_activated: Whether this dependency chain began execution.
        stop_reason: Scheduler stop reason, if ordinary work must not start.

    Returns:
        The execution and activation decision.
    """

    if task.always_run:
        if chain_activated:
            return TaskInstanceEligibility(should_run=True, chain_activated=True)
        return TaskInstanceEligibility(
            should_run=False, skip_reason=stop_reason or "cancelled_before_start"
        )

    if stop_reason is not None:
        return TaskInstanceEligibility(
            should_run=False,
            skip_reason=stop_reason,
            chain_activated=chain_activated and bool(dependency_results),
        )

    if any(
        result.status.is_unsuccessful or result.status.is_skipped
        for result in dependency_results
    ):
        return TaskInstanceEligibility(
            should_run=False,
            skip_reason="dependency_unsuccessful",
            chain_activated=chain_activated,
        )

    return TaskInstanceEligibility(should_run=True, chain_activated=True)


def execute_task_instance_plan(
    *,
    plan: TaskInstancePlan,
    execute: TaskInstanceExecutor,
    max_workers: int,
    max_active_execution_targets: int | None = None,
    max_active_coordinates_per_execution_target: int | None = None,
    cancel_event: threading.Event,
    fail_fast: bool,
    external_fail_fast_event: threading.Event | None = None,
    on_instance_settled: TaskInstanceSettled | None = None,
) -> TaskInstanceSchedule:
    """Execute and settle a precomputed task-instance graph.

    Independent coordinates run concurrently up to ``max_workers``. Invocations
    sharing a target-region coordinate remain serial, preserving the existing
    per-region task execution boundary.

    Args:
        plan: Deterministic task-instance graph.
        execute: Provider-neutral callback for one admitted invocation.
        max_workers: Maximum concurrently running coordinates.
        max_active_execution_targets: Optional cap on distinct concurrently active
            execution-target identities.
        max_active_coordinates_per_execution_target: Optional cap on concurrently
            active target-region coordinates for one execution-target identity.
        cancel_event: Graceful cancellation signal.
        fail_fast: Whether the first unsuccessful invocation stops ordinary work.
        external_fail_fast_event: Optional signal that another execution target
            triggered fail-fast.
        on_instance_settled: Optional callback invoked as each result settles.

    Returns:
        Every planned invocation settled in plan order.

    Raises:
        ValueError: If ``max_workers`` is not positive.
        RuntimeError: If the supplied graph cannot make progress.
    """

    if max_workers <= 0:
        raise ValueError("max_workers must be greater than zero")
    if max_active_execution_targets is not None and max_active_execution_targets <= 0:
        raise ValueError("max_active_execution_targets must be greater than zero")
    if (
        max_active_coordinates_per_execution_target is not None
        and max_active_coordinates_per_execution_target <= 0
    ):
        raise ValueError(
            "max_active_coordinates_per_execution_target must be greater than zero"
        )

    instances_by_key = {instance.key: instance for instance in plan.instances}
    plan_order = {instance.key: index for index, instance in enumerate(plan.instances)}
    remaining_dependencies = {
        instance.key: len(instance.dependencies) for instance in plan.instances
    }
    results_by_key: dict[TaskInstanceKey, TaskResult] = {}
    activated_keys: set[TaskInstanceKey] = set()
    ready_by_coordinate: dict[tuple[str, str], list[tuple[int, TaskInstanceKey]]] = {}
    available_coordinates: list[tuple[int, str, str]] = []
    active_coordinates: set[tuple[str, str]] = set()
    active_futures: dict[Future[TaskResult], tuple[TaskInstance, tuple[str, str]]] = {}
    active_target_counts: dict[str, int] = {}
    fail_fast_triggered = False

    def enqueue_ready(key: TaskInstanceKey) -> None:
        coordinate = (key.execution_target_id, key.region)
        coordinate_ready = ready_by_coordinate.setdefault(coordinate, [])
        heapq.heappush(coordinate_ready, (plan_order[key], key))
        if coordinate not in active_coordinates:
            heapq.heappush(
                available_coordinates,
                (coordinate_ready[0][0], coordinate[0], coordinate[1]),
            )

    def settle(instance: TaskInstance, result: TaskResult) -> None:
        nonlocal fail_fast_triggered
        results_by_key[instance.key] = result
        if on_instance_settled is not None:
            on_instance_settled(instance, result)
        if fail_fast and result.status.is_unsuccessful:
            fail_fast_triggered = True
        for child_key in plan.adjacency[instance.key]:
            remaining_dependencies[child_key] -= 1
            if remaining_dependencies[child_key] == 0:
                enqueue_ready(child_key)

    for instance in plan.instances:
        if remaining_dependencies[instance.key] == 0:
            enqueue_ready(instance.key)

    with ThreadPoolExecutor(
        max_workers=min(max_workers, max(1, len(plan.instances)))
    ) as executor:
        while len(results_by_key) < len(plan.instances):
            deferred_coordinates: list[tuple[int, str, str]] = []
            while available_coordinates and len(active_futures) < max_workers:
                ready_index, target_id, region = heapq.heappop(available_coordinates)
                coordinate = (target_id, region)
                coordinate_ready = ready_by_coordinate.get(coordinate)
                if (
                    coordinate in active_coordinates
                    or not coordinate_ready
                    or coordinate_ready[0][0] != ready_index
                ):
                    continue
                if (
                    max_active_execution_targets is not None
                    and target_id not in active_target_counts
                    and len(active_target_counts) >= max_active_execution_targets
                ) or (
                    max_active_coordinates_per_execution_target is not None
                    and active_target_counts.get(target_id, 0)
                    >= max_active_coordinates_per_execution_target
                ):
                    deferred_coordinates.append((ready_index, target_id, region))
                    continue

                _, key = heapq.heappop(coordinate_ready)
                instance = instances_by_key[key]

                direct_results = {
                    dependency: results_by_key[dependency]
                    for dependency in instance.dependencies
                }
                stop_reason = (
                    "fail_fast"
                    if fail_fast_triggered
                    or (
                        external_fail_fast_event is not None
                        and external_fail_fast_event.is_set()
                    )
                    else ("cancelled_before_start" if cancel_event.is_set() else None)
                )
                eligibility = task_instance_eligibility(
                    instance=instance,
                    dependency_results=direct_results,
                    activated_keys=activated_keys,
                    stop_reason=stop_reason,
                )

                if not eligibility.should_run:
                    skipped_result = _skipped_result(
                        instance=instance,
                        skip_reason=eligibility.skip_reason
                        or "dependency_unsuccessful",
                    )
                    if eligibility.chain_activated:
                        activated_keys.add(key)
                    settle(instance, skipped_result)
                    if coordinate_ready:
                        heapq.heappush(
                            available_coordinates,
                            (coordinate_ready[0][0], target_id, region),
                        )
                    continue

                activated_keys.add(key)
                future = executor.submit(
                    _execute_safely,
                    instance=instance,
                    dependency_results=_group_dependency_results(
                        instance=instance, results_by_key=results_by_key
                    ),
                    execute=execute,
                )
                active_futures[future] = (instance, coordinate)
                active_coordinates.add(coordinate)
                active_target_counts[target_id] = (
                    active_target_counts.get(target_id, 0) + 1
                )

            for deferred_coordinate in deferred_coordinates:
                heapq.heappush(available_coordinates, deferred_coordinate)

            if len(results_by_key) == len(plan.instances):
                break

            if active_futures:
                completed, _ = wait(active_futures, return_when=FIRST_COMPLETED)
                for future in sorted(
                    completed, key=lambda item: plan_order[active_futures[item][0].key]
                ):
                    instance, coordinate = active_futures.pop(future)
                    active_coordinates.remove(coordinate)
                    active_target_counts[instance.key.execution_target_id] -= 1
                    if active_target_counts[instance.key.execution_target_id] == 0:
                        del active_target_counts[instance.key.execution_target_id]
                    result = future.result()
                    settle(instance, result)
                    coordinate_ready = ready_by_coordinate.get(coordinate)
                    if coordinate_ready:
                        heapq.heappush(
                            available_coordinates,
                            (coordinate_ready[0][0], coordinate[0], coordinate[1]),
                        )
                continue

            pending = ", ".join(
                instance.key.task_id
                for instance in plan.instances
                if instance.key not in results_by_key
            )
            raise RuntimeError(
                f"Task instance graph stalled with unsettled nodes: {pending}"
            )

    ordered_results = tuple(
        ScheduledTaskResult(key=instance.key, result=results_by_key[instance.key])
        for instance in plan.instances
    )
    return TaskInstanceSchedule(results=ordered_results)


def _group_dependency_results(
    *, instance: TaskInstance, results_by_key: Mapping[TaskInstanceKey, TaskResult]
) -> dict[str, TaskResult | tuple[TaskResult, ...]]:
    grouped: dict[str, list[TaskResult]] = {}
    for dependency in instance.dependencies:
        grouped.setdefault(dependency.task_id, []).append(results_by_key[dependency])

    return {
        task_id: values[0] if len(values) == 1 else tuple(values)
        for task_id, values in grouped.items()
    }


def _execute_safely(
    *,
    instance: TaskInstance,
    dependency_results: DependencyResults,
    execute: TaskInstanceExecutor,
) -> TaskResult:
    try:
        return execute(instance, dependency_results)
    except Exception as error:
        now_at = datetime.datetime.now(datetime.UTC).isoformat()
        return TaskResult(
            task_id=instance.task.id,
            task_name=instance.task.name,
            region=instance.region,
            status=ExecutionStatus.ERROR,
            started_at=now_at,
            ended_at=now_at,
            duration_seconds=0.0,
            result=(
                error.partial_result if isinstance(error, TaskExecutionError) else None
            ),
            error=str(error),
        )


def _skipped_result(*, instance: TaskInstance, skip_reason: str) -> TaskResult:
    now_at = datetime.datetime.now(datetime.UTC).isoformat()
    return TaskResult(
        task_id=instance.task.id,
        task_name=instance.task.name,
        region=instance.region,
        status=ExecutionStatus.SKIPPED,
        started_at=now_at,
        ended_at=now_at,
        duration_seconds=0.0,
        skip_reason=skip_reason,
    )
