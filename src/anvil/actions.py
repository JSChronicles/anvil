from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class ActionRecorder:
    """
    Collects actions performed during task execution.

    Created by the engine and passed into tasks.
    """

    actions: list[str]

    def record(self, message: str) -> None:
        self.actions.append(message)
