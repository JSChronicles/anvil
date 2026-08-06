from __future__ import annotations

import datetime
import importlib
import threading
import time
from dataclasses import replace

import pytest

from anvil.providers.base import ExecutionTarget
from anvil.results import ExecutionStatus, TaskResult
from anvil.task_loader import ResolvedTask, TaskScope
from anvil.task_planner import plan_task_instances


def _task(
    task_id: str,
    scope: TaskScope,
    *,
    depends_on: list[str] | None = None,
    always_run: bool = False,
) -> ResolvedTask:
    return ResolvedTask(
        id=task_id,
        name=task_id,
        run=lambda **kwargs: None,
        depends_on=depends_on or [],
        always_run=always_run,
        scope=scope,
    )


def _target(target_id: str, regions: list[str]) -> ExecutionTarget:
    return ExecutionTarget(
        id=target_id, name=target_id, type="resource", provider="fake", regions=regions
    )


def _result(
    instance,
    status: ExecutionStatus = ExecutionStatus.SUCCESS,
    *,
    error: str | None = None,
) -> TaskResult:
    now_at = datetime.datetime.now(datetime.UTC).isoformat()
    return TaskResult(
        task_id=instance.task.id,
        task_name=instance.task.name,
        region=instance.region,
        status=status,
        started_at=now_at,
        ended_at=now_at,
        duration_seconds=0.0,
        error=error,
    )


def _scheduler_api():
    module = importlib.import_module("anvil.task_scheduler")
    scheduler = getattr(module, "execute_task_instance_plan", None)
    assert callable(scheduler)
    return scheduler


def test_scheduler_releases_fan_in_barrier_only_after_every_region_settles() -> None:
    scheduler = _scheduler_api()
    plan = plan_task_instances(
        tasks=[
            _task("regional", TaskScope.REGION),
            _task("summary", TaskScope.TARGET, depends_on=["regional"]),
        ],
        execution_targets=[_target("target", ["region-a", "region-b"])],
        configured_target=None,
    )
    completed_regions: list[str] = []
    summary_inputs: list[list[str]] = []

    def execute(instance, dependency_results):
        if instance.task.id == "regional":
            if instance.region == "region-a":
                time.sleep(0.02)
            completed_regions.append(instance.region)
        else:
            summary_inputs.append(
                [result.region for result in dependency_results["regional"]]
            )
            assert set(completed_regions) == {"region-a", "region-b"}
        return _result(instance)

    scheduler(
        plan=plan,
        execute=execute,
        max_workers=2,
        cancel_event=threading.Event(),
        fail_fast=False,
    )

    assert summary_inputs == [["region-a", "region-b"]]


def test_dependency_preparation_reads_each_fan_in_result_once() -> None:
    scheduler_module = importlib.import_module("anvil.task_scheduler")
    prepare_dependency_results = getattr(
        scheduler_module, "_prepare_dependency_results", None
    )
    assert callable(prepare_dependency_results)

    plan = plan_task_instances(
        tasks=[
            _task("regional", TaskScope.REGION),
            _task("summary", TaskScope.TARGET, depends_on=["regional"]),
        ],
        execution_targets=[_target("target", ["region-a", "region-b", "region-c"])],
        configured_target=None,
    )
    instances_by_key = {instance.key: instance for instance in plan.instances}
    summary = next(
        instance for instance in plan.instances if instance.task.id == "summary"
    )

    class CountingResults(dict):
        lookup_count = 0

        def get(self, key, default=None):
            self.lookup_count += 1
            return super().get(key, default)

    results_by_key = CountingResults(
        {
            dependency: _result(instances_by_key[dependency])
            for dependency in summary.dependencies
        }
    )
    eligibility, grouped = prepare_dependency_results(
        instance=summary,
        results_by_key=results_by_key,
        activated_keys=set(summary.dependencies),
        stop_reason=None,
    )

    assert eligibility.should_run
    assert results_by_key.lookup_count == len(summary.dependencies)
    assert [result.region for result in grouped["regional"]] == [
        "region-a",
        "region-b",
        "region-c",
    ]


def test_scheduler_bounds_concurrency_and_serializes_one_region_coordinate() -> None:
    scheduler = _scheduler_api()
    plan = plan_task_instances(
        tasks=[_task("first", TaskScope.REGION), _task("second", TaskScope.REGION)],
        execution_targets=[_target("target", ["region-a", "region-b", "region-c"])],
        configured_target=None,
    )
    active = 0
    maximum_active = 0
    active_coordinates: set[tuple[str, str]] = set()
    lock = threading.Lock()

    def execute(instance, dependency_results):
        nonlocal active, maximum_active
        coordinate = (instance.key.execution_target_id, instance.region)
        with lock:
            assert coordinate not in active_coordinates
            active_coordinates.add(coordinate)
            active += 1
            maximum_active = max(maximum_active, active)
        time.sleep(0.01)
        with lock:
            active -= 1
            active_coordinates.remove(coordinate)
        return _result(instance)

    scheduler(
        plan=plan,
        execute=execute,
        max_workers=2,
        cancel_event=threading.Event(),
        fail_fast=False,
    )

    assert maximum_active == 2


def test_scheduler_releases_large_chain_through_plan_adjacency() -> None:
    scheduler = _scheduler_api()
    instance_count = 500
    tasks = [
        _task(
            f"task-{index}",
            TaskScope.REGION,
            depends_on=[f"task-{index - 1}"] if index else None,
        )
        for index in range(instance_count)
    ]
    plan = plan_task_instances(
        tasks=tasks,
        execution_targets=[_target("target", ["region"])],
        configured_target=None,
    )

    class CountingAdjacency(dict):
        lookup_count = 0

        def __getitem__(self, key):
            self.lookup_count += 1
            return super().__getitem__(key)

    adjacency = CountingAdjacency(plan.adjacency)
    plan = replace(plan, adjacency=adjacency)
    schedule = scheduler(
        plan=plan,
        execute=lambda instance, dependencies: _result(instance),
        max_workers=1,
        cancel_event=threading.Event(),
        fail_fast=False,
    )

    assert len(schedule.results) == instance_count
    assert adjacency.lookup_count == instance_count


def test_fail_fast_settles_unstarted_nodes_and_runs_activated_finalizer() -> None:
    scheduler = _scheduler_api()
    plan = plan_task_instances(
        tasks=[
            _task("scan", TaskScope.REGION),
            _task(
                "cleanup",
                TaskScope.CONFIGURED_TARGET,
                depends_on=["scan"],
                always_run=True,
            ),
        ],
        execution_targets=[_target("target", ["region-a", "region-b", "region-c"])],
        configured_target=_target("configured", ["home-region"]),
    )
    cleanup_inputs: list[list[str]] = []

    def execute(instance, dependency_results):
        if instance.task.id == "scan":
            return _result(instance, ExecutionStatus.ERROR, error="regional failure")
        cleanup_inputs.append(
            [result.status.value for result in dependency_results["scan"]]
        )
        return _result(instance)

    schedule = scheduler(
        plan=plan,
        execute=execute,
        max_workers=1,
        cancel_event=threading.Event(),
        fail_fast=True,
    )

    assert [
        (item.result.status.value, item.result.skip_reason) for item in schedule.results
    ] == [
        ("error", None),
        ("skipped", "fail_fast"),
        ("skipped", "fail_fast"),
        ("success", None),
    ]
    assert cleanup_inputs == [["error", "skipped", "skipped"]]


def test_cancellation_before_chain_start_does_not_activate_finalizer() -> None:
    scheduler = _scheduler_api()
    plan = plan_task_instances(
        tasks=[
            _task("work", TaskScope.REGION),
            _task("cleanup", TaskScope.REGION, depends_on=["work"], always_run=True),
        ],
        execution_targets=[_target("target", ["region"])],
        configured_target=None,
    )
    cancel_event = threading.Event()
    cancel_event.set()

    schedule = scheduler(
        plan=plan,
        execute=lambda instance, dependencies: _result(instance),
        max_workers=1,
        cancel_event=cancel_event,
        fail_fast=False,
    )

    assert [
        (item.result.status.value, item.result.skip_reason) for item in schedule.results
    ] == [("skipped", "cancelled_before_start"), ("skipped", "cancelled_before_start")]


def test_graceful_cancellation_runs_finalizer_for_started_chain() -> None:
    scheduler = _scheduler_api()
    plan = plan_task_instances(
        tasks=[
            _task("work", TaskScope.REGION),
            _task("pending", TaskScope.REGION),
            _task("cleanup", TaskScope.REGION, depends_on=["work"], always_run=True),
        ],
        execution_targets=[_target("target", ["region"])],
        configured_target=None,
    )
    cancel_event = threading.Event()
    calls: list[str] = []

    def execute(instance, dependency_results):
        calls.append(instance.task.id)
        if instance.task.id == "work":
            cancel_event.set()
        return _result(instance)

    schedule = scheduler(
        plan=plan,
        execute=execute,
        max_workers=1,
        cancel_event=cancel_event,
        fail_fast=False,
    )

    assert calls == ["work", "cleanup"]
    assert [
        (item.result.status.value, item.result.skip_reason) for item in schedule.results
    ] == [("success", None), ("skipped", "cancelled_before_start"), ("success", None)]


def test_fail_fast_runs_finalizer_after_transitively_skipped_dependency() -> None:
    scheduler = _scheduler_api()
    plan = plan_task_instances(
        tasks=[
            _task("producer", TaskScope.REGION),
            _task("blocked", TaskScope.REGION, depends_on=["producer"]),
            _task("cleanup", TaskScope.REGION, depends_on=["blocked"], always_run=True),
        ],
        execution_targets=[_target("target", ["region"])],
        configured_target=None,
    )
    calls: list[str] = []

    def execute(instance, dependency_results):
        calls.append(instance.task.id)
        if instance.task.id == "producer":
            return _result(instance, ExecutionStatus.ERROR, error="producer failed")
        return _result(instance)

    schedule = scheduler(
        plan=plan,
        execute=execute,
        max_workers=1,
        cancel_event=threading.Event(),
        fail_fast=True,
    )

    assert calls == ["producer", "cleanup"]
    assert [
        (item.result.status.value, item.result.skip_reason) for item in schedule.results
    ] == [("error", None), ("skipped", "fail_fast"), ("success", None)]


def test_cancellation_runs_finalizer_after_transitively_skipped_dependency() -> None:
    scheduler = _scheduler_api()
    plan = plan_task_instances(
        tasks=[
            _task("producer", TaskScope.REGION),
            _task("blocked", TaskScope.REGION, depends_on=["producer"]),
            _task("cleanup", TaskScope.REGION, depends_on=["blocked"], always_run=True),
        ],
        execution_targets=[_target("target", ["region"])],
        configured_target=None,
    )
    cancel_event = threading.Event()
    calls: list[str] = []

    def execute(instance, dependency_results):
        calls.append(instance.task.id)
        if instance.task.id == "producer":
            cancel_event.set()
        return _result(instance)

    schedule = scheduler(
        plan=plan,
        execute=execute,
        max_workers=1,
        cancel_event=cancel_event,
        fail_fast=False,
    )

    assert calls == ["producer", "cleanup"]
    assert [
        (item.result.status.value, item.result.skip_reason) for item in schedule.results
    ] == [("success", None), ("skipped", "cancelled_before_start"), ("success", None)]


@pytest.mark.parametrize(
    ("producer_scope", "finalizer_scope"),
    [
        (TaskScope.REGION, TaskScope.TARGET),
        (TaskScope.TARGET, TaskScope.REGION),
        (TaskScope.REGION, TaskScope.CONFIGURED_TARGET),
        (TaskScope.CONFIGURED_TARGET, TaskScope.REGION),
        (TaskScope.TARGET, TaskScope.CONFIGURED_TARGET),
        (TaskScope.CONFIGURED_TARGET, TaskScope.TARGET),
    ],
)
@pytest.mark.parametrize("run_state", ["success", "error", "fail_fast", "cancellation"])
def test_finalizer_state_matrix_across_scope_boundaries(
    producer_scope: TaskScope, finalizer_scope: TaskScope, run_state: str
) -> None:
    scheduler = _scheduler_api()
    plan = plan_task_instances(
        tasks=[
            _task("producer", producer_scope),
            _task(
                "finalizer", finalizer_scope, depends_on=["producer"], always_run=True
            ),
        ],
        execution_targets=[_target("target", ["region-a", "region-b"])],
        configured_target=_target("configured", ["home-region"]),
    )
    cancel_event = threading.Event()
    finalizer_calls: list[tuple[str, str]] = []

    def execute(instance, dependency_results):
        if instance.task.id == "producer":
            if run_state == "cancellation":
                cancel_event.set()
            if run_state in {"error", "fail_fast"}:
                return _result(instance, ExecutionStatus.ERROR, error="producer failed")
        else:
            finalizer_calls.append((instance.key.execution_target_id, instance.region))
        return _result(instance)

    schedule = scheduler(
        plan=plan,
        execute=execute,
        max_workers=1,
        cancel_event=cancel_event,
        fail_fast=run_state == "fail_fast",
    )

    expected_finalizer_count = 2 if finalizer_scope is TaskScope.REGION else 1
    assert len(finalizer_calls) == expected_finalizer_count
    assert all(
        item.result.status is ExecutionStatus.SUCCESS
        for item in schedule.results
        if item.key.task_id == "finalizer"
    )


def test_executor_exception_becomes_error_and_blocks_normal_dependent() -> None:
    scheduler = _scheduler_api()
    plan = plan_task_instances(
        tasks=[
            _task("runtime", TaskScope.REGION),
            _task("consumer", TaskScope.REGION, depends_on=["runtime"]),
        ],
        execution_targets=[_target("target", ["region"])],
        configured_target=None,
    )

    def execute(instance, dependency_results):
        raise RuntimeError("runtime construction failed")

    schedule = scheduler(
        plan=plan,
        execute=execute,
        max_workers=1,
        cancel_event=threading.Event(),
        fail_fast=False,
    )

    assert [
        (item.result.status.value, item.result.error, item.result.skip_reason)
        for item in schedule.results
    ] == [
        ("error", "runtime construction failed", None),
        ("skipped", None, "dependency_unsuccessful"),
    ]


def test_output_order_follows_plan_not_completion_order() -> None:
    scheduler = _scheduler_api()
    plan = plan_task_instances(
        tasks=[_task("scan", TaskScope.REGION)],
        execution_targets=[
            _target("target-b", ["region-b2", "region-b1"]),
            _target("target-a", ["region-a1"]),
        ],
        configured_target=None,
    )

    def execute(instance, dependency_results):
        if instance.region == "region-b2":
            time.sleep(0.03)
        return _result(instance)

    schedule = scheduler(
        plan=plan,
        execute=execute,
        max_workers=3,
        cancel_event=threading.Event(),
        fail_fast=False,
    )

    assert [
        (item.key.execution_target_id, item.key.region) for item in schedule.results
    ] == [
        ("target-b", "region-b2"),
        ("target-b", "region-b1"),
        ("target-a", "region-a1"),
    ]
