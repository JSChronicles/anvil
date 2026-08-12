from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from enum import Enum


class ExecutionStatus(str, Enum):
    SUCCESS = "success"
    ERROR = "error"
    INTERRUPTED = "interrupted"
    SKIPPED = "skipped"

    @property
    def is_success(self) -> bool:
        return self is ExecutionStatus.SUCCESS

    @property
    def is_error(self) -> bool:
        return self is ExecutionStatus.ERROR

    @property
    def is_interrupted(self) -> bool:
        return self is ExecutionStatus.INTERRUPTED

    @property
    def is_skipped(self) -> bool:
        return self is ExecutionStatus.SKIPPED

    @property
    def is_unsuccessful(self) -> bool:
        return self in {ExecutionStatus.ERROR, ExecutionStatus.INTERRUPTED}


def aggregate_execution_statuses(statuses: list[ExecutionStatus]) -> ExecutionStatus:
    """Aggregate execution statuses without treating skipped work as failure.

    Args:
        statuses: Task or child-entity statuses to aggregate.

    Returns:
        Error when any child errored, interrupted when none errored and at least
        one was interrupted, otherwise success.
    """

    if any(status.is_error for status in statuses):
        return ExecutionStatus.ERROR
    if any(status.is_interrupted for status in statuses):
        return ExecutionStatus.INTERRUPTED
    return ExecutionStatus.SUCCESS


class EngineState(str, Enum):
    COMPLETED_SUCCESS = "completed_success"
    COMPLETED_WITH_FAILURES = "completed_with_failures"
    AUTH_FAILED = "auth_failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class TimedResult:
    started_at: str
    ended_at: str
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class TaskResult(TimedResult):
    task_id: str
    task_name: str
    region: str
    status: ExecutionStatus
    result: object | None = None
    error: str | None = None
    skip_reason: str | None = None
    actions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "task_name": self.task_name,
            "region": self.region,
            "status": self.status.value,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_seconds": self.duration_seconds,
            "result": self.result,
            "error": self.error,
            "skip_reason": self.skip_reason,
            "actions": list(self.actions),
        }


@dataclass(frozen=True, slots=True)
class AuthResult(TimedResult):
    target_name: str
    status: ExecutionStatus
    source: str
    message: str | None = None
    remediation: str | None = None

    @property
    def is_success(self) -> bool:
        return self.status.is_success

    @property
    def is_error(self) -> bool:
        return self.status.is_error

    def to_dict(self) -> dict[str, object]:
        return {
            "target": self.target_name,
            "status": self.status.value,
            "source": self.source,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_seconds": self.duration_seconds,
            "message": self.message,
            "remediation": self.remediation,
        }


@dataclass(frozen=True, slots=True)
class EntityResult(TimedResult):
    id: str
    name: str
    type: str
    provider: str
    metadata: dict[str, object]
    status: ExecutionStatus
    tasks: list[TaskResult]
    error: str | None = None
    benchmark: dict[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "provider": self.provider,
            "metadata": dict(self.metadata),
            "status": self.status.value,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_seconds": self.duration_seconds,
            "tasks": [task.to_dict() for task in self.tasks],
            "error": self.error,
        }
        if self.benchmark is not None:
            payload["benchmark"] = self.benchmark

        return payload


@dataclass(frozen=True, slots=True)
class TargetResult:
    target_name: str
    provider: str
    generated_at: str
    dry_run: bool
    entities: list[EntityResult]
    tasks: list[TaskResult] = field(default_factory=list)
    error: str | None = None
    benchmark: dict[str, object] | None = None

    @property
    def total_entities(self) -> int:
        return len(self.entities)

    @property
    def failed_entities(self) -> list[EntityResult]:
        return [result for result in self.entities if result.status.is_error]

    @property
    def interrupted_entities(self) -> list[EntityResult]:
        return [result for result in self.entities if result.status.is_interrupted]

    @property
    def unsuccessful_entities(self) -> list[EntityResult]:
        return [result for result in self.entities if result.status.is_unsuccessful]

    @property
    def has_failures(self) -> bool:
        return (
            self.error is not None
            or any(result.status.is_unsuccessful for result in self.entities)
            or any(result.status.is_unsuccessful for result in self.tasks)
        )

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "target": self.target_name,
            "provider": self.provider,
            "generated_at": self.generated_at,
            "dry_run": self.dry_run,
            "total_entities": self.total_entities,
            "tasks": [task.to_dict() for task in self.tasks],
            "entities": [result.to_dict() for result in self.entities],
            "error": self.error,
        }
        if self.benchmark is not None:
            payload["benchmark"] = self.benchmark

        return payload

    @classmethod
    def create(
        cls,
        *,
        target_name: str,
        provider: str,
        dry_run: bool,
        entities: list[EntityResult],
        tasks: list[TaskResult] | None = None,
        error: str | None = None,
        benchmark: dict[str, object] | None = None,
    ) -> TargetResult:
        return cls(
            target_name=target_name,
            provider=provider,
            generated_at=datetime.datetime.now(datetime.UTC).isoformat(),
            dry_run=dry_run,
            entities=entities,
            tasks=list(tasks or []),
            error=error,
            benchmark=benchmark,
        )


@dataclass(frozen=True, slots=True)
class EngineResult:
    """
    Top-level result container for a full config-driven execution.
    """

    state: EngineState
    generated_at: str
    auth_results: list[AuthResult]
    target_results: list[TargetResult]
    benchmark: dict[str, object] | None = None

    @property
    def has_auth_failures(self) -> bool:
        return any(auth_result.is_error for auth_result in self.auth_results)

    @property
    def has_target_failures(self) -> bool:
        return any(target_result.has_failures for target_result in self.target_results)

    @property
    def total_failed_entities(self) -> int:
        return sum(
            len(target_result.failed_entities) for target_result in self.target_results
        )

    @property
    def total_interrupted_entities(self) -> int:
        return sum(
            len(target_result.interrupted_entities)
            for target_result in self.target_results
        )

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "state": self.state.value,
            "generated_at": self.generated_at,
            "auth": [auth_result.to_dict() for auth_result in self.auth_results],
            "targets": [
                target_result.to_dict() for target_result in self.target_results
            ],
        }
        if self.benchmark is not None:
            payload["benchmark"] = self.benchmark

        return payload

    @classmethod
    def create(
        cls,
        *,
        state: EngineState,
        auth_results: list[AuthResult],
        target_results: list[TargetResult],
        benchmark: dict[str, object] | None = None,
    ) -> EngineResult:
        return cls(
            state=state,
            generated_at=datetime.datetime.now(datetime.UTC).isoformat(),
            auth_results=auth_results,
            target_results=target_results,
            benchmark=benchmark,
        )

    def build_summary(self) -> dict[str, object]:
        """
        Build a high-level summary of the execution suitable for CLI output.
        """
        target_summaries: list[dict[str, object]] = []
        auth_results = [auth_result.to_dict() for auth_result in self.auth_results]
        total_failed_entities = 0
        total_interrupted_entities = 0
        total_failed_tasks = 0
        total_interrupted_tasks = 0
        total_skipped_tasks = 0

        for target_result in self.target_results:
            entities = target_result.entities

            failed_entities = [
                entity_result
                for entity_result in entities
                if entity_result.status.is_error
            ]
            interrupted_entities = [
                entity_result
                for entity_result in entities
                if entity_result.status.is_interrupted
            ]

            tasks = [
                *target_result.tasks,
                *(task for entity_result in entities for task in entity_result.tasks),
            ]
            failed_tasks = sum(1 for task in tasks if task.status.is_error)
            interrupted_tasks = sum(1 for task in tasks if task.status.is_interrupted)
            skipped_tasks = sum(1 for task in tasks if task.status.is_skipped)

            total_failed_entities += len(failed_entities)
            total_interrupted_entities += len(interrupted_entities)
            total_failed_tasks += failed_tasks
            total_interrupted_tasks += interrupted_tasks
            total_skipped_tasks += skipped_tasks

            target_summaries.append(
                {
                    "target": target_result.target_name,
                    "total_entities": target_result.total_entities,
                    "failed_entities": len(failed_entities),
                    "interrupted_entities": len(interrupted_entities),
                    "total_tasks": len(tasks),
                    "failed_tasks": failed_tasks,
                    "interrupted_tasks": interrupted_tasks,
                    "skipped_tasks": skipped_tasks,
                    "has_failures": target_result.has_failures,
                    "error": target_result.error,
                    **(
                        {"benchmark": target_result.benchmark}
                        if target_result.benchmark is not None
                        else {}
                    ),
                }
            )

        payload: dict[str, object] = {
            "state": self.state.value,
            "generated_at": self.generated_at,
            "auth": auth_results,
            "targets": target_summaries,
            "total_failed_entities": total_failed_entities,
            "total_interrupted_entities": total_interrupted_entities,
            "total_failed_tasks": total_failed_tasks,
            "total_interrupted_tasks": total_interrupted_tasks,
            "total_skipped_tasks": total_skipped_tasks,
        }
        if self.benchmark is not None:
            payload["benchmark"] = self.benchmark

        return payload
