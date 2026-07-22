from __future__ import annotations

import datetime
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import (
    FIRST_COMPLETED,
    CancelledError,
    Future,
    ThreadPoolExecutor,
    as_completed,
    wait,
)
from dataclasses import dataclass, field, replace

from boto3.session import Session

from anvil.benchmark import BenchmarkRecorder
from anvil.account_resolver import AccountResolver
from anvil.actions import ActionRecorder
from anvil.auth import AuthSource, auth_check, infer_auth_source
from anvil.descriptors import ConfigBranch, TargetDescriptor
from anvil.execution_context import ExecutionContext
from anvil.organization import OrganizationResolver
from anvil.providers.aws import AwsProvider
from anvil.providers.azure import AzureProvider
from anvil.provider_loader import load_provider
from anvil.providers.base import (
    ExecutionTarget,
    Provider,
    ProviderAuthResult,
    ProviderExecutionRuntime,
    configured_or_default_regions,
    validate_resolved_regions,
)
from anvil.results import (
    AuthResult,
    EngineResult,
    EngineState,
    EntityResult,
    ExecutionStatus,
    TargetResult,
    TaskResult,
)
from anvil.session import SessionFactory
from anvil.task_context import TaskCallContext
from anvil.task_invocation import invoke_task
from anvil.task_loader import ResolvedExecution, ResolvedTask, TaskScope, resolve_tasks

__LOGGER__ = logging.getLogger(__name__)


STATE_PRECEDENCE: dict[EngineState, int] = {
    EngineState.AUTH_FAILED: 4,
    EngineState.CANCELLED: 3,
    EngineState.COMPLETED_WITH_FAILURES: 2,
    EngineState.COMPLETED_SUCCESS: 1,
}

DEFAULT_AUTH_CHECK_MAX_WORKERS = 4


def _load_provider(provider_name: str) -> Provider:
    return load_provider(provider_name)


@dataclass(slots=True)
class _SingleFlightEntry:
    event: threading.Event = field(default_factory=threading.Event)
    value: object | None = None
    error: BaseException | None = None


class _SingleFlightCache:
    def __init__(self) -> None:
        self._values: dict[object, object] = {}
        self._flights: dict[object, _SingleFlightEntry] = {}
        self._lock = threading.Lock()

    def get_or_create(
        self, *, key: object, create: Callable[[], object]
    ) -> tuple[object, bool, bool]:
        with self._lock:
            existing = self._values.get(key)
            if existing is not None:
                return existing, True, False

            flight = self._flights.get(key)
            if flight is None:
                flight = _SingleFlightEntry()
                self._flights[key] = flight
                owns_create = True
            else:
                owns_create = False

        if owns_create:
            try:
                value = create()
            except BaseException as error:
                with self._lock:
                    flight.error = error
                    self._flights.pop(key, None)
                    flight.event.set()
                raise

            with self._lock:
                existing = self._values.get(key)
                cached_value = existing if existing is not None else value
                self._values[key] = cached_value
                flight.value = cached_value
                self._flights.pop(key, None)
                flight.event.set()

            return cached_value, False, False

        flight.event.wait()
        if flight.error is not None:
            raise flight.error
        if flight.value is None:
            raise RuntimeError("Single-flight cache entry completed empty")

        return flight.value, True, True


@dataclass(frozen=True, slots=True)
class PreparedTarget:
    index: int
    effective_target: TargetDescriptor
    auth_result: AuthResult
    context: ExecutionContext | None
    session_factory: SessionFactory = field(default_factory=SessionFactory)
    provider_preflight: object | None = None
    preflight_error: str | None = None
    exclusive_execution_key: object | None = None
    exclusive_execution_keys: tuple[object, ...] = ()
    base_session: Session | None = None
    organization_id: str | None = None
    management_account_id: str | None = None
    base_session_account_id: str | None = None
    discovered_accounts: dict[str, dict[str, str]] | None = None
    region_statuses: dict[str, str] | None = None
    effective_include: list[str] | None = None
    effective_exclude: list[str] | None = None
    benchmark: dict[str, object] | None = None

    @property
    def runnable(self) -> bool:
        return self.context is not None


@dataclass(frozen=True, slots=True)
class TargetExecutionOutcome:
    index: int
    target_result: TargetResult
    cancelled: bool


@dataclass(frozen=True, slots=True)
class AuthCheckOutcome:
    status: ExecutionStatus
    source: str
    started_at: str
    ended_at: str
    duration_seconds: float
    message: str | None
    remediation: str | None


@dataclass(frozen=True, slots=True)
class _AuthCheckCacheLookup:
    outcome: AuthCheckOutcome
    hit: bool
    waited: bool


class AuthCheckCache:
    def __init__(self) -> None:
        self._cache = _SingleFlightCache()

    def get_or_check(
        self,
        *,
        profile: str | None,
        auth_source: AuthSource,
        check: Callable[[], AuthCheckOutcome],
    ) -> _AuthCheckCacheLookup:
        key = (profile, auth_source.value)
        outcome, hit, waited = self._cache.get_or_create(key=key, create=check)
        if not isinstance(outcome, AuthCheckOutcome):
            raise RuntimeError("Auth check cache returned unexpected value")

        return _AuthCheckCacheLookup(outcome=outcome, hit=hit, waited=waited)


@dataclass(frozen=True, slots=True)
class OrganizationRunCacheEntry:
    management_account_id: str
    discovered_accounts: dict[str, dict[str, str]]
    region_statuses: dict[str, str]


@dataclass(frozen=True, slots=True)
class _OrganizationRunCacheLookup:
    entry: object
    hit: bool
    waited: bool


class OrganizationRunCache:
    def __init__(self) -> None:
        self._cache = _SingleFlightCache()

    def get_or_discover(
        self, *, organization_id: str, discover: Callable[[], object]
    ) -> _OrganizationRunCacheLookup:
        entry, hit, waited = self._cache.get_or_create(
            key=organization_id, create=discover
        )

        return _OrganizationRunCacheLookup(entry=entry, hit=hit, waited=waited)


def _elevate_state(current: EngineState, new: EngineState) -> EngineState:
    """
    Elevate engine state based on explicit precedence rules.
    """
    if STATE_PRECEDENCE[new] > STATE_PRECEDENCE[current]:
        return new
    return current


def _engine_state_from_auth_results(*, auth_results: list[AuthResult]) -> EngineState:
    if any(auth_result.is_error for auth_result in auth_results):
        return EngineState.AUTH_FAILED

    return EngineState.COMPLETED_SUCCESS


def _auth_outcome_from_result(auth_result: AuthResult) -> AuthCheckOutcome:
    return AuthCheckOutcome(
        status=auth_result.status,
        source=auth_result.source,
        started_at=auth_result.started_at,
        ended_at=auth_result.ended_at,
        duration_seconds=auth_result.duration_seconds,
        message=auth_result.message,
        remediation=auth_result.remediation,
    )


def _auth_result_from_outcome(
    *, target_name: str, outcome: AuthCheckOutcome, cached: bool
) -> AuthResult:
    if cached:
        started_at = datetime.datetime.now(datetime.UTC).isoformat()
        ended_at = started_at
        duration_seconds = 0.0
    else:
        started_at = outcome.started_at
        ended_at = outcome.ended_at
        duration_seconds = outcome.duration_seconds

    return AuthResult(
        target_name=target_name,
        status=outcome.status,
        source=outcome.source,
        started_at=started_at,
        ended_at=ended_at,
        duration_seconds=duration_seconds,
        message=outcome.message,
        remediation=outcome.remediation,
    )


def _run_cached_auth_check_for_target(
    *, target: TargetDescriptor, auth_cache: AuthCheckCache
) -> AuthResult:
    auth_source: AuthSource = infer_auth_source(target.profile)

    def check() -> AuthCheckOutcome:
        return _auth_outcome_from_result(
            auth_check(
                target_name=target.name, profile=target.profile, auth_source=auth_source
            )
        )

    lookup = auth_cache.get_or_check(
        profile=target.profile, auth_source=auth_source, check=check
    )
    return _auth_result_from_outcome(
        target_name=target.name, outcome=lookup.outcome, cached=lookup.hit
    )


def _auth_result_from_provider_result(
    *,
    target_name: str,
    provider_result: ProviderAuthResult,
    started_at: str,
    started_perf: float,
) -> AuthResult:
    ended_at = datetime.datetime.now(datetime.UTC).isoformat()
    return AuthResult(
        target_name=target_name,
        status=provider_result.status,
        source=provider_result.source,
        started_at=started_at,
        ended_at=ended_at,
        duration_seconds=time.perf_counter() - started_perf,
        message=provider_result.message,
        remediation=provider_result.remediation,
    )


def _run_provider_auth_check_for_target(*, target: TargetDescriptor) -> AuthResult:
    provider = _load_provider(target.provider)
    started_perf = time.perf_counter()
    started_at = datetime.datetime.now(datetime.UTC).isoformat()
    try:
        provider_result = provider.auth_check(target)
    except ValueError as error:
        ended_at = datetime.datetime.now(datetime.UTC).isoformat()
        return AuthResult(
            target_name=target.name,
            status=ExecutionStatus.ERROR,
            source=target.provider,
            started_at=started_at,
            ended_at=ended_at,
            duration_seconds=time.perf_counter() - started_perf,
            message=str(error),
        )

    return _auth_result_from_provider_result(
        target_name=target.name,
        provider_result=provider_result,
        started_at=started_at,
        started_perf=started_perf,
    )


def _run_dispatched_auth_check_for_target(
    *, target: TargetDescriptor, auth_cache: AuthCheckCache
) -> AuthResult:
    if target.provider == "aws":
        return _run_cached_auth_check_for_target(target=target, auth_cache=auth_cache)

    return _run_provider_auth_check_for_target(target=target)


def _resolve_effective_account_filters(
    *,
    target: TargetDescriptor,
    cli_include: list[str] | None,
    cli_exclude: list[str] | None,
) -> tuple[list[str] | None, list[str] | None]:
    if target.config_branch is ConfigBranch.TARGETS and target.is_explicit_mode:
        effective_exclude = cli_exclude if cli_exclude is not None else target.exclude
        if cli_include is None:
            return target.include, effective_exclude

        configured_target_ids = set(target.include or [])
        narrowed_include = [
            target_id for target_id in cli_include if target_id in configured_target_ids
        ]
        return narrowed_include, effective_exclude

    if target.is_accounts_config:
        if target.provider in {"azure", "gcp"}:
            effective_exclude = (
                cli_exclude if cli_exclude is not None else target.exclude
            )
            if cli_include is None:
                return target.include, effective_exclude
            if target.include is None:
                return cli_include, effective_exclude

            configured_account_ids = set(target.include)
            narrowed_include = [
                account_id
                for account_id in cli_include
                if account_id in configured_account_ids
            ]
            return narrowed_include, effective_exclude

        if cli_include is None:
            return target.include, None

        configured_account_ids = set(target.include or [])
        narrowed_include: list[str] = [
            account_id
            for account_id in cli_include
            if account_id in configured_account_ids
        ]
        return narrowed_include, None

    effective_include: list[str] | None = target.include
    effective_exclude: list[str] | None = target.exclude

    if cli_include is not None:
        effective_include: list[str] = cli_include
    if cli_exclude is not None:
        effective_exclude: list[str] = cli_exclude

    return effective_include, effective_exclude


def _validate_effective_account_filters(
    *, target: TargetDescriptor, include: list[str] | None, exclude: list[str] | None
) -> None:
    if (
        target.config_branch is ConfigBranch.TARGETS
        and target.is_explicit_mode
        and not (
            target.provider == "gcp"
            and target.mode == "projects"
            and target.include is None
        )
        and exclude is not None
    ):
        raise ValueError(
            f"Target '{target.name}' provider '{target.provider}' mode "
            f"'{target.mode}' does not allow exclude; explicit modes require include."
        )

    if include is not None and exclude is not None:
        raise ValueError(
            f"Target '{target.name}' cannot use include and exclude together; "
            "they are mutually exclusive for all providers and modes."
        )


def _build_effective_target(
    *,
    target: TargetDescriptor,
    cli_dry_run: bool | None,
    cli_include: list[str] | None,
    cli_exclude: list[str] | None,
) -> TargetDescriptor:
    effective_dry_run: bool = cli_dry_run if cli_dry_run is not None else target.dry_run
    effective_include, effective_exclude = _resolve_effective_account_filters(
        target=target, cli_include=cli_include, cli_exclude=cli_exclude
    )
    _validate_effective_account_filters(
        target=target, include=effective_include, exclude=effective_exclude
    )

    return replace(
        target,
        dry_run=effective_dry_run,
        include=effective_include,
        exclude=effective_exclude,
    )


def _auth_result_from_config_error(
    *, target: TargetDescriptor, error: ValueError
) -> AuthResult:
    started_at = datetime.datetime.now(datetime.UTC).isoformat()
    return AuthResult(
        target_name=target.name,
        status=ExecutionStatus.ERROR,
        source="config",
        started_at=started_at,
        ended_at=started_at,
        duration_seconds=0.0,
        message=str(error),
    )


def _build_execution_context(
    *, target: TargetDescriptor, tasks: list[ResolvedTask], benchmark_enabled: bool
) -> ExecutionContext:
    if target.regions is None:
        raise ValueError("execution context requires resolved regions")
    return ExecutionContext(
        regions=target.regions,
        role_name=target.role_name,
        dry_run=target.dry_run,
        tasks=tasks,
        metadata=target.metadata,
        fail_fast=target.fail_fast,
        max_parallel_regions=target.max_parallel_regions,
        benchmark_enabled=benchmark_enabled,
    )


def prepare_target(
    *,
    index: int,
    target: TargetDescriptor,
    cli_dry_run: bool | None,
    cli_include: list[str] | None,
    cli_exclude: list[str] | None,
    organization_cache: OrganizationRunCache,
    auth_cache: AuthCheckCache,
    benchmark_enabled: bool = False,
) -> PreparedTarget:
    recorder = BenchmarkRecorder(enabled=benchmark_enabled)
    session_factory = SessionFactory()

    with recorder.phase("prepare_target_seconds"):
        provider = _load_provider(target.provider)
        auth_result: AuthResult = _run_dispatched_auth_check_for_target(
            target=target, auth_cache=auth_cache
        )

        try:
            effective_target: TargetDescriptor = _build_effective_target(
                target=target,
                cli_dry_run=cli_dry_run,
                cli_include=cli_include,
                cli_exclude=cli_exclude,
            )
            effective_include, effective_exclude = _resolve_effective_account_filters(
                target=target, cli_include=cli_include, cli_exclude=cli_exclude
            )
        except ValueError as error:
            return PreparedTarget(
                index=index,
                effective_target=target,
                auth_result=_auth_result_from_config_error(target=target, error=error),
                context=None,
                session_factory=session_factory,
                benchmark=recorder.data,
            )

        if auth_result.is_error:
            return PreparedTarget(
                index=index,
                effective_target=effective_target,
                auth_result=auth_result,
                context=None,
                session_factory=session_factory,
                effective_include=effective_include,
                effective_exclude=effective_exclude,
                benchmark=recorder.data,
            )

        regions = configured_or_default_regions(
            configured=effective_target.regions,
            default=provider.metadata.default_regions,
        )
        validate_resolved_regions(regions=regions)
        effective_target = replace(effective_target, regions=regions)

        with recorder.phase("resolve_tasks_seconds"):
            execution: ResolvedExecution = resolve_tasks(
                task_specs=effective_target.tasks,
                provider_name=effective_target.provider,
                supported_task_scopes=provider.metadata.supported_task_scopes,
            )
            tasks: list[ResolvedTask] = execution.ordered

        context: ExecutionContext = _build_execution_context(
            target=effective_target, tasks=tasks, benchmark_enabled=benchmark_enabled
        )

        provider_preflight: object | None = None
        preflight_error: str | None = None
        exclusive_execution_key: object | None = None
        exclusive_execution_keys: tuple[object, ...] = ()
        base_session: Session | None = None
        organization_id: str | None = None
        management_account_id: str | None = None
        base_session_account_id: str | None = None
        discovered_accounts: dict[str, dict[str, str]] | None = None
        region_statuses: dict[str, str] | None = None
        if (
            effective_target.provider == "aws"
            and effective_target.is_organization_config
        ):
            if not isinstance(provider, AwsProvider):
                raise TypeError("AWS target resolved to a non-AWS provider")

            preflight_result = provider.preflight_execution(
                target=effective_target,
                context=context,
                session_factory=session_factory,
                organization_cache=organization_cache,
                benchmark=recorder.data,
                organization_resolver_cls=OrganizationResolver,
            )
            provider_preflight = preflight_result.data
            exclusive_execution_key = preflight_result.exclusive_execution_key
            base_session = preflight_result.data.base_session
            organization_id = preflight_result.data.organization_id
            management_account_id = preflight_result.data.management_account_id
            base_session_account_id = preflight_result.data.base_session_account_id
            discovered_accounts = preflight_result.data.discovered_accounts
            region_statuses = preflight_result.data.region_statuses
        elif effective_target.provider == "azure":
            if not isinstance(provider, AzureProvider):
                raise TypeError("Azure target resolved to a non-Azure provider")

            try:
                preflight_result = provider.preflight_execution(
                    target=effective_target,
                    regions=context.regions,
                    include=effective_include,
                    exclude=effective_exclude,
                    benchmark=recorder.data,
                )
            except (RuntimeError, ValueError) as error:
                provider_preflight = None
                preflight_error = str(error)
                exclusive_execution_keys = ()
            else:
                provider_preflight = preflight_result.data
                exclusive_execution_keys = preflight_result.exclusive_execution_keys

    return PreparedTarget(
        index=index,
        effective_target=effective_target,
        auth_result=auth_result,
        context=context,
        session_factory=session_factory,
        provider_preflight=provider_preflight,
        preflight_error=preflight_error,
        exclusive_execution_key=exclusive_execution_key,
        exclusive_execution_keys=exclusive_execution_keys,
        base_session=base_session,
        organization_id=organization_id,
        management_account_id=management_account_id,
        base_session_account_id=base_session_account_id,
        discovered_accounts=discovered_accounts,
        region_statuses=region_statuses,
        effective_include=effective_include,
        effective_exclude=effective_exclude,
        benchmark=recorder.data,
    )


@dataclass(frozen=True, slots=True)
class _ProviderRegionOutcome:
    region: str
    task_results: list[TaskResult]
    failed: bool
    interrupted: bool
    duration_seconds: float


def _task_order(context: ExecutionContext) -> dict[str, int]:
    return {task.name: index for index, task in enumerate(context.tasks)}


def _execution_target_regions(
    *, execution_target: ExecutionTarget, context: ExecutionContext
) -> list[str]:
    provider_data = execution_target.provider_data
    locations = getattr(provider_data, "locations", None)
    if isinstance(locations, list) and all(
        isinstance(location, str) for location in locations
    ):
        return [location for location in locations if isinstance(location, str)]

    return list(context.regions)


def _execute_provider_region(
    *,
    execution_target: ExecutionTarget,
    runtime: ProviderExecutionRuntime,
    context: ExecutionContext,
    region: str,
    target_cancel_event: threading.Event,
    actions: ActionRecorder | None = None,
    tasks: list[ResolvedTask] | None = None,
    dependency_results: dict[str, TaskResult] | None = None,
) -> _ProviderRegionOutcome:
    region_started = time.perf_counter()
    session = runtime.build_session(region=region)
    if actions is None:
        actions = ActionRecorder(actions=[])
    task_results: list[TaskResult] = []
    region_task_results: dict[str, TaskResult] = dict(dependency_results or {})
    optional_map = {task.name: task.optional for task in context.tasks}
    interrupted = False

    for task in tasks if tasks is not None else context.tasks:
        if context.cancel_event.is_set() or target_cancel_event.is_set():
            interrupted = True
            break

        dependency_failed = any(
            region_task_results[dependency].status.is_error
            for dependency in task.depends_on
            if dependency in region_task_results
        )
        if dependency_failed:
            now_at = datetime.datetime.now(datetime.UTC).isoformat()
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
                break
            continue

        task_started_perf = time.perf_counter()
        task_started_at = datetime.datetime.now(datetime.UTC).isoformat()
        try:
            task_context = TaskCallContext(
                provider=execution_target.provider,
                execution_target_id=execution_target.id,
                execution_target_name=execution_target.name,
                execution_target_type=execution_target.type,
                region=region,
                session=session,
                dry_run=context.dry_run,
                metadata=context.metadata,
                actions=actions,
            )
            result = invoke_task(task.run, context=task_context)
        except Exception as error:
            task_ended_perf = time.perf_counter()
            task_ended_at = datetime.datetime.now(datetime.UTC).isoformat()
            task_result = TaskResult(
                task_name=task.name,
                region=region,
                status=ExecutionStatus.ERROR,
                started_at=task_started_at,
                ended_at=task_ended_at,
                duration_seconds=task_ended_perf - task_started_perf,
                error=str(error),
            )
            region_task_results[task.name] = task_result
            task_results.append(task_result)
            if not task.optional:
                break
            continue

        task_ended_perf = time.perf_counter()
        task_ended_at = datetime.datetime.now(datetime.UTC).isoformat()
        task_result = TaskResult(
            task_name=task.name,
            region=region,
            status=ExecutionStatus.SUCCESS,
            started_at=task_started_at,
            ended_at=task_ended_at,
            duration_seconds=task_ended_perf - task_started_perf,
            result=result,
        )
        region_task_results[task.name] = task_result
        task_results.append(task_result)

    failed = any(
        result.status.is_error and not optional_map.get(result.task_name, False)
        for result in region_task_results.values()
    )
    duration_seconds = time.perf_counter() - region_started
    runtime.record_region_outcome(
        region=region,
        duration_seconds=duration_seconds,
        failed=failed,
        interrupted=interrupted,
    )
    return _ProviderRegionOutcome(
        region=region,
        task_results=task_results,
        failed=failed,
        interrupted=interrupted,
        duration_seconds=duration_seconds,
    )


def _execute_provider_execution_target(
    *,
    provider: Provider,
    target: TargetDescriptor,
    execution_target: ExecutionTarget,
    context: ExecutionContext,
) -> EntityResult:
    started_perf = time.perf_counter()
    started_at = datetime.datetime.now(datetime.UTC).isoformat()
    task_results: list[TaskResult] = []
    runtime: ProviderExecutionRuntime | None = None
    benchmark: dict[str, object] | None = None
    try:
        runtime = provider.prepare_execution_runtime(
            target=target, execution_target=execution_target, context=context
        )
        regions = _execution_target_regions(
            execution_target=execution_target, context=context
        )
        target_tasks = [
            task for task in context.tasks if task.scope is TaskScope.TARGET
        ]
        region_tasks = [
            task for task in context.tasks if task.scope is TaskScope.REGION
        ]
        target_outcome: _ProviderRegionOutcome | None = None
        target_execution_seconds = 0.0
        if target_tasks:
            target_started = time.perf_counter()
            target_outcome = _execute_provider_region(
                execution_target=execution_target,
                runtime=runtime,
                context=context,
                region=regions[0],
                target_cancel_event=threading.Event(),
                actions=ActionRecorder(actions=[]),
                tasks=target_tasks,
            )
            target_execution_seconds = time.perf_counter() - target_started
            task_results.extend(target_outcome.task_results)

        region_started = time.perf_counter()
        if (region_tasks or not context.tasks) and not (
            target_outcome is not None
            and (target_outcome.failed or target_outcome.interrupted)
        ):
            region_outcomes = _execute_provider_regions(
                execution_target=execution_target,
                runtime=runtime,
                context=context,
                regions=regions,
                tasks=region_tasks,
                dependency_results={
                    result.task_name: result for result in target_outcome.task_results
                }
                if target_outcome is not None
                else None,
            )
        else:
            region_outcomes = []
        region_execution_seconds = time.perf_counter() - region_started
        benchmark = _provider_runtime_benchmark(
            runtime=runtime,
            region_outcomes=region_outcomes,
            region_execution_seconds=region_execution_seconds,
            target_outcome=target_outcome,
            target_execution_seconds=target_execution_seconds,
        )
        for outcome in region_outcomes:
            task_results.extend(outcome.task_results)

        region_order = {region: index for index, region in enumerate(regions)}
        task_order = _task_order(context)
        task_scope_order = {
            task.name: 0 if task.scope is TaskScope.TARGET else 1
            for task in context.tasks
        }
        task_results.sort(
            key=lambda result: (
                task_scope_order.get(result.task_name, 1),
                region_order.get(result.region, len(region_order)),
                task_order.get(result.task_name, len(task_order)),
            )
        )

        interrupted = (
            target_outcome.interrupted if target_outcome is not None else False
        ) or any(outcome.interrupted for outcome in region_outcomes)
        failed = (
            target_outcome.failed if target_outcome is not None else False
        ) or any(outcome.failed for outcome in region_outcomes)
        status = (
            ExecutionStatus.INTERRUPTED
            if interrupted
            else ExecutionStatus.ERROR
            if failed
            else ExecutionStatus.SUCCESS
        )
        error = None
    except Exception as runtime_error:
        status = ExecutionStatus.ERROR
        error = str(runtime_error)
    finally:
        if runtime is not None:
            runtime.close()

    ended_at = datetime.datetime.now(datetime.UTC).isoformat()
    return EntityResult(
        id=execution_target.id,
        name=execution_target.name,
        type=execution_target.type,
        status=status,
        started_at=started_at,
        ended_at=ended_at,
        duration_seconds=time.perf_counter() - started_perf,
        tasks=task_results,
        error=error,
        benchmark=benchmark,
    )


def _provider_runtime_benchmark(
    *,
    runtime: ProviderExecutionRuntime,
    region_outcomes: list[_ProviderRegionOutcome],
    region_execution_seconds: float,
    target_outcome: _ProviderRegionOutcome | None = None,
    target_execution_seconds: float = 0.0,
) -> dict[str, object] | None:
    benchmark = getattr(runtime, "benchmark", None)
    if callable(benchmark):
        benchmark = benchmark()
    if not isinstance(benchmark, dict):
        return None

    benchmark["region_execution_seconds"] = region_execution_seconds
    if target_outcome is not None:
        benchmark["target_execution_seconds"] = target_execution_seconds
        benchmark["target"] = {
            "region": target_outcome.region,
            "task_count": len(target_outcome.task_results),
            "interrupted": target_outcome.interrupted,
            "failed": target_outcome.failed,
        }
    benchmark["regions"] = {
        outcome.region: {
            "duration_seconds": outcome.duration_seconds,
            "task_count": len(outcome.task_results),
            "interrupted": outcome.interrupted,
            "failed": outcome.failed,
        }
        for outcome in region_outcomes
    }
    return benchmark


def _execute_provider_regions(
    *,
    execution_target: ExecutionTarget,
    runtime: ProviderExecutionRuntime,
    context: ExecutionContext,
    regions: list[str],
    tasks: list[ResolvedTask] | None = None,
    dependency_results: dict[str, TaskResult] | None = None,
) -> list[_ProviderRegionOutcome]:
    target_cancel_event = threading.Event()
    if context.max_parallel_regions == 1:
        return _execute_provider_regions_sequential(
            execution_target=execution_target,
            runtime=runtime,
            context=context,
            regions=regions,
            target_cancel_event=target_cancel_event,
            tasks=tasks,
            dependency_results=dependency_results,
        )

    return _execute_provider_regions_parallel(
        execution_target=execution_target,
        runtime=runtime,
        context=context,
        regions=regions,
        target_cancel_event=target_cancel_event,
        tasks=tasks,
        dependency_results=dependency_results,
    )


def _execute_provider_regions_sequential(
    *,
    execution_target: ExecutionTarget,
    runtime: ProviderExecutionRuntime,
    context: ExecutionContext,
    regions: list[str],
    target_cancel_event: threading.Event,
    tasks: list[ResolvedTask] | None = None,
    dependency_results: dict[str, TaskResult] | None = None,
) -> list[_ProviderRegionOutcome]:
    region_outcomes: list[_ProviderRegionOutcome] = []
    actions = ActionRecorder(actions=[])

    for region in regions:
        outcome = _execute_provider_region(
            execution_target=execution_target,
            runtime=runtime,
            context=context,
            region=region,
            target_cancel_event=target_cancel_event,
            actions=actions,
            tasks=tasks,
            dependency_results=dependency_results,
        )
        region_outcomes.append(outcome)

        if outcome.interrupted or outcome.failed:
            target_cancel_event.set()
            break

    return region_outcomes


def _execute_provider_regions_parallel(
    *,
    execution_target: ExecutionTarget,
    runtime: ProviderExecutionRuntime,
    context: ExecutionContext,
    regions: list[str],
    target_cancel_event: threading.Event,
    tasks: list[ResolvedTask] | None = None,
    dependency_results: dict[str, TaskResult] | None = None,
) -> list[_ProviderRegionOutcome]:
    pending_regions: deque[str] = deque(regions)
    active_futures: set[Future[_ProviderRegionOutcome]] = set()
    region_outcomes: list[_ProviderRegionOutcome] = []
    region_worker_limit = min(context.max_parallel_regions, len(regions))

    if region_worker_limit == 0:
        return region_outcomes

    with ThreadPoolExecutor(max_workers=region_worker_limit) as executor:
        while pending_regions or active_futures:
            while (
                pending_regions
                and not target_cancel_event.is_set()
                and not context.cancel_event.is_set()
                and len(active_futures) < region_worker_limit
            ):
                region = pending_regions.popleft()
                future = executor.submit(
                    _execute_provider_region,
                    execution_target=execution_target,
                    runtime=runtime,
                    context=context,
                    region=region,
                    target_cancel_event=target_cancel_event,
                    tasks=tasks,
                    dependency_results=dependency_results,
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

                region_outcomes.append(outcome)

                if outcome.interrupted or outcome.failed:
                    target_cancel_event.set()
                    pending_regions.clear()

            if target_cancel_event.is_set() or context.cancel_event.is_set():
                for future in active_futures:
                    future.cancel()

    return region_outcomes


def _execute_provider_targets(
    *,
    provider: Provider,
    target: TargetDescriptor,
    context: ExecutionContext,
    execution_targets: list[ExecutionTarget],
    benchmark_data: dict[str, object] | None,
) -> TargetResult:
    entity_results: list[EntityResult] = []
    recorder = BenchmarkRecorder(data=benchmark_data)

    with recorder.phase("entity_execution_seconds"):
        with ThreadPoolExecutor(max_workers=target.max_workers) as executor:
            futures: dict[Future[EntityResult], ExecutionTarget] = {
                executor.submit(
                    _execute_provider_execution_target,
                    provider=provider,
                    target=target,
                    execution_target=execution_target,
                    context=context,
                ): execution_target
                for execution_target in execution_targets
            }
            fail_fast_triggered = False

            try:
                for future in as_completed(futures):
                    try:
                        entity_result = future.result()
                    except CancelledError:
                        continue

                    entity_results.append(entity_result)
                    if (
                        context.fail_fast
                        and entity_result.status.is_unsuccessful
                        and not fail_fast_triggered
                    ):
                        context.cancel_event.set()
                        fail_fast_triggered = True
                        for pending in futures:
                            if not pending.done():
                                pending.cancel()
            except Exception:
                executor.shutdown(cancel_futures=True)
                raise

    entity_results.sort(key=lambda result: (result.name.lower(), result.id))

    if recorder.enabled:
        entity_execution_window_seconds = _entity_execution_window_seconds(
            entity_results
        )
        sum_entity_duration_seconds = sum(
            result.duration_seconds for result in entity_results
        )
        recorder.update(
            {
                "submitted_entity_count": len(execution_targets),
                "completed_entity_count": len(entity_results),
                "max_workers": target.max_workers,
                "entity_execution_window_seconds": entity_execution_window_seconds,
                "sum_entity_duration_seconds": sum_entity_duration_seconds,
                "max_entity_duration_seconds": max(
                    (result.duration_seconds for result in entity_results), default=0.0
                ),
                "worker_utilization": _entity_worker_utilization(
                    sum_entity_duration_seconds=sum_entity_duration_seconds,
                    max_workers=target.max_workers,
                    entity_execution_window_seconds=entity_execution_window_seconds,
                ),
                "max_parallel_regions": context.max_parallel_regions,
                "entity_region_limit": target.max_workers
                * context.max_parallel_regions,
            }
        )

    return TargetResult.create(
        config_branch=target.config_branch,
        target_name=target.name,
        dry_run=context.dry_run,
        entities=entity_results,
        benchmark=recorder.data,
    )


def _entity_execution_window_seconds(entity_results: list[EntityResult]) -> float:
    if not entity_results:
        return 0.0

    starts = [
        datetime.datetime.fromisoformat(result.started_at) for result in entity_results
    ]
    ends = [
        datetime.datetime.fromisoformat(result.ended_at) for result in entity_results
    ]
    return (max(ends) - min(starts)).total_seconds()


def _entity_worker_utilization(
    *,
    sum_entity_duration_seconds: float,
    max_workers: int,
    entity_execution_window_seconds: float,
) -> float:
    if max_workers <= 0 or entity_execution_window_seconds <= 0:
        return 0.0

    return sum_entity_duration_seconds / (max_workers * entity_execution_window_seconds)


def run_prepared_target(*, prepared_target: PreparedTarget) -> TargetExecutionOutcome:
    if prepared_target.context is None:
        raise ValueError("Prepared target is not runnable.")

    target: TargetDescriptor = prepared_target.effective_target
    context: ExecutionContext = prepared_target.context
    if prepared_target.preflight_error is not None:
        return TargetExecutionOutcome(
            index=prepared_target.index,
            target_result=TargetResult.create(
                config_branch=target.config_branch,
                target_name=target.name,
                dry_run=context.dry_run,
                entities=[],
                error=prepared_target.preflight_error,
            ),
            cancelled=context.cancel_event.is_set(),
        )

    benchmark_data = (
        dict(prepared_target.benchmark)
        if prepared_target.benchmark is not None
        else None
    )
    sink = BenchmarkRecorder(data=benchmark_data)

    provider = _load_provider(target.provider)
    try:
        with sink.phase("resolve_execution_targets_seconds"):
            if target.provider == "aws":
                if not isinstance(provider, AwsProvider):
                    raise TypeError("AWS target resolved to a non-AWS provider")

                execution_plan = provider.resolve_execution_targets(
                    target=target,
                    regions=context.regions,
                    include=prepared_target.effective_include,
                    exclude=prepared_target.effective_exclude,
                    preflight_data=prepared_target.provider_preflight,
                    organization_resolver_cls=OrganizationResolver,
                    account_resolver_cls=AccountResolver,
                )
            elif target.provider == "azure":
                if not isinstance(provider, AzureProvider):
                    raise TypeError("Azure target resolved to a non-Azure provider")

                execution_plan = provider.resolve_execution_targets(
                    target=target,
                    regions=context.regions,
                    include=prepared_target.effective_include,
                    exclude=prepared_target.effective_exclude,
                    preflight_data=prepared_target.provider_preflight,
                )
            else:
                execution_plan = provider.resolve_execution_targets(
                    target=target,
                    regions=context.regions,
                    include=prepared_target.effective_include,
                    exclude=prepared_target.effective_exclude,
                )
        sink.update(
            {
                "resolved_execution_target_count": len(
                    execution_plan.execution_targets
                ),
                "max_workers": target.max_workers,
                "max_parallel_regions": context.max_parallel_regions,
            }
        )
        target_result = _execute_provider_targets(
            provider=provider,
            target=target,
            execution_targets=execution_plan.execution_targets,
            benchmark_data=benchmark_data,
            context=context,
        )
    except Exception as error:
        if target.provider == "aws" and not isinstance(error, ValueError):
            raise

        target_result = TargetResult.create(
            config_branch=target.config_branch,
            target_name=target.name,
            dry_run=context.dry_run,
            entities=[],
            error=str(error),
        )

    return TargetExecutionOutcome(
        index=prepared_target.index,
        target_result=target_result,
        cancelled=context.cancel_event.is_set(),
    )


def _next_eligible_target(
    *, pending: deque[PreparedTarget], active_execution_keys: set[object]
) -> PreparedTarget | None:

    # Providers may allow matching targets in one YAML but require exclusive
    # execution. We enforce that only at execution admission so preparation can
    # still proceed in parallel.
    for offset, prepared_target in enumerate(pending):
        execution_keys = _prepared_target_execution_keys(prepared_target)
        if any(key in active_execution_keys for key in execution_keys):
            continue

        del pending[offset]
        return prepared_target

    return None


def _prepared_target_execution_keys(
    prepared_target: PreparedTarget,
) -> tuple[object, ...]:
    if prepared_target.exclusive_execution_keys:
        return prepared_target.exclusive_execution_keys

    execution_key = (
        prepared_target.exclusive_execution_key or prepared_target.organization_id
    )
    return () if execution_key is None else (execution_key,)


def run_auth_checks(*, targets: list[TargetDescriptor]) -> EngineResult:
    """
    Run authentication checks only. Does not resolve tasks or execute targets.
    """
    config_branch: ConfigBranch = (
        targets[0].config_branch if targets else ConfigBranch.TARGETS
    )
    auth_results: list[AuthResult] = []
    auth_cache = AuthCheckCache()

    with ThreadPoolExecutor(
        max_workers=max(1, min(DEFAULT_AUTH_CHECK_MAX_WORKERS, len(targets)))
    ) as executor:
        futures: list[Future[AuthResult]] = [
            executor.submit(
                _run_dispatched_auth_check_for_target,
                target=target,
                auth_cache=auth_cache,
            )
            for target in targets
        ]

        for target, future in zip(targets, futures, strict=True):
            auth_result = future.result()
            auth_results.append(auth_result)

    return EngineResult.create(
        config_branch=config_branch,
        state=_engine_state_from_auth_results(auth_results=auth_results),
        auth_results=auth_results,
        target_results=[],
    )


def _run_target_pipeline(
    *,
    targets: list[TargetDescriptor],
    max_parallel_targets: int,
    cli_dry_run: bool | None,
    cli_include: list[str] | None,
    cli_exclude: list[str] | None,
    benchmark_enabled: bool = False,
) -> tuple[list[AuthResult], list[TargetResult], EngineState]:

    # Preparation and execution complete out of order, but final EngineResult
    # output must stay in the original YAML input order.
    auth_results_by_index: dict[int, AuthResult] = {}
    target_results_by_index: dict[int, TargetResult] = {}
    execution_state = EngineState.COMPLETED_SUCCESS

    # Prepared targets wait here until an execution slot is free and any
    # same-organization exclusion has cleared.
    ready_targets: deque[PreparedTarget] = deque()
    active_execution_keys: set[object] = set()
    organization_cache = OrganizationRunCache()
    auth_cache = AuthCheckCache()

    if targets:
        worker_limit = max(1, min(max_parallel_targets, len(targets)))

        with (
            ThreadPoolExecutor(max_workers=worker_limit) as prepare_executor,
            ThreadPoolExecutor(max_workers=worker_limit) as execute_executor,
        ):
            preflight_futures: dict[Future[PreparedTarget], int] = {
                prepare_executor.submit(
                    prepare_target,
                    index=index,
                    target=target,
                    cli_dry_run=cli_dry_run,
                    cli_include=cli_include,
                    cli_exclude=cli_exclude,
                    organization_cache=organization_cache,
                    auth_cache=auth_cache,
                    benchmark_enabled=benchmark_enabled,
                ): index
                for index, target in enumerate(targets)
            }
            execution_futures: dict[Future[TargetExecutionOutcome], PreparedTarget] = {}

            # This loop coordinates two concurrent flows:
            # - preflight futures prepare targets and record auth results in input order
            # - execution futures run prepared targets as soon as scheduler capacity
            #   and same-org exclusion rules allow
            while preflight_futures or ready_targets or execution_futures:
                while len(execution_futures) < worker_limit:
                    next_target = _next_eligible_target(
                        pending=ready_targets,
                        active_execution_keys=active_execution_keys,
                    )
                    if next_target is None:
                        break

                    future = execute_executor.submit(
                        run_prepared_target, prepared_target=next_target
                    )
                    execution_futures[future] = next_target

                    active_execution_keys.update(
                        _prepared_target_execution_keys(next_target)
                    )

                waited_futures = set(preflight_futures) | set(execution_futures)
                if not waited_futures:
                    break

                done, _ = wait(waited_futures, return_when=FIRST_COMPLETED)
                for future in done:
                    if future in preflight_futures:
                        prepared_target = future.result()
                        auth_results_by_index[prepared_target.index] = (
                            prepared_target.auth_result
                        )
                        del preflight_futures[future]

                        if prepared_target.runnable:
                            ready_targets.append(prepared_target)
                        continue

                    prepared_target = execution_futures.pop(future)
                    active_execution_keys.difference_update(
                        _prepared_target_execution_keys(prepared_target)
                    )

                    outcome = future.result()
                    target_results_by_index[outcome.index] = outcome.target_result

                    if outcome.cancelled:
                        execution_state = _elevate_state(
                            execution_state, EngineState.CANCELLED
                        )
                    elif outcome.target_result.has_failures:
                        execution_state = _elevate_state(
                            execution_state, EngineState.COMPLETED_WITH_FAILURES
                        )

    auth_results = [
        auth_results_by_index[index]
        for index in range(len(targets))
        if index in auth_results_by_index
    ]
    target_results = [
        target_results_by_index[index]
        for index in range(len(targets))
        if index in target_results_by_index
    ]
    engine_state = _elevate_state(
        _engine_state_from_auth_results(auth_results=auth_results), execution_state
    )

    return auth_results, target_results, engine_state


def run_multiple_targets(
    *,
    targets: list[TargetDescriptor],
    max_parallel_targets: int,
    cli_dry_run: bool | None,
    cli_include: list[str] | None,
    cli_exclude: list[str] | None,
    benchmark_enabled: bool = False,
) -> EngineResult:
    config_branch: ConfigBranch = (
        targets[0].config_branch if targets else ConfigBranch.TARGETS
    )
    recorder = BenchmarkRecorder(enabled=benchmark_enabled)
    with recorder.phase("run_multiple_targets_seconds"):
        auth_results, target_results, engine_state = _run_target_pipeline(
            targets=targets,
            max_parallel_targets=max_parallel_targets,
            cli_dry_run=cli_dry_run,
            cli_include=cli_include,
            cli_exclude=cli_exclude,
            benchmark_enabled=benchmark_enabled,
        )
    recorder.update(
        {"max_parallel_targets": max_parallel_targets, "target_count": len(targets)}
    )

    return EngineResult.create(
        config_branch=config_branch,
        state=engine_state,
        auth_results=auth_results,
        target_results=target_results,
        benchmark=recorder.data,
    )
