from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import pytest

from anvil.descriptors import TargetDescriptor
from anvil.execution_context import ExecutionContext
from anvil.providers.base import ExecutionTarget, ProviderMetadata
from anvil.results import ExecutionStatus
from anvil.runner import _execute_provider_execution_target, _execute_provider_targets
from anvil.task_loader import ResolvedTask, TaskScope


@dataclass(frozen=True, slots=True)
class _ProviderData:
    locations: list[str]


class _Runtime:
    def __init__(self, *, target_id: str, calls: dict[str, object]) -> None:
        self._target_id = target_id
        self._calls = calls

    def build_session(self, *, region: str) -> dict[str, str]:
        build_sessions = self._calls.setdefault("build_sessions", [])
        build_sessions.append((self._target_id, region))
        return {"target_id": self._target_id, "region": region}

    def record_region_outcome(
        self, *, region: str, duration_seconds: float, failed: bool, interrupted: bool
    ) -> None:
        outcomes = self._calls.setdefault("region_outcomes", [])
        outcomes.append((self._target_id, region, failed, interrupted))

    def close(self) -> None:
        closes = self._calls.setdefault("closes", [])
        closes.append(self._target_id)


class _Provider:
    metadata = ProviderMetadata(
        name="azure",
        display_name="Azure",
        supported_task_scopes=frozenset({"region", "target"}),
    )

    def __init__(self, *, calls: dict[str, object]) -> None:
        self._calls = calls

    def prepare_execution_runtime(
        self,
        *,
        target: TargetDescriptor,
        execution_target: ExecutionTarget,
        context: ExecutionContext,
    ) -> _Runtime:
        return _Runtime(target_id=execution_target.id, calls=self._calls)


class _BenchmarkRuntime(_Runtime):
    @property
    def benchmark(self) -> dict[str, object]:
        return {}


class _BenchmarkProvider(_Provider):
    def prepare_execution_runtime(
        self,
        *,
        target: TargetDescriptor,
        execution_target: ExecutionTarget,
        context: ExecutionContext,
    ) -> _BenchmarkRuntime:
        return _BenchmarkRuntime(target_id=execution_target.id, calls=self._calls)


class _TimedLifecycleRuntime(_Runtime):
    def close(self) -> None:
        super().close()
        self._calls["runtime_ended_perf"] = time.perf_counter()


class _TimedLifecycleProvider(_Provider):
    def prepare_execution_runtime(
        self,
        *,
        target: TargetDescriptor,
        execution_target: ExecutionTarget,
        context: ExecutionContext,
    ) -> _TimedLifecycleRuntime:
        self._calls["runtime_started_perf"] = time.perf_counter()
        return _TimedLifecycleRuntime(target_id=execution_target.id, calls=self._calls)


def _target(*, max_workers: int = 1) -> TargetDescriptor:
    return TargetDescriptor(
        name="provider-target",
        provider="azure",
        mode="subscriptions",
        include=["target-a"],
        tasks=[],
        max_workers=max_workers,
    )


def _execution_target(
    target_id: str, *, name: str | None = None, regions: list[str] | None = None
) -> ExecutionTarget:
    return ExecutionTarget(
        id=target_id,
        name=name or target_id,
        type="resource",
        provider="azure",
        regions=regions or ["region-a"],
        provider_data=_ProviderData(locations=regions or ["region-a"]),
    )


def _context(
    *,
    regions: list[str] | None = None,
    tasks: list[ResolvedTask] | None = None,
    fail_fast: bool = False,
    max_parallel_regions: int = 1,
) -> ExecutionContext:
    return ExecutionContext(
        regions=regions or ["region-a"],
        dry_run=False,
        tasks=tasks or [],
        metadata={},
        fail_fast=fail_fast,
        max_parallel_regions=max_parallel_regions,
    )


def _task(
    name: str,
    run,
    *,
    scope: TaskScope = TaskScope.REGION,
    depends_on: list[str] | None = None,
    dependency_data: dict[str, dict[str, str]] | None = None,
    always_run: bool = False,
) -> ResolvedTask:
    return ResolvedTask(
        name=name,
        run=run,
        depends_on=depends_on or [],
        scope=scope,
        dependency_data=dependency_data or {},
        always_run=always_run,
    )


def test_target_task_runs_once_with_first_resolved_region() -> None:
    invocations: list[str] = []

    def run(**kwargs):
        invocations.append(kwargs["region"])
        return {"resources": ["from-all-locations"]}

    calls: dict[str, object] = {}
    context = _context(tasks=[_task("inventory", run, scope=TaskScope.TARGET)])

    result = _execute_provider_execution_target(
        provider=_Provider(calls=calls),
        target=_target(),
        execution_target=_execution_target(
            "target-a", regions=["region-a", "region-b", "region-c"]
        ),
        context=context,
    )

    assert result.status is ExecutionStatus.SUCCESS
    assert invocations == ["region-a"]
    assert [call[1] for call in calls["build_sessions"]] == ["region-a"]
    assert result.tasks[0].region == "region-a"
    assert result.tasks[0].result == {"resources": ["from-all-locations"]}


def test_target_task_preserves_target_benchmark_shape() -> None:
    result = _execute_provider_execution_target(
        provider=_BenchmarkProvider(calls={}),
        target=_target(),
        execution_target=_execution_target(
            "target-a", regions=["region-a", "region-b"]
        ),
        context=_context(
            tasks=[
                _task(
                    "inventory", lambda **kwargs: {"ok": True}, scope=TaskScope.TARGET
                )
            ]
        ),
    )

    assert result.benchmark is not None
    assert result.benchmark["target_execution_seconds"] >= 0.0
    assert result.benchmark["target"] == {
        "region": "region-a",
        "task_count": 1,
        "interrupted": False,
        "failed": False,
    }
    assert result.benchmark["regions"] == {}


def test_mixed_target_and_region_tasks_run_in_separate_phases() -> None:
    invocations: list[tuple[str, str]] = []

    def target_run(**kwargs):
        invocations.append(("target", kwargs["region"]))
        return {"ok": True}

    def region_run(**kwargs):
        invocations.append(("region", kwargs["region"]))
        return {"ok": True}

    context = _context(
        tasks=[
            _task("target", target_run, scope=TaskScope.TARGET),
            _task("region", region_run, depends_on=["target"]),
        ]
    )

    result = _execute_provider_execution_target(
        provider=_Provider(calls={}),
        target=_target(),
        execution_target=_execution_target(
            "target-a", regions=["region-a", "region-b"]
        ),
        context=context,
    )

    assert result.status is ExecutionStatus.SUCCESS
    assert invocations == [
        ("target", "region-a"),
        ("region", "region-a"),
        ("region", "region-b"),
    ]
    assert [(task.region, task.task_name) for task in result.tasks] == [
        ("region-a", "target"),
        ("region-a", "region"),
        ("region-b", "region"),
    ]


def test_region_results_release_target_fan_in_in_configured_region_order() -> None:
    received: list[object] = []

    def regional_run(**kwargs):
        if kwargs["region"] == "region-a":
            time.sleep(0.02)
        return kwargs["region"]

    def target_run(**kwargs):
        received.append(kwargs["dependency_data"]["regions"])
        return {"summarized": True}

    context = _context(
        tasks=[
            _task("regional", regional_run),
            _task(
                "summary",
                target_run,
                scope=TaskScope.TARGET,
                depends_on=["regional"],
                dependency_data={"regions": {"task_id": "regional", "path": "result"}},
            ),
        ],
        max_parallel_regions=2,
    )

    result = _execute_provider_execution_target(
        provider=_BenchmarkProvider(calls={}),
        target=_target(),
        execution_target=_execution_target(
            "target-a", regions=["region-a", "region-b"]
        ),
        context=context,
    )

    assert result.status is ExecutionStatus.SUCCESS
    assert received == [["region-a", "region-b"]]
    assert [(task.task_id, task.region) for task in result.tasks] == [
        ("regional", "region-a"),
        ("regional", "region-b"),
        ("summary", "region-a"),
    ]
    assert result.benchmark is not None
    assert result.benchmark["target"]["task_count"] == 1
    assert {
        region: metrics["task_count"]
        for region, metrics in result.benchmark["regions"].items()
    } == {"region-a": 1, "region-b": 1}


def test_graph_entity_duration_includes_runtime_lifecycle() -> None:
    calls: dict[str, object] = {}
    context = _context(
        tasks=[
            _task("regional", lambda **kwargs: kwargs["region"]),
            _task(
                "summary",
                lambda **kwargs: {"ok": True},
                scope=TaskScope.TARGET,
                depends_on=["regional"],
            ),
        ]
    )

    result = _execute_provider_execution_target(
        provider=_TimedLifecycleProvider(calls=calls),
        target=_target(),
        execution_target=_execution_target("target-a"),
        context=context,
    )

    started_perf = calls["runtime_started_perf"]
    ended_perf = calls["runtime_ended_perf"]
    assert isinstance(started_perf, float)
    assert isinstance(ended_perf, float)
    observed_lifecycle_seconds = ended_perf - started_perf
    assert result.duration_seconds >= observed_lifecycle_seconds
    assert result.started_at <= result.tasks[0].started_at
    assert result.ended_at >= result.tasks[-1].ended_at


def test_task_failure_does_not_suppress_independent_work() -> None:
    independent_regions: list[str] = []

    def target_run(**kwargs):
        raise RuntimeError("optional target failure")

    def dependent_run(**kwargs):
        raise AssertionError("blocked dependency must not run")

    def independent_run(**kwargs):
        independent_regions.append(kwargs["region"])
        return {"ok": True}

    context = _context(
        tasks=[
            _task("target", target_run, scope=TaskScope.TARGET),
            _task("dependent", dependent_run, depends_on=["target"]),
            _task("independent", independent_run),
        ]
    )

    result = _execute_provider_execution_target(
        provider=_Provider(calls={}),
        target=_target(),
        execution_target=_execution_target(
            "target-a", regions=["region-a", "region-b"]
        ),
        context=context,
    )

    assert result.status is ExecutionStatus.ERROR
    assert independent_regions == ["region-a", "region-b"]
    assert [(task.task_name, task.status) for task in result.tasks] == [
        ("target", ExecutionStatus.ERROR),
        ("dependent", ExecutionStatus.SKIPPED),
        ("independent", ExecutionStatus.SUCCESS),
        ("dependent", ExecutionStatus.SKIPPED),
        ("independent", ExecutionStatus.SUCCESS),
    ]


def test_target_failure_with_fail_fast_runs_activated_regional_finalizers() -> None:
    cleanup_regions: list[str] = []

    def fail_target(**kwargs):
        raise RuntimeError("target failed")

    tasks = [
        _task("producer", fail_target, scope=TaskScope.TARGET),
        _task(
            "cleanup",
            lambda **kwargs: cleanup_regions.append(kwargs["region"]),
            depends_on=["producer"],
            always_run=True,
        ),
    ]

    result = _execute_provider_execution_target(
        provider=_Provider(calls={}),
        target=_target(),
        execution_target=_execution_target(
            "target-a", regions=["region-a", "region-b"]
        ),
        context=_context(tasks=tasks, fail_fast=True),
    )

    assert cleanup_regions == ["region-a", "region-b"]
    assert [
        (task.task_id, task.region, task.status, task.skip_reason)
        for task in result.tasks
    ] == [
        ("producer", "region-a", ExecutionStatus.ERROR, None),
        ("cleanup", "region-a", ExecutionStatus.SUCCESS, None),
        ("cleanup", "region-b", ExecutionStatus.SUCCESS, None),
    ]
    assert result.status is ExecutionStatus.ERROR


@pytest.mark.parametrize("stop_kind", ["fail_fast", "cancellation"])
def test_transitive_target_chain_activates_regional_finalizer(stop_kind: str) -> None:
    cleanup_regions: list[str] = []
    context_holder: dict[str, ExecutionContext] = {}

    def producer(**kwargs):
        if stop_kind == "fail_fast":
            raise RuntimeError("target failed")
        context_holder["context"].cancel_event.set()
        return {"started": True}

    tasks = [
        _task("producer", producer, scope=TaskScope.TARGET),
        _task(
            "blocked",
            lambda **kwargs: (_ for _ in ()).throw(
                AssertionError("blocked task must not run")
            ),
            scope=TaskScope.TARGET,
            depends_on=["producer"],
        ),
        _task(
            "cleanup",
            lambda **kwargs: cleanup_regions.append(kwargs["region"]),
            depends_on=["blocked"],
            always_run=True,
        ),
    ]
    context = _context(tasks=tasks, fail_fast=stop_kind == "fail_fast")
    context_holder["context"] = context

    result = _execute_provider_execution_target(
        provider=_Provider(calls={}),
        target=_target(),
        execution_target=_execution_target(
            "target-a", regions=["region-a", "region-b"]
        ),
        context=context,
    )

    assert cleanup_regions == ["region-a", "region-b"]
    assert [(task.task_id, task.status, task.skip_reason) for task in result.tasks] == [
        (
            "producer",
            (
                ExecutionStatus.ERROR
                if stop_kind == "fail_fast"
                else ExecutionStatus.SUCCESS
            ),
            None,
        ),
        (
            "blocked",
            ExecutionStatus.SKIPPED,
            "fail_fast" if stop_kind == "fail_fast" else "cancelled_before_start",
        ),
        ("cleanup", ExecutionStatus.SUCCESS, None),
        ("cleanup", ExecutionStatus.SUCCESS, None),
    ]
    assert result.status is (
        ExecutionStatus.ERROR
        if stop_kind == "fail_fast"
        else ExecutionStatus.INTERRUPTED
    )


def test_target_fail_fast_settles_regions_without_unused_sessions() -> None:
    calls: dict[str, object] = {}
    tasks = [
        _task(
            "producer",
            lambda **kwargs: (_ for _ in ()).throw(RuntimeError("target failed")),
            scope=TaskScope.TARGET,
        ),
        _task("ordinary", lambda **kwargs: {"unexpected": True}),
    ]

    result = _execute_provider_execution_target(
        provider=_Provider(calls=calls),
        target=_target(),
        execution_target=_execution_target(
            "target-a", regions=["region-a", "region-b"]
        ),
        context=_context(tasks=tasks, fail_fast=True),
    )

    assert [
        (task.task_id, task.region, task.status, task.skip_reason)
        for task in result.tasks
    ] == [
        ("producer", "region-a", ExecutionStatus.ERROR, None),
        ("ordinary", "region-a", ExecutionStatus.SKIPPED, "fail_fast"),
        ("ordinary", "region-b", ExecutionStatus.SKIPPED, "fail_fast"),
    ]
    assert calls["build_sessions"] == [("target-a", "region-a")]


@pytest.mark.parametrize("stop_kind", ["fail_fast", "cancellation"])
def test_unactivated_regional_finalizer_does_not_build_unused_sessions(
    stop_kind: str,
) -> None:
    calls: dict[str, object] = {}
    tasks = [
        _task("producer", lambda **kwargs: {"unexpected": True}),
        _task(
            "cleanup",
            lambda **kwargs: {"unexpected": True},
            depends_on=["producer"],
            always_run=True,
        ),
    ]
    context = _context(tasks=tasks, fail_fast=stop_kind == "fail_fast")
    stop_event = (
        context.fail_fast_event if stop_kind == "fail_fast" else context.cancel_event
    )
    stop_event.set()
    skip_reason = "fail_fast" if stop_kind == "fail_fast" else "cancelled_before_start"

    result = _execute_provider_execution_target(
        provider=_Provider(calls=calls),
        target=_target(),
        execution_target=_execution_target(
            "target-a", regions=["region-a", "region-b"]
        ),
        context=context,
    )

    assert [
        (task.task_id, task.region, task.status, task.skip_reason)
        for task in result.tasks
    ] == [
        ("producer", "region-a", ExecutionStatus.SKIPPED, skip_reason),
        ("cleanup", "region-a", ExecutionStatus.SKIPPED, skip_reason),
        ("producer", "region-b", ExecutionStatus.SKIPPED, skip_reason),
        ("cleanup", "region-b", ExecutionStatus.SKIPPED, skip_reason),
    ]
    assert calls.get("build_sessions", []) == []


def test_provider_execution_respects_max_parallel_regions() -> None:
    active_regions = 0
    max_active_regions = 0
    lock = threading.Lock()

    def run(**kwargs):
        nonlocal active_regions, max_active_regions
        with lock:
            active_regions += 1
            max_active_regions = max(max_active_regions, active_regions)
        time.sleep(0.03)
        with lock:
            active_regions -= 1
        return {"region": kwargs["region"]}

    calls: dict[str, object] = {}
    context = _context(tasks=[_task("scan", run)], max_parallel_regions=2)

    result = _execute_provider_execution_target(
        provider=_Provider(calls=calls),
        target=_target(),
        execution_target=_execution_target(
            "target-a", regions=["region-a", "region-b", "region-c"]
        ),
        context=context,
    )

    assert result.status is ExecutionStatus.SUCCESS
    assert max_active_regions == 2
    assert sorted(call[1] for call in calls["region_outcomes"]) == [
        "region-a",
        "region-b",
        "region-c",
    ]


def test_provider_execution_continues_independent_regions_without_fail_fast() -> None:
    def run(**kwargs):
        raise RuntimeError(f"failed {kwargs['region']}")

    calls: dict[str, object] = {}
    context = _context(tasks=[_task("scan", run)], max_parallel_regions=1)

    result = _execute_provider_execution_target(
        provider=_Provider(calls=calls),
        target=_target(),
        execution_target=_execution_target(
            "target-a", regions=["region-a", "region-b"]
        ),
        context=context,
    )

    assert result.status is ExecutionStatus.ERROR
    assert [call[1] for call in calls["build_sessions"]] == ["region-a", "region-b"]
    assert [call[1:] for call in calls["region_outcomes"]] == [
        ("region-a", True, False),
        ("region-b", True, False),
    ]


def test_provider_execution_honors_context_cancel_event() -> None:
    ran = False
    calls: dict[str, object] = {}

    def run(**kwargs):
        nonlocal ran
        ran = True
        return {"ok": True}

    cancelled_context = _context(tasks=[_task("scan", run)])
    cancelled_context.cancel_event.set()

    result = _execute_provider_execution_target(
        provider=_Provider(calls=calls),
        target=_target(),
        execution_target=_execution_target(
            "target-a", regions=["region-a", "region-b"]
        ),
        context=cancelled_context,
    )

    assert result.status is ExecutionStatus.INTERRUPTED
    assert not ran
    assert [call[1] for call in calls["build_sessions"]] == ["region-a"]
    assert [call[1:] for call in calls["region_outcomes"]] == [
        ("region-a", False, True)
    ]


def test_provider_fail_fast_cancels_pending_execution_targets() -> None:
    def run(**kwargs):
        raise RuntimeError("failed")

    context = _context(tasks=[_task("scan", run)], fail_fast=True)
    calls: dict[str, object] = {}

    result = _execute_provider_targets(
        provider=_Provider(calls=calls),
        target=_target(max_workers=1),
        context=context,
        execution_targets=[
            _execution_target("target-a"),
            _execution_target("target-b"),
            _execution_target("target-c"),
        ],
        benchmark_data=None,
    )

    assert context.fail_fast_event.is_set()
    assert not context.cancel_event.is_set()
    assert [entity.id for entity in result.entities] == ["target-a"]
    assert result.entities[0].status is ExecutionStatus.ERROR
    assert [call[0] for call in calls["build_sessions"]] == ["target-a"]


def test_provider_execution_records_each_completed_region_outcome() -> None:
    calls: dict[str, object] = {}
    context = _context(
        tasks=[_task("scan", lambda **kwargs: {"ok": True})], max_parallel_regions=2
    )

    result = _execute_provider_execution_target(
        provider=_Provider(calls=calls),
        target=_target(),
        execution_target=_execution_target(
            "target-a", regions=["region-a", "region-b"]
        ),
        context=context,
    )

    assert result.status is ExecutionStatus.SUCCESS
    assert sorted(call[1:] for call in calls["region_outcomes"]) == [
        ("region-a", False, False),
        ("region-b", False, False),
    ]


def test_provider_result_keeps_region_and_task_order_stable() -> None:
    def first(**kwargs):
        if kwargs["region"] == "region-a":
            time.sleep(0.03)
        return {"task": "first", "region": kwargs["region"]}

    def second(**kwargs):
        return {"task": "second", "region": kwargs["region"]}

    context = _context(
        tasks=[_task("first", first), _task("second", second)], max_parallel_regions=2
    )

    result = _execute_provider_execution_target(
        provider=_Provider(calls={}),
        target=_target(),
        execution_target=_execution_target(
            "target-a", regions=["region-a", "region-b"]
        ),
        context=context,
    )

    assert [(task.region, task.task_name) for task in result.tasks] == [
        ("region-a", "first"),
        ("region-a", "second"),
        ("region-b", "first"),
        ("region-b", "second"),
    ]


def test_provider_regions_isolate_and_persist_task_actions() -> None:
    seen_actions: list[list[str]] = []

    def run(**kwargs):
        seen_actions.append(list(kwargs["actions"].actions))
        kwargs["actions"].record(kwargs["region"])
        return {"region": kwargs["region"]}

    context = _context(tasks=[_task("scan", run)], max_parallel_regions=1)

    result = _execute_provider_execution_target(
        provider=_Provider(calls={}),
        target=_target(),
        execution_target=_execution_target(
            "target-a", regions=["region-a", "region-b"]
        ),
        context=context,
    )

    assert result.status is ExecutionStatus.SUCCESS
    assert seen_actions == [[], []]
    assert [task.actions for task in result.tasks] == [["region-a"], ["region-b"]]


def test_provider_benchmark_records_entity_worker_metrics() -> None:
    context = _context(
        tasks=[_task("scan", lambda **kwargs: {"ok": True})], max_parallel_regions=3
    )
    benchmark_data: dict[str, object] = {}

    result = _execute_provider_targets(
        provider=_Provider(calls={}),
        target=_target(max_workers=2),
        context=context,
        execution_targets=[
            _execution_target("target-b", name="Beta"),
            _execution_target("target-a", name="Alpha"),
        ],
        benchmark_data=benchmark_data,
    )

    assert [entity.id for entity in result.entities] == ["target-a", "target-b"]
    assert result.benchmark is benchmark_data
    assert benchmark_data["submitted_entity_count"] == 2
    assert benchmark_data["completed_entity_count"] == 2
    assert benchmark_data["max_workers"] == 2
    assert benchmark_data["max_parallel_regions"] == 3
    assert benchmark_data["entity_region_limit"] == 6
    assert benchmark_data["entity_execution_window_seconds"] >= 0.0
    assert benchmark_data["sum_entity_duration_seconds"] >= 0.0
    assert benchmark_data["max_entity_duration_seconds"] >= 0.0
    assert benchmark_data["worker_utilization"] >= 0.0
