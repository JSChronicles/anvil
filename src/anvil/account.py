from __future__ import annotations

import datetime
import logging
import time

import boto3

from anvil.execution_context import ExecutionContext
from anvil.results import AccountResult, ExecutionStatus, TaskResult
from anvil.session import assume_role, get_worker_session
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
        base_session: boto3.Session,
        context: ExecutionContext,
    ) -> None:
        self.account_id = account_id
        self.account_alias = account_alias
        self.is_management = is_management
        self._base_session = base_session
        self._context = context

    def execute(self) -> AccountResult:
        __LOGGER__.info(f"Processing account {self.account_alias} ({self.account_id})")

        # Account-level timing
        started_perf = time.perf_counter()
        started_at = datetime.datetime.now(datetime.UTC).isoformat()

        actions = ActionRecorder(actions=[])

        worker_session = get_worker_session(
            profile_name=self._base_session.profile_name,
            region_name=self._context.region,
        )

        task_results: dict[str, TaskResult] = {}

        optional_map = {task.name: task.optional for task in self._context.tasks}

        try:
            # Establish session
            if self.is_management:
                session = worker_session
            else:
                session = assume_role(
                    session=worker_session,
                    account_id=self.account_id,
                    role_name=self._context.role_name,
                )

            # Execute tasks
            for task in self._context.tasks:
                # Cooperative cancellation check
                if self._context.cancel_event.is_set():
                    __LOGGER__.warning(
                        f"Account {self.account_id} stopping due to cancellation signal"
                    )
                    break

                # Dependency gate
                dependency_failed = any(
                    task_results[dep].status.is_error
                    for dep in task.depends_on
                    if dep in task_results
                )

                if dependency_failed:
                    now_at = datetime.datetime.now(datetime.UTC).isoformat()

                    task_results[task.name] = TaskResult(
                        task_name=task.name,
                        status=ExecutionStatus.ERROR,
                        started_at=now_at,
                        ended_at=now_at,
                        duration_seconds=0.0,
                        error="Blocked: dependency failed",
                    )

                    if not task.optional:
                        __LOGGER__.error(
                            f"Task '{task.name}' blocked by failed dependency "
                            f"in account {self.account_id}"
                        )
                        break

                    __LOGGER__.warning(
                        f"Optional task '{task.name}' skipped due to dependency "
                        f"failure in account {self.account_id}"
                    )
                    continue

                # Execute task
                task_started_perf = time.perf_counter()
                task_started_at = datetime.datetime.now(datetime.UTC).isoformat()

                try:
                    result = task.run(
                        account_id=self.account_id,
                        account_alias=self.account_alias,
                        session=session,
                        dry_run=self._context.dry_run,
                        metadata=self._context.metadata,
                        actions=actions,
                    )

                    task_ended_perf = time.perf_counter()
                    task_ended_at = datetime.datetime.now(datetime.UTC).isoformat()

                    task_results[task.name] = TaskResult(
                        task_name=task.name,
                        status=ExecutionStatus.SUCCESS,
                        started_at=task_started_at,
                        ended_at=task_ended_at,
                        duration_seconds=task_ended_perf - task_started_perf,
                        result=result,
                    )

                except Exception as error:
                    task_ended_perf = time.perf_counter()
                    task_ended_at = datetime.datetime.now(datetime.UTC).isoformat()

                    task_results[task.name] = TaskResult(
                        task_name=task.name,
                        status=ExecutionStatus.ERROR,
                        started_at=task_started_at,
                        ended_at=task_ended_at,
                        duration_seconds=task_ended_perf - task_started_perf,
                        error=str(error),
                    )

                    __LOGGER__.error(
                        f"Task '{task.name}' failed in account "
                        f"{self.account_id}: {error}"
                    )

                    if not task.optional:
                        break

            # Derive account status
            account_failed = any(
                result.status.is_error and not optional_map.get(result.task_name, False)
                for result in task_results.values()
            )

            ended_perf = time.perf_counter()
            ended_at = datetime.datetime.now(datetime.UTC).isoformat()

            return AccountResult(
                account_id=self.account_id,
                account_alias=self.account_alias,
                status=ExecutionStatus.ERROR
                if account_failed
                else ExecutionStatus.SUCCESS,
                started_at=started_at,
                ended_at=ended_at,
                duration_seconds=ended_perf - started_perf,
                tasks=list(task_results.values()),
            )

        except Exception as error:
            ended_perf = time.perf_counter()
            ended_at = datetime.datetime.now(datetime.UTC).isoformat()

            return AccountResult(
                account_id=self.account_id,
                account_alias=self.account_alias,
                status=ExecutionStatus.ERROR,
                started_at=started_at,
                ended_at=ended_at,
                duration_seconds=ended_perf - started_perf,
                tasks=list(task_results.values()),
                error=str(error),
            )
