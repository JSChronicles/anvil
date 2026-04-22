from __future__ import annotations

import datetime
from dataclasses import dataclass
from enum import Enum

from anvil.descriptors import ConfigBranch


def _result_labels(config_branch: ConfigBranch) -> tuple[str, str]:
    if config_branch is ConfigBranch.ACCOUNTS:
        return "account_group", "account_groups"

    return "organization", "organizations"


class ExecutionStatus(str, Enum):
    SUCCESS = "success"
    ERROR = "error"
    INTERRUPTED = "interrupted"

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
    def is_unsuccessful(self) -> bool:
        return self is not ExecutionStatus.SUCCESS


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
    task_name: str
    region: str
    status: ExecutionStatus
    result: object | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "task": self.task_name,
            "region": self.region,
            "status": self.status.value,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_seconds": self.duration_seconds,
            "result": self.result,
            "error": self.error,
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

    def to_dict(self, *, config_branch: ConfigBranch) -> dict[str, object]:
        singular_key, _ = _result_labels(config_branch)

        return {
            singular_key: self.target_name,
            "status": self.status.value,
            "source": self.source,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_seconds": self.duration_seconds,
            "message": self.message,
            "remediation": self.remediation,
        }


@dataclass(frozen=True, slots=True)
class AccountResult(TimedResult):
    account_id: str
    account_alias: str
    status: ExecutionStatus
    tasks: list[TaskResult]
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "account_alias": self.account_alias,
            "status": self.status.value,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_seconds": self.duration_seconds,
            "tasks": [task.to_dict() for task in self.tasks],
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class TargetResult:
    config_branch: ConfigBranch
    target_name: str
    generated_at: str
    dry_run: bool
    account_results: list[AccountResult]
    error: str | None = None

    @property
    def total_accounts(self) -> int:
        return len(self.account_results)

    @property
    def failed_accounts(self) -> list[AccountResult]:
        return [result for result in self.account_results if result.status.is_error]

    @property
    def interrupted_accounts(self) -> list[AccountResult]:
        return [
            result for result in self.account_results if result.status.is_interrupted
        ]

    @property
    def unsuccessful_accounts(self) -> list[AccountResult]:
        return [
            result for result in self.account_results if result.status.is_unsuccessful
        ]

    @property
    def has_failures(self) -> bool:
        return self.error is not None or any(
            result.status.is_unsuccessful for result in self.account_results
        )

    def to_dict(self) -> dict[str, object]:
        singular_key, _ = _result_labels(self.config_branch)

        return {
            singular_key: self.target_name,
            "generated_at": self.generated_at,
            "dry_run": self.dry_run,
            "total_accounts": self.total_accounts,
            "account_results": [result.to_dict() for result in self.account_results],
            "error": self.error,
        }

    @classmethod
    def create(
        cls,
        *,
        config_branch: ConfigBranch,
        target_name: str,
        dry_run: bool,
        account_results: list[AccountResult],
        error: str | None = None,
    ) -> TargetResult:
        return cls(
            config_branch=config_branch,
            target_name=target_name,
            generated_at=datetime.datetime.now(datetime.UTC).isoformat(),
            dry_run=dry_run,
            account_results=account_results,
            error=error,
        )


@dataclass(frozen=True, slots=True)
class EngineResult:
    """
    Top-level result container for a full config-driven execution.
    """

    config_branch: ConfigBranch
    state: EngineState
    generated_at: str
    auth_results: list[AuthResult]
    target_results: list[TargetResult]

    @property
    def has_auth_failures(self) -> bool:
        return any(auth_result.is_error for auth_result in self.auth_results)

    @property
    def has_target_failures(self) -> bool:
        return any(target_result.has_failures for target_result in self.target_results)

    @property
    def total_failed_accounts(self) -> int:
        return sum(
            len(target_result.failed_accounts) for target_result in self.target_results
        )

    @property
    def total_interrupted_accounts(self) -> int:
        return sum(
            len(target_result.interrupted_accounts)
            for target_result in self.target_results
        )

    def to_dict(self) -> dict[str, object]:
        _, plural_key = _result_labels(self.config_branch)

        return {
            "state": self.state.value,
            "generated_at": self.generated_at,
            "auth": [
                auth_result.to_dict(config_branch=self.config_branch)
                for auth_result in self.auth_results
            ],
            plural_key: [
                target_result.to_dict() for target_result in self.target_results
            ],
        }

    @classmethod
    def create(
        cls,
        *,
        config_branch: ConfigBranch,
        state: EngineState,
        auth_results: list[AuthResult],
        target_results: list[TargetResult],
    ) -> EngineResult:
        return cls(
            config_branch=config_branch,
            state=state,
            generated_at=datetime.datetime.now(datetime.UTC).isoformat(),
            auth_results=auth_results,
            target_results=target_results,
        )

    def build_summary(self) -> dict[str, object]:
        """
        Build a high-level summary of the execution suitable for CLI output.
        """
        singular_key, plural_key = _result_labels(self.config_branch)

        target_summaries: list[dict[str, object]] = []
        auth_results = [
            auth_result.to_dict(config_branch=self.config_branch)
            for auth_result in self.auth_results
        ]
        total_failed_accounts = 0
        total_interrupted_accounts = 0
        total_failed_tasks = 0

        for target_result in self.target_results:
            account_results = target_result.account_results

            failed_accounts = [
                account_result
                for account_result in account_results
                if account_result.status.is_error
            ]
            interrupted_accounts = [
                account_result
                for account_result in account_results
                if account_result.status.is_interrupted
            ]

            failed_tasks = sum(
                1
                for account_result in account_results
                for task in account_result.tasks
                if task.status.is_error
            )

            total_failed_accounts += len(failed_accounts)
            total_interrupted_accounts += len(interrupted_accounts)
            total_failed_tasks += failed_tasks

            target_summaries.append(
                {
                    singular_key: target_result.target_name,
                    "total_accounts": target_result.total_accounts,
                    "failed_accounts": len(failed_accounts),
                    "interrupted_accounts": len(interrupted_accounts),
                    "failed_tasks": failed_tasks,
                    "has_failures": target_result.has_failures,
                    "error": target_result.error,
                }
            )

        return {
            "state": self.state.value,
            "generated_at": self.generated_at,
            "auth": auth_results,
            plural_key: target_summaries,
            "total_failed_accounts": total_failed_accounts,
            "total_interrupted_accounts": total_interrupted_accounts,
            "total_failed_tasks": total_failed_tasks,
        }
