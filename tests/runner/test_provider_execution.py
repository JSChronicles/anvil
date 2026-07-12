from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from anvil.descriptors import ConfigBranch, TargetDescriptor
from anvil.execution_context import ExecutionContext
from anvil.providers.base import ExecutionTarget, ProviderMetadata
from anvil.results import ExecutionStatus
from anvil.runner import (
    _execute_provider_execution_target,
    _execute_provider_targets,
)
from anvil.task_loader import ResolvedTask


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
    metadata = ProviderMetadata(name="azure", display_name="Azure")

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


def _target(*, max_workers: int = 1) -> TargetDescriptor:
    return TargetDescriptor(
        config_branch=ConfigBranch.TARGETS,
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
        role_name=None,
        dry_run=False,
        tasks=tasks or [],
        metadata={},
        fail_fast=fail_fast,
        max_parallel_regions=max_parallel_regions,
    )


def _task(name: str, run, *, optional: bool = False) -> ResolvedTask:
    return ResolvedTask(name=name, run=run, depends_on=[], optional=optional)


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
    context = _context(
        tasks=[_task("scan", run)],
        max_parallel_regions=2,
    )

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


def test_provider_execution_stops_launching_regions_after_required_failure() -> None:
    def run(**kwargs):
        raise RuntimeError(f"failed {kwargs['region']}")

    calls: dict[str, object] = {}
    context = _context(
        tasks=[_task("scan", run)],
        max_parallel_regions=1,
    )

    result = _execute_provider_execution_target(
        provider=_Provider(calls=calls),
        target=_target(),
        execution_target=_execution_target(
            "target-a", regions=["region-a", "region-b"]
        ),
        context=context,
    )

    assert result.status is ExecutionStatus.ERROR
    assert [call[1] for call in calls["build_sessions"]] == ["region-a"]
    assert [call[1:] for call in calls["region_outcomes"]] == [
        ("region-a", True, False)
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

    assert context.cancel_event.is_set()
    assert [entity.id for entity in result.entities] == ["target-a"]
    assert result.entities[0].status is ExecutionStatus.ERROR
    assert [call[0] for call in calls["build_sessions"]] == ["target-a"]


def test_provider_execution_records_each_completed_region_outcome() -> None:
    calls: dict[str, object] = {}
    context = _context(
        tasks=[_task("scan", lambda **kwargs: {"ok": True})],
        max_parallel_regions=2,
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
        tasks=[_task("first", first), _task("second", second)],
        max_parallel_regions=2,
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


def test_provider_sequential_regions_share_action_recorder() -> None:
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
    assert seen_actions == [[], ["region-a"]]


def test_provider_benchmark_records_entity_worker_metrics() -> None:
    context = _context(
        tasks=[_task("scan", lambda **kwargs: {"ok": True})],
        max_parallel_regions=3,
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
