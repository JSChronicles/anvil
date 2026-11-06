"""Provider-neutral progress events for Anvil execution."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum


class ProgressPhase(StrEnum):
    """Lifecycle phases displayed for one configured target."""

    QUEUED = "queued"
    PREPARING = "preparing"
    RESOLVING = "resolving"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ProgressTarget:
    """Stable identity and display metadata for one configured target."""

    index: int
    name: str
    provider: str
    provider_display_name: str
    mode: str


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """One immutable progress update emitted by the execution runner."""

    target: ProgressTarget
    phase: ProgressPhase
    completed: int = 0
    total: int | None = None
    duration_seconds: float | None = None


ProgressCallback = Callable[[ProgressEvent], None]


@dataclass(frozen=True, slots=True)
class ProgressReporter:
    """Emit progress events for one configured target."""

    target: ProgressTarget
    callback: ProgressCallback

    def emit(
        self,
        phase: ProgressPhase,
        *,
        completed: int = 0,
        total: int | None = None,
        duration_seconds: float | None = None,
    ) -> None:
        """Send one progress update to the configured observer.

        Args:
            phase: Current target lifecycle phase.
            completed: Number of settled execution targets.
            total: Total resolved execution targets, when known.
            duration_seconds: Final target execution duration, when known.
        """

        self.callback(
            ProgressEvent(
                target=self.target,
                phase=phase,
                completed=completed,
                total=total,
                duration_seconds=duration_seconds,
            )
        )
