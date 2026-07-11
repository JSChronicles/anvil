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
from anvil.account import Account
from anvil.account_resolver import AccountResolver
from anvil.actions import ActionRecorder
from anvil.auth import AuthSource, auth_check, infer_auth_source
from anvil.descriptors import ConfigBranch, TargetDescriptor
from anvil.execution_context import ExecutionContext
from anvil.executor import execute_accounts
from anvil.organization import OrganizationResolver
from anvil.providers.aws import AwsProvider
from anvil.provider_loader import list_providers
from anvil.providers.base import (
    ExecutionTarget,
    Provider,
    ProviderAuthResult,
    ProviderExecutionPlan,
    ProviderExecutionRuntime,
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
from anvil.task_loader import ResolvedExecution, ResolvedTask, resolve_tasks

__LOGGER__ = logging.getLogger(__name__)


STATE_PRECEDENCE: dict[EngineState, int] = {
    EngineState.AUTH_FAILED: 4,
    EngineState.CANCELLED: 3,
    EngineState.COMPLETED_WITH_FAILURES: 2,
    EngineState.COMPLETED_SUCCESS: 1,
}

DEFAULT_AUTH_CHECK_MAX_WORKERS = 4


def _load_provider(provider_name: str) -> Provider:
    if provider_name == "aws":
        return AwsProvider()

    for descriptor in list_providers():
        if descriptor.name == provider_name:
            return descriptor.load()

    raise ValueError(f"Unknown provider '{provider_name}'")


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
    entry: OrganizationRunCacheEntry
    hit: bool
    waited: bool


class OrganizationRunCache:
    def __init__(self) -> None:
        self._cache = _SingleFlightCache()

    def get_or_discover(
        self, *, organization_id: str, discover: Callable[[], OrganizationRunCacheEntry]
    ) -> _OrganizationRunCacheLookup:
        entry, hit, waited = self._cache.get_or_create(
            key=organization_id, create=discover
        )
        if not isinstance(entry, OrganizationRunCacheEntry):
            raise RuntimeError("Organization discovery cache returned unexpected value")

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


def _preflight_organization(
    *,
    target: TargetDescriptor,
    context: ExecutionContext,
    session_factory: SessionFactory,
    organization_cache: OrganizationRunCache,
    benchmark: dict[str, object] | None = None,
) -> tuple[Session, str, str, str, dict[str, dict[str, str]], dict[str, str]]:
    sink = BenchmarkRecorder(data=benchmark)
    aws_provider = AwsProvider()

    with sink.phase("create_base_session_seconds"):
        base_session: Session = session_factory.create_base_session(
            profile_name=target.profile,
            region_name=aws_provider.bootstrap_region(
                configured_regions=context.regions
            ),
        )

    with sink.phase("describe_organization_seconds"):
        organization_id, management_account_id = (
            OrganizationResolver.describe_organization(base_session)
        )

    with sink.phase("describe_base_session_account_seconds"):
        base_session_account_id = OrganizationResolver.describe_base_session_account(
            base_session
        )

    def discover_organization() -> OrganizationRunCacheEntry:
        with sink.phase("discover_accounts_seconds"):
            discovered_accounts = OrganizationResolver.discover_accounts(base_session)

        with sink.phase("discover_region_statuses_seconds"):
            region_statuses = aws_provider.discover_region_statuses(
                session=base_session
            )

        return OrganizationRunCacheEntry(
            management_account_id=management_account_id,
            discovered_accounts=discovered_accounts,
            region_statuses=region_statuses,
        )

    lookup = organization_cache.get_or_discover(
        organization_id=organization_id, discover=discover_organization
    )
    sink.set("organization_cache_hit", lookup.hit)
    sink.set("organization_cache_waited", lookup.waited)

    return (
        base_session,
        organization_id,
        lookup.entry.management_account_id,
        base_session_account_id,
        lookup.entry.discovered_accounts,
        lookup.entry.region_statuses,
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

        with recorder.phase("resolve_tasks_seconds"):
            execution: ResolvedExecution = resolve_tasks(
                task_specs=effective_target.tasks,
                provider_name=effective_target.provider,
            )
            tasks: list[ResolvedTask] = execution.ordered

        regions = provider.default_regions(effective_target)
        effective_target = replace(effective_target, regions=regions)
        context: ExecutionContext = _build_execution_context(
            target=effective_target, tasks=tasks, benchmark_enabled=benchmark_enabled
        )

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
            (
                base_session,
                organization_id,
                management_account_id,
                base_session_account_id,
                discovered_accounts,
                region_statuses,
            ) = _preflight_organization(
                target=effective_target,
                context=context,
                session_factory=session_factory,
                organization_cache=organization_cache,
                benchmark=recorder.data,
            )

    return PreparedTarget(
        index=index,
        effective_target=effective_target,
        auth_result=auth_result,
        context=context,
        session_factory=session_factory,
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
        return list(locations)

    return list(context.regions)


def _execute_provider_region(
    *,
    execution_target: ExecutionTarget,
    runtime: ProviderExecutionRuntime,
    context: ExecutionContext,
    region: str,
) -> _ProviderRegionOutcome:
    region_started = time.perf_counter()
    session = runtime.build_session(region=region)
    actions = ActionRecorder(actions=[])
    task_results: list[TaskResult] = []
    region_task_results: dict[str, TaskResult] = {}
    optional_map = {task.name: task.optional for task in context.tasks}
    interrupted = False

    for task in context.tasks:
        if context.cancel_event.is_set():
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
    try:
        runtime = provider.prepare_execution_runtime(
            target=target, execution_target=execution_target, context=context
        )
        regions = _execution_target_regions(
            execution_target=execution_target, context=context
        )
        region_outcomes = [
            _execute_provider_region(
                execution_target=execution_target,
                runtime=runtime,
                context=context,
                region=region,
            )
            for region in regions
        ]
        for outcome in region_outcomes:
            task_results.extend(outcome.task_results)

        region_order = {region: index for index, region in enumerate(regions)}
        task_order = _task_order(context)
        task_results.sort(
            key=lambda result: (
                region_order.get(result.region, len(region_order)),
                task_order.get(result.task_name, len(task_order)),
            )
        )

        interrupted = any(outcome.interrupted for outcome in region_outcomes)
        failed = any(outcome.failed for outcome in region_outcomes)
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
    )


def _execute_provider_targets(
    *,
    provider: Provider,
    target: TargetDescriptor,
    context: ExecutionContext,
    execution_targets: list[ExecutionTarget],
    benchmark_data: dict[str, object] | None,
) -> TargetResult:
    entity_results: list[EntityResult] = []
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

    entity_results.sort(key=lambda result: (result.name.lower(), result.id))
    return TargetResult.create(
        config_branch=target.config_branch,
        target_name=target.name,
        dry_run=context.dry_run,
        entities=entity_results,
        benchmark=benchmark_data,
    )


def run_prepared_target(*, prepared_target: PreparedTarget) -> TargetExecutionOutcome:
    if prepared_target.context is None:
        raise ValueError("Prepared target is not runnable.")

    target: TargetDescriptor = prepared_target.effective_target
    context: ExecutionContext = prepared_target.context
    benchmark_data = (
        dict(prepared_target.benchmark)
        if prepared_target.benchmark is not None
        else None
    )
    sink = BenchmarkRecorder(data=benchmark_data)

    if target.provider == "aws":
        try:
            aws_provider = AwsProvider()
            with sink.phase("resolve_accounts_seconds"):
                execution_plan: ProviderExecutionPlan = (
                    aws_provider.resolve_execution_targets(
                        target=target,
                        regions=context.regions,
                        include=target.include,
                        exclude=target.exclude,
                        session_factory=prepared_target.session_factory,
                        base_session=prepared_target.base_session,
                        organization_id=prepared_target.organization_id,
                        management_account_id=prepared_target.management_account_id,
                        base_session_account_id=prepared_target.base_session_account_id,
                        discovered_accounts=prepared_target.discovered_accounts,
                        region_statuses=prepared_target.region_statuses,
                        organization_resolver_cls=OrganizationResolver,
                        account_resolver_cls=AccountResolver,
                    )
                )
                accounts: list[Account] = aws_provider.accounts_from_execution_targets(
                    execution_targets=execution_plan.execution_targets, context=context
                )

            sink.update(
                {
                    "resolved_account_count": len(accounts),
                    "max_workers": target.max_workers,
                    "max_parallel_regions": context.max_parallel_regions,
                    "account_region_limit": (
                        target.max_workers * context.max_parallel_regions
                    ),
                }
            )

            account_region_limit = target.max_workers * context.max_parallel_regions
            __LOGGER__.info(
                f"Target '{target.name}' concurrency: "
                f"max_workers={target.max_workers}, "
                f"max_parallel_regions={context.max_parallel_regions}, "
                f"account_region_limit={account_region_limit}"
            )
            target_result: TargetResult = execute_accounts(
                name=target.name,
                config_branch=target.config_branch,
                max_workers=target.max_workers,
                context=context,
                accounts=accounts,
                benchmark_enabled=sink.enabled,
                benchmark=benchmark_data,
            )
        except ValueError as error:
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

    provider = _load_provider(target.provider)
    try:
        with sink.phase("resolve_execution_targets_seconds"):
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
    *, pending: deque[PreparedTarget], active_organization_ids: set[str]
) -> PreparedTarget | None:

    # Same-org targets may coexist in one YAML, but they must not execute at the
    # same time. We enforce that only at execution admission so preparation can
    # still proceed in parallel.
    for offset, prepared_target in enumerate(pending):
        organization_id = prepared_target.organization_id
        if organization_id is not None and organization_id in active_organization_ids:
            continue

        del pending[offset]
        return prepared_target

    return None


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
    active_organization_ids: set[str] = set()
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
                        active_organization_ids=active_organization_ids,
                    )
                    if next_target is None:
                        break

                    future = execute_executor.submit(
                        run_prepared_target, prepared_target=next_target
                    )
                    execution_futures[future] = next_target

                    if next_target.organization_id is not None:
                        active_organization_ids.add(next_target.organization_id)

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
                    if prepared_target.organization_id is not None:
                        active_organization_ids.discard(prepared_target.organization_id)

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
