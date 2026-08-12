"""Constant-size provider lifecycle state for task graphs."""

from __future__ import annotations

from dataclasses import dataclass

from anvil.results import TaskResult


@dataclass(slots=True)
class CoordinateLifecycleState:
    """Constant-size settlement and timing state for one runtime coordinate."""

    remaining_instances: int = 0
    failed: bool = False
    interrupted: bool = False
    started_perf: float | None = None
    region_started_perf: float | None = None
    region_ended_perf: float | None = None

    def record_settlement(
        self, *, result: TaskResult, region_scoped: bool, ended_perf: float
    ) -> bool:
        """Record one terminal result without retaining the result object.

        Args:
            result: Terminal result for one task instance.
            region_scoped: Whether the instance contributes to regional timing.
            ended_perf: Monotonic settlement timestamp.

        Returns:
            Whether every task instance for this coordinate has settled.

        Raises:
            RuntimeError: If more results settle than were planned.
        """

        if self.remaining_instances <= 0:
            raise RuntimeError(
                "Provider coordinate settled more task instances than planned"
            )
        self.failed = self.failed or result.status.is_error
        self.interrupted = self.interrupted or (
            result.status.is_interrupted
            or (
                result.status.is_skipped
                and result.skip_reason == "cancelled_before_start"
            )
        )
        self.remaining_instances -= 1
        if region_scoped:
            self.region_ended_perf = ended_perf
        return self.remaining_instances == 0
