from __future__ import annotations

from dataclasses import dataclass

from anvil.results import ExecutionStatus


@dataclass(frozen=True, slots=True)
class TaskExecutionResult:
    task_name: str
    status: ExecutionStatus
    started_at: float
    ended_at: float
    result: object | None = None
    error: str | None = None


@dataclass(slots=True)
class ActionRecorder:
    """
    Collects actions performed during task execution.

    Created by the engine and passed into tasks.
    """

    actions: list[str]

    def record(self, message: str) -> None:
        self.actions.append(message)
