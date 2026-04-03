from __future__ import annotations

import datetime
import logging
import time
from typing import Literal

import boto3
from boto3.session import Session

from anvil.execution_context import ExecutionContext
from anvil.results import AccountResult, ExecutionStatus, TaskResult
from anvil.session import AssumedRoleCredentials, SessionFactory
from anvil.task_definition import ActionRecorder

__LOGGER__ = logging.getLogger(__name__)


class Account:
    """
    Executable AWS account.

    Owns:
    - account identity
    - management vs member behavior
    - execution lifecycle

    Does NOT own:
    - threading
    - retries
    - orchestration
    """

    def __init__(
        self,
        *,
        account_id: str,
        account_alias: str,
        is_management: bool,
        assume_role: bool,
        base_session: boto3.Session,
        context: ExecutionContext,
        regions: list[str],
        session_factory: SessionFactory,
    ) -> None:
        self.account_id: str = account_id
        self.account_alias: str = account_alias
        self.is_management: bool = is_management
        self._assume_role: bool = assume_role
        self._base_session: Session = base_session
        self._context: ExecutionContext = context
        self._regions: list[str] = regions
        self._session_factory: SessionFactory = session_factory

    def execute(self) -> AccountResult:
        """
        Execute the configured task graph for this account across all effective regions.
        """
        __LOGGER__.info(f"Processing account {self.account_alias} ({self.account_id})")

        # Account-level timing
        started_perf = time.perf_counter()
        started_at = datetime.datetime.now(datetime.UTC).isoformat()

        actions = ActionRecorder(actions=[])
        task_results: list[TaskResult] = []
        interrupted = False

        optional_map = {task.name: task.optional for task in self._context.tasks}

        try:
            assumed_credentials: AssumedRoleCredentials | None = None

            if self._assume_role:
                assumed_credentials: AssumedRoleCredentials = (
                    self._get_assumed_role_credentials()
                )
            else:
                self._validate_direct_account_access()

            # Execute configured regions in declared order
            for region in self._regions:
                session: Session = self._get_region_session(
                    region=region, assumed_credentials=assumed_credentials
                )

                region_task_results: dict[str, TaskResult] = {}

                # Execute tasks
                for task in self._context.tasks:
                    # Cooperative cancellation check
                    if self._context.cancel_event.is_set():
                        __LOGGER__.warning(
                            f"Account {self.account_id} stopping due to cancellation signal"
                        )
                        interrupted = True
                        break

                    # Dependency gate
                    dependency_failed: bool = any(
                        region_task_results[dep].status.is_error
                        for dep in task.depends_on
                        if dep in region_task_results
                    )

                    if dependency_failed:
                        now_at: str = datetime.datetime.now(datetime.UTC).isoformat()

                        blocked_result = TaskResult(
                            task_name=task.name,
                            region=region,
                            status=ExecutionStatus.ERROR,
                            started_at=now_at,
                            ended_at=now_at,
                            duration_seconds=0.0,
                            error="Blocked: dependency failed",
                        )

                        region_task_results[task.name] = blocked_result
                        task_results.append(blocked_result)

                        if not task.optional:
                            __LOGGER__.error(
                                f"Task '{task.name}' blocked by failed dependency "
                                f"in account {self.account_id} region {region}"
                            )
                            break

                        __LOGGER__.warning(
                            f"Optional task '{task.name}' skipped due to dependency "
                            f"failure in account {self.account_id} region {region}"
                        )
                        continue

                    # Execute task
                    task_started_perf: int | float = time.perf_counter()
                    task_started_at: str = datetime.datetime.now(
                        datetime.UTC
                    ).isoformat()

                    try:
                        result = task.run(
                            account_id=self.account_id,
                            account_alias=self.account_alias,
                            session=session,
                            dry_run=self._context.dry_run,
                            metadata=self._context.metadata,
                            actions=actions,
                        )

                        task_ended_perf: int | float = time.perf_counter()
                        task_ended_at: str = datetime.datetime.now(
                            datetime.UTC
                        ).isoformat()

                        success_result = TaskResult(
                            task_name=task.name,
                            region=region,
                            status=ExecutionStatus.SUCCESS,
                            started_at=task_started_at,
                            ended_at=task_ended_at,
                            duration_seconds=task_ended_perf - task_started_perf,
                            result=result,
                        )

                        region_task_results[task.name] = success_result
                        task_results.append(success_result)

                    except Exception as error:
                        task_ended_perf: int | float = time.perf_counter()
                        task_ended_at: str = datetime.datetime.now(
                            datetime.UTC
                        ).isoformat()

                        error_result = TaskResult(
                            task_name=task.name,
                            region=region,
                            status=ExecutionStatus.ERROR,
                            started_at=task_started_at,
                            ended_at=task_ended_at,
                            duration_seconds=task_ended_perf - task_started_perf,
                            error=str(error),
                        )

                        region_task_results[task.name] = error_result
                        task_results.append(error_result)

                        __LOGGER__.error(
                            f"Task '{task.name}' failed in account "
                            f"{self.account_id} region {region}: {error}"
                        )

                        if not task.optional:
                            break

                non_optional_region_failure = any(
                    result.status.is_error
                    and not optional_map.get(result.task_name, False)
                    for result in region_task_results.values()
                )

                if interrupted or non_optional_region_failure:
                    break

            # Derive account status
            account_failed: bool = any(
                result.status.is_error and not optional_map.get(result.task_name, False)
                for result in task_results
            )
            expected_total_tasks: int = len(self._context.tasks) * len(self._regions)
            account_interrupted: bool = (
                interrupted and len(task_results) < expected_total_tasks
            )

            ended_perf: int | float = time.perf_counter()
            ended_at: str = datetime.datetime.now(datetime.UTC).isoformat()

            if account_failed:
                account_status: Literal[ExecutionStatus.ERROR] = ExecutionStatus.ERROR
            elif account_interrupted:
                account_status: Literal[ExecutionStatus.INTERRUPTED] = (
                    ExecutionStatus.INTERRUPTED
                )
            else:
                account_status: Literal[ExecutionStatus.SUCCESS] = (
                    ExecutionStatus.SUCCESS
                )

            return AccountResult(
                account_id=self.account_id,
                account_alias=self.account_alias,
                status=account_status,
                started_at=started_at,
                ended_at=ended_at,
                duration_seconds=ended_perf - started_perf,
                tasks=task_results,
            )

        except Exception as error:
            ended_perf: int | float = time.perf_counter()
            ended_at: str = datetime.datetime.now(datetime.UTC).isoformat()

            return AccountResult(
                account_id=self.account_id,
                account_alias=self.account_alias,
                status=ExecutionStatus.ERROR,
                started_at=started_at,
                ended_at=ended_at,
                duration_seconds=ended_perf - started_perf,
                tasks=task_results,
                error=str(error),
            )

    def _get_assumed_role_credentials(self) -> AssumedRoleCredentials:
        """
        Assume the configured member-account role once for this account execution.

        The returned temporary credentials are then reused to build
        region-scoped sessions for each configured region.
        """
        source_region: str = self._regions[0]
        worker_session: Session = self._session_factory.get_worker_session(
            profile_name=self._base_session.profile_name, region_name=source_region
        )

        if self._context.role_name is None:
            raise ValueError("Expected role_name for assume-role execution")

        return self._session_factory.assume_role_credentials(
            session=worker_session,
            account_id=self.account_id,
            role_name=self._context.role_name,
        )

    def _validate_direct_account_access(self) -> None:
        source_region: str = self._regions[0]
        worker_session: Session = self._session_factory.get_worker_session(
            profile_name=self._base_session.profile_name, region_name=source_region
        )

        caller_account_id = worker_session.client("sts").get_caller_identity()[
            "Account"
        ]
        if caller_account_id != self.account_id:
            raise ValueError(
                f"Direct execution credentials resolve to account '{caller_account_id}', "
                f"not target account '{self.account_id}'"
            )

    def _get_region_session(
        self, *, region: str, assumed_credentials: AssumedRoleCredentials | None
    ) -> boto3.Session:
        """
        Build the execution session for one account-region pair.

        Management accounts use the org/profile-backed worker session directly.
        Assume-role execution builds a regional session from the already-assumed
        temporary credentials.
        """
        if not self._assume_role:
            return self._session_factory.get_worker_session(
                profile_name=self._base_session.profile_name, region_name=region
            )

        if assumed_credentials is None:
            raise ValueError(
                "Expected assumed credentials for member account execution"
            )

        return self._session_factory.create_session_from_credentials(
            credentials=assumed_credentials, region_name=region
        )
