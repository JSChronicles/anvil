from __future__ import annotations

from dataclasses import dataclass, fields

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

    @classmethod
    def keyword_names(cls) -> frozenset[str]:
        """Return the canonical task invocation keyword names."""

        return frozenset(field.name for field in fields(cls))

    def to_kwargs(self) -> dict[str, object]:
        """Return provider-neutral task keyword arguments."""

        invocation_kwargs = {
            field.name: getattr(self, field.name) for field in fields(self)
        }
        invocation_kwargs["metadata"] = dict(self.metadata)
        return invocation_kwargs
