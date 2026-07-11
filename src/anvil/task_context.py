from __future__ import annotations

from dataclasses import dataclass

from anvil.actions import ActionRecorder


@dataclass(frozen=True, slots=True)
class TaskCallContext:
    """Provider-neutral task invocation context."""

    provider: str
    execution_target_id: str
    execution_target_name: str
    execution_target_type: str
    region: str
    session: object
    dry_run: bool
    metadata: dict[str, object]
    actions: ActionRecorder

    def to_kwargs(self) -> dict[str, object]:
        """Return provider-neutral task keyword arguments."""

        return {
            "provider": self.provider,
            "execution_target_id": self.execution_target_id,
            "execution_target_name": self.execution_target_name,
            "execution_target_type": self.execution_target_type,
            "region": self.region,
            "session": self.session,
            "dry_run": self.dry_run,
            "metadata": self.metadata,
            "actions": self.actions,
            "task_context": self,
        }
