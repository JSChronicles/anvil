from __future__ import annotations

import pytest

from anvil.provider_lifecycle import CoordinateLifecycleState
from anvil.results import ExecutionStatus, TaskResult


def _result(status: ExecutionStatus, *, skip_reason: str | None = None) -> TaskResult:
    return TaskResult(
        task_id="task",
        task_name="task",
        region="region-a",
        status=status,
        started_at="2026-01-01T00:00:00+00:00",
        ended_at="2026-01-01T00:00:00+00:00",
        duration_seconds=0.0,
        skip_reason=skip_reason,
    )


def test_coordinate_lifecycle_state_uses_constant_size_aggregation() -> None:
    instance_count = 10_000
    state = CoordinateLifecycleState(remaining_instances=instance_count)
    success = _result(ExecutionStatus.SUCCESS)

    for index in range(instance_count):
        settled = state.record_settlement(
            result=success, region_scoped=True, ended_perf=float(index)
        )

    assert settled is True
    assert state.remaining_instances == 0
    assert state.failed is False
    assert state.interrupted is False
    assert state.region_ended_perf == instance_count - 1
    assert not hasattr(state, "results")


@pytest.mark.parametrize(
    ("result", "failed", "interrupted"),
    [
        (_result(ExecutionStatus.ERROR), True, False),
        (_result(ExecutionStatus.INTERRUPTED), False, True),
        (
            _result(ExecutionStatus.SKIPPED, skip_reason="cancelled_before_start"),
            False,
            True,
        ),
        (
            _result(ExecutionStatus.SKIPPED, skip_reason="dependency_unsuccessful"),
            False,
            False,
        ),
    ],
)
def test_coordinate_lifecycle_state_preserves_outcome_semantics(
    result: TaskResult, failed: bool, interrupted: bool
) -> None:
    state = CoordinateLifecycleState(remaining_instances=1)

    assert state.record_settlement(result=result, region_scoped=False, ended_perf=1.0)
    assert state.failed is failed
    assert state.interrupted is interrupted
