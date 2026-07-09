from __future__ import annotations

import datetime
import logging
import threading
import time
from collections import deque
from concurrent.futures import (
    FIRST_COMPLETED,
    CancelledError,
    Future,
    ThreadPoolExecutor,
    wait,
)
from dataclasses import dataclass, field
from enum import StrEnum

import boto3
from boto3.session import Session

from anvil.benchmark import BenchmarkRecorder
from anvil.execution_context import ExecutionContext
from anvil.results import EntityResult, ExecutionStatus, TaskResult
from anvil.session import AssumedRoleCredentials, SessionFactory
from anvil.actions import ActionRecorder
from anvil.task_context import TaskCallContext
from anvil.task_invocation import invoke_task

__LOGGER__ = logging.getLogger(__name__)

MINIMUM_ASSUMED_CREDENTIAL_REFRESH_WINDOW = datetime.timedelta(minutes=5)
ASSUMED_CREDENTIAL_REFRESH_BUFFER = datetime.timedelta(minutes=2)


class AccountAccessStrategy(StrEnum):
    """
    Credential path used to access an executable account.
    """

    BASE_SESSION = "base_session"
    ASSUME_ROLE = "assume_role"
    DIRECT_PROFILE = "direct_profile"


@dataclass(frozen=True, slots=True)
class RegionExecutionOutcome:
    region: str
    task_results: list[TaskResult]
    interrupted: bool
    failed: bool
    duration_seconds: float


@dataclass(slots=True)
class _AssumedCredentialState:
    credentials: AssumedRoleCredentials
    refresh_window: datetime.timedelta = MINIMUM_ASSUMED_CREDENTIAL_REFRESH_WINDOW
    refresh_count: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


class Account:
    """
    Executable AWS account.

    Owns:
    - account identity
    - access strategy
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
        access_strategy: AccountAccessStrategy,
        base_session: boto3.Session,
        context: ExecutionContext,
        regions: list[str],
        session_factory: SessionFactory,
    ) -> None:
        self.account_id: str = account_id
        self.account_alias: str = account_alias
        self.is_management: bool = is_management
        self.access_strategy: AccountAccessStrategy = access_strategy
        self._base_session: Session = base_session
        self._context: ExecutionContext = context
        self._regions: list[str] = regions
        self._session_factory: SessionFactory = session_factory

    def execute(self) -> EntityResult:
        """
        Execute the configured task graph for this account across all effective regions.
        """
        __LOGGER__.info(f"Processing account {self.account_alias} ({self.account_id})")

        # Account-level timing
        started_perf = time.perf_counter()
        started_at = datetime.datetime.now(datetime.UTC).isoformat()

        task_results: list[TaskResult] = []

        optional_map = {task.name: task.optional for task in self._context.tasks}

        try:
            assumed_credential_state: _AssumedCredentialState | None = None
            recorder = BenchmarkRecorder(enabled=self._context.benchmark_enabled)
            recorder.update(
                {
                    "access_strategy": self.access_strategy.value,
                    "assume_role_seconds": 0.0,
                    "assume_role_refresh_count": 0,
                    "direct_access_validation_seconds": 0.0,
                }
            )

            if self.access_strategy is AccountAccessStrategy.ASSUME_ROLE:
                with recorder.phase("assume_role_seconds"):
                    assumed_credential_state = _AssumedCredentialState(
                        credentials=self._get_assumed_role_credentials()
                    )
            else:
                with recorder.phase("direct_access_validation_seconds"):
                    self._validate_direct_account_access()

            account_cancel_event = threading.Event()
            with recorder.phase("region_execution_seconds"):
                region_outcomes = self._execute_regions(
                    assumed_credential_state=assumed_credential_state,
                    optional_map=optional_map,
                    account_cancel_event=account_cancel_event,
                )
            if assumed_credential_state is not None:
                recorder.set(
                    "assume_role_refresh_count", assumed_credential_state.refresh_count
                )
                recorder.set(
                    "assume_role_refresh_window_seconds",
                    assumed_credential_state.refresh_window.total_seconds(),
                )
            recorder.set(
                "regions",
                {
                    outcome.region: {
                        "duration_seconds": outcome.duration_seconds,
                        "task_count": len(outcome.task_results),
                        "interrupted": outcome.interrupted,
                        "failed": outcome.failed,
                    }
                    for outcome in region_outcomes
                },
            )
            for outcome in region_outcomes:
                task_results.extend(outcome.task_results)

            self._sort_task_results(task_results)
            account_status = self._derive_account_status(
                task_results=task_results,
                region_outcomes=region_outcomes,
                optional_map=optional_map,
            )

            ended_perf: int | float = time.perf_counter()
            ended_at: str = datetime.datetime.now(datetime.UTC).isoformat()

            return EntityResult(
                id=self.account_id,
                name=self.account_alias,
                type="account",
                status=account_status,
                started_at=started_at,
                ended_at=ended_at,
                duration_seconds=ended_perf - started_perf,
                tasks=task_results,
                benchmark=recorder.data,
            )

        except Exception as error:
            ended_perf: int | float = time.perf_counter()
            ended_at: str = datetime.datetime.now(datetime.UTC).isoformat()

            return EntityResult(
                id=self.account_id,
                name=self.account_alias,
                type="account",
                status=ExecutionStatus.ERROR,
                started_at=started_at,
                ended_at=ended_at,
                duration_seconds=ended_perf - started_perf,
                tasks=task_results,
                error=str(error),
            )

    def _derive_account_status(
        self,
        *,
        task_results: list[TaskResult],
        region_outcomes: list[RegionExecutionOutcome],
        optional_map: dict[str, bool],
    ) -> ExecutionStatus:
        account_failed = any(
            result.status.is_error and not optional_map.get(result.task_name, False)
            for result in task_results
        )
        if account_failed:
            return ExecutionStatus.ERROR

        expected_total_tasks = len(self._context.tasks) * len(self._regions)
        account_interrupted = (
            self._context.cancel_event.is_set()
            or any(outcome.interrupted for outcome in region_outcomes)
        ) and len(task_results) < expected_total_tasks
        if account_interrupted:
            return ExecutionStatus.INTERRUPTED

        return ExecutionStatus.SUCCESS

    def _execute_regions(
        self,
        *,
        assumed_credential_state: _AssumedCredentialState | None,
        optional_map: dict[str, bool],
        account_cancel_event: threading.Event,
    ) -> list[RegionExecutionOutcome]:
        if self._context.max_parallel_regions == 1:
            return self._execute_regions_sequential(
                assumed_credential_state=assumed_credential_state,
                optional_map=optional_map,
                account_cancel_event=account_cancel_event,
            )

        return self._execute_regions_parallel(
            assumed_credential_state=assumed_credential_state,
            optional_map=optional_map,
            account_cancel_event=account_cancel_event,
        )

    def _execute_regions_sequential(
        self,
        *,
        assumed_credential_state: _AssumedCredentialState | None,
        optional_map: dict[str, bool],
        account_cancel_event: threading.Event,
    ) -> list[RegionExecutionOutcome]:
        outcomes: list[RegionExecutionOutcome] = []
        actions = ActionRecorder(actions=[])

        for region in self._regions:
            outcome = self._execute_region(
                region=region,
                assumed_credential_state=assumed_credential_state,
                optional_map=optional_map,
                account_cancel_event=account_cancel_event,
                actions=actions,
            )
            outcomes.append(outcome)
            self._update_assumed_credential_refresh_window(
                assumed_credential_state=assumed_credential_state,
                region_duration_seconds=outcome.duration_seconds,
            )

            if outcome.interrupted or outcome.failed:
                account_cancel_event.set()
                break

        return outcomes

    def _execute_regions_parallel(
        self,
        *,
        assumed_credential_state: _AssumedCredentialState | None,
        optional_map: dict[str, bool],
        account_cancel_event: threading.Event,
    ) -> list[RegionExecutionOutcome]:
        pending_regions: deque[str] = deque(self._regions)
        active_futures: set[Future[RegionExecutionOutcome]] = set()
        outcomes: list[RegionExecutionOutcome] = []
        region_worker_limit = min(
            self._context.max_parallel_regions, len(self._regions)
        )

        with ThreadPoolExecutor(max_workers=region_worker_limit) as executor:
            while pending_regions or active_futures:
                while (
                    pending_regions
                    and not account_cancel_event.is_set()
                    and not self._context.cancel_event.is_set()
                    and len(active_futures) < region_worker_limit
                ):
                    region = pending_regions.popleft()
                    future = executor.submit(
                        self._execute_region,
                        region=region,
                        assumed_credential_state=assumed_credential_state,
                        optional_map=optional_map,
                        account_cancel_event=account_cancel_event,
                    )
                    active_futures.add(future)

                if not active_futures:
                    break

                done, _ = wait(active_futures, return_when=FIRST_COMPLETED)

                for future in done:
                    active_futures.remove(future)
                    try:
                        outcome = future.result()
                    except CancelledError:
                        continue

                    outcomes.append(outcome)
                    self._update_assumed_credential_refresh_window(
                        assumed_credential_state=assumed_credential_state,
                        region_duration_seconds=outcome.duration_seconds,
                    )

                    if outcome.interrupted or outcome.failed:
                        account_cancel_event.set()
                        pending_regions.clear()

                if account_cancel_event.is_set():
                    for future in active_futures:
                        future.cancel()

        return outcomes

    def _execute_region(
        self,
        *,
        region: str,
        assumed_credential_state: _AssumedCredentialState | None,
        optional_map: dict[str, bool],
        account_cancel_event: threading.Event,
        actions: ActionRecorder | None = None,
    ) -> RegionExecutionOutcome:
        region_started = time.perf_counter()
        region_session = self._get_region_session(
            region=region, assumed_credential_state=assumed_credential_state
        )
        session = self._session_factory.create_cached_client_session(
            session=region_session
        )

        if actions is None:
            actions = ActionRecorder(actions=[])
        task_results: list[TaskResult] = []
        region_task_results: dict[str, TaskResult] = {}
        interrupted = False

        for task in self._context.tasks:
            if self._context.cancel_event.is_set() or account_cancel_event.is_set():
                __LOGGER__.warning(
                    f"Account {self.account_id} region {region} stopping "
                    f"due to cancellation signal"
                )
                interrupted = True
                break

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

            task_started_perf: int | float = time.perf_counter()
            task_started_at: str = datetime.datetime.now(datetime.UTC).isoformat()

            try:
                task_context = TaskCallContext(
                    provider="aws",
                    execution_target_id=self.account_id,
                    execution_target_name=self.account_alias,
                    execution_target_type="account",
                    region=region,
                    location=region,
                    session=session,
                    dry_run=self._context.dry_run,
                    metadata=self._context.metadata,
                    actions=actions,
                )
                result = invoke_task(
                    task.run,
                    context=task_context,
                    legacy_kwargs={
                        "account_id": self.account_id,
                        "account_alias": self.account_alias,
                        "session": session,
                        "dry_run": self._context.dry_run,
                        "metadata": self._context.metadata,
                        "actions": actions,
                    },
                )

                task_ended_perf: int | float = time.perf_counter()
                task_ended_at: str = datetime.datetime.now(datetime.UTC).isoformat()

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
                task_ended_at: str = datetime.datetime.now(datetime.UTC).isoformat()

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
            result.status.is_error and not optional_map.get(result.task_name, False)
            for result in region_task_results.values()
        )

        return RegionExecutionOutcome(
            region=region,
            task_results=task_results,
            interrupted=interrupted,
            failed=non_optional_region_failure,
            duration_seconds=time.perf_counter() - region_started,
        )

    def _sort_task_results(self, task_results: list[TaskResult]) -> None:
        region_order = {region: index for index, region in enumerate(self._regions)}
        task_order = {
            task.name: index for index, task in enumerate(self._context.tasks)
        }
        task_results.sort(
            key=lambda result: (
                region_order.get(result.region, len(region_order)),
                task_order.get(result.task_name, len(task_order)),
            )
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

    @staticmethod
    def _assumed_credentials_should_refresh(
        credentials: AssumedRoleCredentials,
        *,
        refresh_window: datetime.timedelta = MINIMUM_ASSUMED_CREDENTIAL_REFRESH_WINDOW,
        now: datetime.datetime | None = None,
    ) -> bool:
        expiration = credentials.expiration
        if not isinstance(expiration, datetime.datetime):
            return False

        if expiration.tzinfo is None:
            expiration = expiration.replace(tzinfo=datetime.UTC)

        current_time = now or datetime.datetime.now(datetime.UTC)
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=datetime.UTC)

        return expiration <= current_time + refresh_window

    @staticmethod
    def _refresh_window_for_region_duration(
        region_duration_seconds: float,
    ) -> datetime.timedelta:
        if region_duration_seconds <= 0:
            return MINIMUM_ASSUMED_CREDENTIAL_REFRESH_WINDOW

        return max(
            MINIMUM_ASSUMED_CREDENTIAL_REFRESH_WINDOW,
            datetime.timedelta(seconds=region_duration_seconds)
            + ASSUMED_CREDENTIAL_REFRESH_BUFFER,
        )

    def _update_assumed_credential_refresh_window(
        self,
        *,
        assumed_credential_state: _AssumedCredentialState | None,
        region_duration_seconds: float,
    ) -> None:
        if assumed_credential_state is None:
            return

        observed_window = self._refresh_window_for_region_duration(
            region_duration_seconds
        )
        with assumed_credential_state.lock:
            if observed_window > assumed_credential_state.refresh_window:
                assumed_credential_state.refresh_window = observed_window

    def _get_valid_assumed_role_credentials(
        self, state: _AssumedCredentialState
    ) -> AssumedRoleCredentials:
        with state.lock:
            if self._assumed_credentials_should_refresh(
                state.credentials, refresh_window=state.refresh_window
            ):
                __LOGGER__.info(
                    f"Refreshing assumed-role credentials for account "
                    f"{self.account_alias} ({self.account_id})"
                )
                state.credentials = self._get_assumed_role_credentials()
                state.refresh_count += 1

            return state.credentials

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
        self, *, region: str, assumed_credential_state: _AssumedCredentialState | None
    ) -> boto3.Session:
        """
        Build the execution session for one account-region pair.

        Base-session and direct-profile accounts use the profile-backed worker
        session directly.
        Assume-role execution builds a regional session from the already-assumed
        temporary credentials.
        """
        if self.access_strategy is not AccountAccessStrategy.ASSUME_ROLE:
            return self._session_factory.get_worker_session(
                profile_name=self._base_session.profile_name, region_name=region
            )

        if assumed_credential_state is None:
            raise ValueError(
                "Expected assumed credentials for assume-role account execution"
            )

        assumed_credentials = self._get_valid_assumed_role_credentials(
            assumed_credential_state
        )
        return self._session_factory.create_session_from_credentials(
            credentials=assumed_credentials, region_name=region
        )
