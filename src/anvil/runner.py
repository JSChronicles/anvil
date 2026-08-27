from __future__ import annotations

import datetime
import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Collection
from concurrent.futures import (
    FIRST_COMPLETED,
    CancelledError,
    Future,
    ThreadPoolExecutor,
    wait,
)
from dataclasses import dataclass, field, replace
from typing import cast

from anvil.benchmark import BenchmarkRecorder
from anvil.actions import ActionRecorder
from anvil.descriptors import TargetDescriptor
from anvil.execution_context import ExecutionContext
from anvil.provider_lifecycle import CoordinateLifecycleState
from anvil.provider_loader import load_provider
from anvil.providers.base import (
    ConfiguredTargetProvider,
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
    aggregate_execution_statuses,
)
from anvil.task_context import (
    TaskCallContext,
    merge_task_metadata,
    resolve_dependency_data,
)
from anvil.task_errors import TaskExecutionError
from anvil.task_loader import (
    ResolvedExecution,
    ResolvedTask,
    TaskConfigError,
    TaskScope,
    resolve_tasks,
)
from anvil.task_planner import TaskInstance, plan_task_instances
from anvil.task_scheduler import (
    DependencyResults,
    ScheduledTaskResult,
    TaskInstanceEligibility,
    execute_task_instance_plan,
    task_dependency_eligibility,
)

__LOGGER__ = logging.getLogger(__name__)


STATE_PRECEDENCE: dict[EngineState, int] = {
    EngineState.AUTH_FAILED: 4,
    EngineState.COMPLETED_WITH_FAILURES: 3,
    EngineState.CANCELLED: 2,
    EngineState.COMPLETED_SUCCESS: 1,
}

DEFAULT_AUTH_CHECK_MAX_WORKERS = 4
_SESSION_NOT_CREATED = object()


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
    provider: Provider
    effective_target: TargetDescriptor
    auth_result: AuthResult
    context: ExecutionContext | None
    provider_preflight: object | None = None
    preflight_error: str | None = None
    exclusive_execution_keys: tuple[object, ...] = ()
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
        self, *, key: object, check: Callable[[], AuthCheckOutcome]
    ) -> _AuthCheckCacheLookup:
        outcome, hit, waited = self._cache.get_or_create(key=key, create=check)
        if not isinstance(outcome, AuthCheckOutcome):
            raise RuntimeError("Auth check cache returned unexpected value")

        return _AuthCheckCacheLookup(outcome=outcome, hit=hit, waited=waited)


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


def _run_provider_auth_check_for_target(
    *, provider: Provider, target: TargetDescriptor, validate_target: bool = True
) -> AuthResult:
    started_perf = time.perf_counter()
    started_at = datetime.datetime.now(datetime.UTC).isoformat()
    try:
        if validate_target:
            provider.validate_target(target)
        provider_result = provider.auth_check(target)
    except Exception as error:
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


def _run_cached_provider_auth_check_for_target(
    *,
    provider: Provider,
    target: TargetDescriptor,
    auth_cache: AuthCheckCache,
    validate_target: bool = True,
) -> AuthResult:
    cache_key = provider.auth_cache_key(target)
    if cache_key is None:
        return _run_provider_auth_check_for_target(
            provider=provider, target=target, validate_target=validate_target
        )

    def check() -> AuthCheckOutcome:
        return _auth_outcome_from_result(
            _run_provider_auth_check_for_target(
                provider=provider, target=target, validate_target=validate_target
            )
        )

    lookup = auth_cache.get_or_check(key=cache_key, check=check)
    return _auth_result_from_outcome(
        target_name=target.name, outcome=lookup.outcome, cached=lookup.hit
    )


def _build_effective_target(
    *,
    provider: Provider,
    target: TargetDescriptor,
    cli_dry_run: bool | None,
    cli_include: list[str] | None,
    cli_exclude: list[str] | None,
) -> TargetDescriptor:
    effective_dry_run: bool = cli_dry_run if cli_dry_run is not None else target.dry_run
    effective_include, effective_exclude = provider.resolve_target_filters(
        target=target, include_override=cli_include, exclude_override=cli_exclude
    )

    return replace(
        target,
        dry_run=effective_dry_run,
        include=effective_include,
        exclude=effective_exclude,
    )


def _auth_result_from_config_error(
    *, target: TargetDescriptor, error: Exception
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
    preparation_cache: _SingleFlightCache,
    auth_cache: AuthCheckCache,
    benchmark_enabled: bool = False,
) -> PreparedTarget:
    recorder = BenchmarkRecorder(enabled=benchmark_enabled)
    with recorder.phase("prepare_target_seconds"):
        provider = _load_provider(target.provider)

        try:
            effective_target: TargetDescriptor = _build_effective_target(
                provider=provider,
                target=target,
                cli_dry_run=cli_dry_run,
                cli_include=cli_include,
                cli_exclude=cli_exclude,
            )
            provider.validate_target(effective_target)

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

            configured_provider = cast(ConfiguredTargetProvider, provider)
            if any(task.scope is TaskScope.CONFIGURED_TARGET for task in tasks):
                configured_provider.validate_task_configuration(
                    target=effective_target,
                    task_scopes={task.id: task.scope.value for task in tasks},
                )
        except (TaskConfigError, TypeError, ValueError) as error:
            return PreparedTarget(
                index=index,
                provider=provider,
                effective_target=target,
                auth_result=_auth_result_from_config_error(target=target, error=error),
                context=None,
                benchmark=recorder.data,
            )

        effective_include = effective_target.include
        effective_exclude = effective_target.exclude
        auth_result: AuthResult = _run_cached_provider_auth_check_for_target(
            provider=provider,
            target=effective_target,
            auth_cache=auth_cache,
            validate_target=False,
        )

        if auth_result.is_error:
            return PreparedTarget(
                index=index,
                provider=provider,
                effective_target=effective_target,
                auth_result=auth_result,
                context=None,
                effective_include=effective_include,
                effective_exclude=effective_exclude,
                benchmark=recorder.data,
            )

        context: ExecutionContext = _build_execution_context(
            target=effective_target, tasks=tasks, benchmark_enabled=benchmark_enabled
        )

        provider_preflight: object | None = None
        preflight_error: str | None = None
        exclusive_execution_keys: tuple[object, ...] = ()
        try:
            preparation = provider.prepare_target(
                target=effective_target,
                context=context,
                include=effective_include,
                exclude=effective_exclude,
                cache=preparation_cache,
                benchmark=recorder.data,
            )
        except Exception as error:
            preflight_error = str(error)
        else:
            provider_preflight = preparation.data
            exclusive_execution_keys = preparation.exclusive_execution_keys

    return PreparedTarget(
        index=index,
        provider=provider,
        effective_target=effective_target,
        auth_result=auth_result,
        context=context,
        provider_preflight=provider_preflight,
        preflight_error=preflight_error,
        exclusive_execution_keys=exclusive_execution_keys,
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
    activated_task_ids: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class _OrdinaryTaskExecution:
    """Task results and runtime metrics for one ordinary execution target."""

    task_results: list[TaskResult]
    benchmark: dict[str, object] | None


def _aggregate_task_result_status(task_results: list[TaskResult]) -> ExecutionStatus:
    """Aggregate task results while preserving cancellation-only outcomes."""

    status = aggregate_execution_statuses(
        [task_result.status for task_result in task_results]
    )
    if status is ExecutionStatus.SUCCESS and any(
        task_result.status.is_skipped
        and task_result.skip_reason == "cancelled_before_start"
        for task_result in task_results
    ):
        return ExecutionStatus.INTERRUPTED
    return status


def _provider_region_outcome(
    *,
    region: str,
    task_results: list[TaskResult],
    duration_seconds: float,
    status_results: Collection[TaskResult] | None = None,
    interrupted: bool = False,
    activated_task_ids: frozenset[str] = frozenset(),
) -> _ProviderRegionOutcome:
    """Build one provider lifecycle outcome with shared status semantics."""

    aggregate_results = task_results if status_results is None else status_results
    return _ProviderRegionOutcome(
        region=region,
        task_results=task_results,
        failed=any(result.status.is_error for result in aggregate_results),
        interrupted=interrupted
        or any(
            result.status.is_interrupted
            or (
                result.status.is_skipped
                and result.skip_reason == "cancelled_before_start"
            )
            for result in aggregate_results
        ),
        duration_seconds=duration_seconds,
        activated_task_ids=activated_task_ids,
    )


def _task_eligibility(
    *,
    task: ResolvedTask,
    task_results: dict[str, TaskResult],
    activated_task_ids: set[str],
    stop_reason: str | None,
) -> TaskInstanceEligibility:
    """Return the shared execution and finalizer-activation decision."""

    missing_dependencies = [
        dependency for dependency in task.depends_on if dependency not in task_results
    ]
    if missing_dependencies:
        dependencies = ", ".join(missing_dependencies)
        raise RuntimeError(
            f"Task '{task.id}' dependencies have not settled: {dependencies}"
        )

    chain_activated = not task.depends_on or any(
        dependency in activated_task_ids for dependency in task.depends_on
    )
    return task_dependency_eligibility(
        task=task,
        dependency_results=[task_results[dependency] for dependency in task.depends_on],
        chain_activated=chain_activated,
        stop_reason=stop_reason,
    )


def _skipped_task_result(
    *, task: ResolvedTask, region: str, skip_reason: str
) -> TaskResult:
    """Create a zero-duration result for settled unstarted work."""

    now_at = datetime.datetime.now(datetime.UTC).isoformat()
    return TaskResult(
        task_id=task.id,
        task_name=task.name,
        region=region,
        status=ExecutionStatus.SKIPPED,
        started_at=now_at,
        ended_at=now_at,
        duration_seconds=0.0,
        skip_reason=skip_reason,
    )


def _run_task_instance(
    *,
    instance: TaskInstance,
    dependency_results: DependencyResults,
    session: object,
    context: ExecutionContext,
) -> TaskResult:
    """Invoke one planned task instance with isolated provider-neutral inputs."""

    return _invoke_task(
        task=instance.task,
        execution_target=instance.execution_target,
        region=instance.region,
        session=session,
        context=context,
        dependency_results=dependency_results,
    )


def _invoke_task(
    *,
    task: ResolvedTask,
    execution_target: ExecutionTarget,
    region: str,
    session: object,
    context: ExecutionContext,
    dependency_results: DependencyResults,
) -> TaskResult:
    """Invoke one task and return its complete terminal result."""

    task_started_perf = time.perf_counter()
    task_started_at = datetime.datetime.now(datetime.UTC).isoformat()
    actions = ActionRecorder(actions=[])
    try:
        result = task.run(
            **TaskCallContext(
                provider=execution_target.provider,
                execution_target_id=execution_target.id,
                execution_target_name=execution_target.name,
                execution_target_type=execution_target.type,
                region=region,
                session=session,
                dry_run=context.dry_run,
                metadata=merge_task_metadata(
                    target_metadata=context.metadata, task_metadata=task.metadata
                ),
                dependency_data=resolve_dependency_data(
                    references=task.dependency_data,
                    dependency_results=dependency_results,
                ),
                actions=actions,
            ).to_kwargs()
        )
        status = ExecutionStatus.SUCCESS
        error_message = None
        task_data = result
    except Exception as task_error:
        status = ExecutionStatus.ERROR
        error_message = str(task_error)
        task_data = (
            task_error.partial_result
            if isinstance(task_error, TaskExecutionError)
            else None
        )

    return TaskResult(
        task_id=task.id,
        task_name=task.name,
        region=region,
        status=status,
        started_at=task_started_at,
        ended_at=datetime.datetime.now(datetime.UTC).isoformat(),
        duration_seconds=time.perf_counter() - task_started_perf,
        result=task_data,
        error=error_message,
        actions=list(actions.actions),
    )


def _execution_target_regions(
    *, execution_target: ExecutionTarget, context: ExecutionContext
) -> list[str]:
    validate_resolved_regions(regions=execution_target.regions)
    return list(execution_target.regions)


def _requires_task_instance_scheduler(tasks: list[ResolvedTask]) -> bool:
    """Return whether ordinary tasks contain a narrow-to-broad barrier."""

    depends_on_region: dict[str, bool] = {}
    for task in tasks:
        has_region_ancestor = any(
            depends_on_region.get(dependency_id, False)
            for dependency_id in task.depends_on
        )
        if task.scope is TaskScope.TARGET and has_region_ancestor:
            return True
        depends_on_region[task.id] = (
            task.scope is TaskScope.REGION or has_region_ancestor
        )
    return False


def _task_result_barrier_stages(tasks: list[ResolvedTask]) -> dict[str, int]:
    """Return stable result-order stages for topologically ordered tasks.

    A dependency from a narrower scope to a broader scope creates a fan-in
    barrier. Advancing the broader consumer to the next stage keeps every
    producer ahead of it while preserving the established scope-first,
    region-major ordering within each stage.

    Args:
        tasks: Resolved tasks in dependency order.

    Returns:
        Result-order stage keyed by effective task invocation ID.
    """

    scope_breadth = {
        TaskScope.REGION: 0,
        TaskScope.TARGET: 1,
        TaskScope.CONFIGURED_TARGET: 2,
    }
    task_scopes: dict[str, TaskScope] = {}
    stages: dict[str, int] = {}
    for task in tasks:
        stages[task.id] = max(
            (
                stages[dependency_id]
                + int(
                    scope_breadth[task.scope]
                    > scope_breadth[task_scopes[dependency_id]]
                )
                for dependency_id in task.depends_on
            ),
            default=0,
        )
        task_scopes[task.id] = task.scope

    return stages


def _execute_provider_region(
    *,
    execution_target: ExecutionTarget,
    runtime: ProviderExecutionRuntime,
    context: ExecutionContext,
    region: str,
    target_cancel_event: threading.Event,
    tasks: list[ResolvedTask] | None = None,
    dependency_results: dict[str, TaskResult] | None = None,
    dependency_activated_task_ids: frozenset[str] | None = None,
    initial_stop_reason: str | None = None,
    lazy_session: bool = False,
) -> _ProviderRegionOutcome:
    region_started = time.perf_counter()
    session = (
        _SESSION_NOT_CREATED if lazy_session else runtime.build_session(region=region)
    )
    task_results: list[TaskResult] = []
    region_task_results: dict[str, TaskResult] = dict(dependency_results or {})
    activated_task_ids = (
        {
            task_id
            for task_id, result in region_task_results.items()
            if not result.status.is_skipped
            or result.skip_reason == "dependency_unsuccessful"
        }
        if dependency_activated_task_ids is None
        else set(dependency_activated_task_ids)
    )
    interrupted = initial_stop_reason == "cancelled_before_start"
    stop_reason = initial_stop_reason

    for task in tasks if tasks is not None else context.tasks:
        if stop_reason is None:
            if context.cancel_event.is_set() or target_cancel_event.is_set():
                interrupted = True
                stop_reason = "cancelled_before_start"
            elif context.fail_fast_event.is_set():
                stop_reason = "fail_fast"

        eligibility = _task_eligibility(
            task=task,
            task_results=region_task_results,
            activated_task_ids=activated_task_ids,
            stop_reason=stop_reason,
        )
        if not eligibility.should_run:
            skipped_result = _skipped_task_result(
                task=task,
                region=region,
                skip_reason=eligibility.skip_reason or "dependency_unsuccessful",
            )
            region_task_results[task.id] = skipped_result
            task_results.append(skipped_result)
            if eligibility.chain_activated:
                activated_task_ids.add(task.id)
            continue

        activated_task_ids.add(task.id)
        if session is _SESSION_NOT_CREATED:
            session = runtime.build_session(region=region)
        task_result = _invoke_task(
            task=task,
            execution_target=execution_target,
            region=region,
            session=session,
            context=context,
            dependency_results=region_task_results,
        )
        region_task_results[task.id] = task_result
        task_results.append(task_result)
        if (
            context.fail_fast
            and task_result.status.is_unsuccessful
            and stop_reason is None
        ):
            stop_reason = "fail_fast"

    duration_seconds = time.perf_counter() - region_started
    if session is not _SESSION_NOT_CREATED:
        runtime.record_region_outcome(
            region=region,
            duration_seconds=duration_seconds,
            failed=any(
                result.status.is_error for result in region_task_results.values()
            ),
            interrupted=interrupted,
        )
    return _provider_region_outcome(
        region=region,
        task_results=task_results,
        status_results=region_task_results.values(),
        interrupted=interrupted,
        duration_seconds=duration_seconds,
        activated_task_ids=frozenset(activated_task_ids),
    )


def _execute_ordinary_tasks_fast(
    *,
    execution_target: ExecutionTarget,
    runtime: ProviderExecutionRuntime,
    context: ExecutionContext,
    regions: list[str],
    tasks: list[ResolvedTask],
) -> _OrdinaryTaskExecution:
    """Preserve region-oriented execution when no fan-in barrier is required."""

    target_tasks = [task for task in tasks if task.scope is TaskScope.TARGET]
    region_tasks = [task for task in tasks if task.scope is TaskScope.REGION]
    task_results: list[TaskResult] = []
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
            tasks=target_tasks,
        )
        target_execution_seconds = time.perf_counter() - target_started
        task_results.extend(target_outcome.task_results)

    region_started = time.perf_counter()
    has_regional_finalizers = any(task.always_run for task in region_tasks)
    target_stop_reason = (
        "cancelled_before_start"
        if (has_regional_finalizers and context.cancel_event.is_set())
        or (target_outcome is not None and target_outcome.interrupted)
        else (
            "fail_fast"
            if (has_regional_finalizers and context.fail_fast_event.is_set())
            or (
                target_outcome is not None
                and context.fail_fast
                and target_outcome.failed
            )
            else None
        )
    )
    if region_tasks:
        region_outcomes = _execute_provider_regions(
            execution_target=execution_target,
            runtime=runtime,
            context=context,
            regions=regions,
            tasks=region_tasks,
            dependency_results=dict(
                zip(
                    (task.id for task in target_tasks),
                    target_outcome.task_results,
                    strict=True,
                )
            )
            if target_outcome is not None
            else None,
            dependency_activated_task_ids=(
                target_outcome.activated_task_ids
                if target_outcome is not None
                else frozenset()
            ),
            initial_stop_reason=target_stop_reason,
        )
    else:
        region_outcomes = []
    region_execution_seconds = time.perf_counter() - region_started
    for outcome in region_outcomes:
        task_results.extend(outcome.task_results)

    region_order = {region: index for index, region in enumerate(regions)}
    task_order = {task.id: index for index, task in enumerate(tasks)}
    task_scope_order = {
        task.id: 0 if task.scope is TaskScope.TARGET else 1 for task in tasks
    }
    task_results.sort(
        key=lambda result: (
            task_scope_order.get(result.task_id, 1),
            region_order.get(result.region, len(region_order)),
            task_order.get(result.task_id, len(task_order)),
        )
    )

    return _OrdinaryTaskExecution(
        task_results=task_results,
        benchmark=_provider_runtime_benchmark(
            runtime=runtime,
            region_outcomes=region_outcomes,
            region_execution_seconds=region_execution_seconds,
            target_outcome=target_outcome,
            target_execution_seconds=target_execution_seconds,
        ),
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
    ordinary_tasks = [
        task for task in context.tasks if task.scope is not TaskScope.CONFIGURED_TARGET
    ]
    if _requires_task_instance_scheduler(ordinary_tasks):
        try:
            graph_result = _execute_provider_task_graph(
                provider=provider,
                target=target,
                context=context,
                execution_targets=[execution_target],
                configured_execution_target=None,
                benchmark_data=None,
            )
            return graph_result.entities[0]
        except Exception as runtime_error:
            ended_at = datetime.datetime.now(datetime.UTC).isoformat()
            return EntityResult(
                id=execution_target.id,
                name=execution_target.name,
                type=execution_target.type,
                provider=execution_target.provider,
                metadata=dict(execution_target.metadata),
                status=ExecutionStatus.ERROR,
                started_at=started_at,
                ended_at=ended_at,
                duration_seconds=time.perf_counter() - started_perf,
                tasks=[],
                error=str(runtime_error),
            )

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
        if not ordinary_tasks:
            region_started = time.perf_counter()
            region_outcomes = _execute_provider_regions(
                execution_target=execution_target,
                runtime=runtime,
                context=context,
                regions=regions,
                tasks=[],
            )
            region_execution_seconds = time.perf_counter() - region_started
            benchmark = _provider_runtime_benchmark(
                runtime=runtime,
                region_outcomes=region_outcomes,
                region_execution_seconds=region_execution_seconds,
            )
        else:
            ordinary_execution = _execute_ordinary_tasks_fast(
                execution_target=execution_target,
                runtime=runtime,
                context=context,
                regions=regions,
                tasks=ordinary_tasks,
            )
            task_results = ordinary_execution.task_results
            benchmark = ordinary_execution.benchmark
        status = _aggregate_task_result_status(task_results)
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
        provider=execution_target.provider,
        metadata=dict(execution_target.metadata),
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

    return _augment_provider_runtime_benchmark(
        benchmark=benchmark,
        region_outcomes=region_outcomes,
        region_execution_seconds=region_execution_seconds,
        target_outcome=target_outcome,
        target_execution_seconds=target_execution_seconds,
    )


def _augment_provider_runtime_benchmark(
    *,
    benchmark: dict[str, object],
    region_outcomes: list[_ProviderRegionOutcome],
    region_execution_seconds: float,
    target_outcome: _ProviderRegionOutcome | None = None,
    target_execution_seconds: float = 0.0,
) -> dict[str, object]:
    """Attach engine-owned execution timings to provider benchmark data."""

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


def _settled_provider_region_outcomes(
    *, regions: list[str], tasks: list[ResolvedTask], skip_reason: str
) -> list[_ProviderRegionOutcome]:
    """Settle unstarted region tasks without constructing runtime sessions."""

    return [
        _provider_region_outcome(
            region=region,
            task_results=[
                _skipped_task_result(task=task, region=region, skip_reason=skip_reason)
                for task in tasks
            ],
            interrupted=skip_reason == "cancelled_before_start",
            duration_seconds=0.0,
        )
        for region in regions
    ]


def _execute_provider_regions(
    *,
    execution_target: ExecutionTarget,
    runtime: ProviderExecutionRuntime,
    context: ExecutionContext,
    regions: list[str],
    tasks: list[ResolvedTask] | None = None,
    dependency_results: dict[str, TaskResult] | None = None,
    dependency_activated_task_ids: frozenset[str] | None = None,
    initial_stop_reason: str | None = None,
) -> list[_ProviderRegionOutcome]:
    target_cancel_event = threading.Event()
    if context.max_parallel_regions == 1:
        outcomes = _execute_provider_regions_sequential(
            execution_target=execution_target,
            runtime=runtime,
            context=context,
            regions=regions,
            target_cancel_event=target_cancel_event,
            tasks=tasks,
            dependency_results=dependency_results,
            dependency_activated_task_ids=dependency_activated_task_ids,
            initial_stop_reason=initial_stop_reason,
        )
    else:
        outcomes = _execute_provider_regions_parallel(
            execution_target=execution_target,
            runtime=runtime,
            context=context,
            regions=regions,
            target_cancel_event=target_cancel_event,
            tasks=tasks,
            dependency_results=dependency_results,
            dependency_activated_task_ids=dependency_activated_task_ids,
            initial_stop_reason=initial_stop_reason,
        )

    completed_regions = {outcome.region for outcome in outcomes}
    missing_regions = [region for region in regions if region not in completed_regions]
    if not missing_regions:
        return outcomes

    skip_reason = (
        "fail_fast"
        if context.fail_fast
        and (
            context.fail_fast_event.is_set()
            or any(outcome.failed for outcome in outcomes)
        )
        else "cancelled_before_start"
    )
    settled_tasks = tasks if tasks is not None else context.tasks
    outcomes.extend(
        _settled_provider_region_outcomes(
            regions=missing_regions, tasks=settled_tasks, skip_reason=skip_reason
        )
    )
    return outcomes


def _execute_provider_regions_sequential(
    *,
    execution_target: ExecutionTarget,
    runtime: ProviderExecutionRuntime,
    context: ExecutionContext,
    regions: list[str],
    target_cancel_event: threading.Event,
    tasks: list[ResolvedTask] | None = None,
    dependency_results: dict[str, TaskResult] | None = None,
    dependency_activated_task_ids: frozenset[str] | None = None,
    initial_stop_reason: str | None = None,
) -> list[_ProviderRegionOutcome]:
    region_outcomes: list[_ProviderRegionOutcome] = []
    for region in regions:
        outcome = _execute_provider_region(
            execution_target=execution_target,
            runtime=runtime,
            context=context,
            region=region,
            target_cancel_event=target_cancel_event,
            tasks=tasks,
            dependency_results=dependency_results,
            dependency_activated_task_ids=dependency_activated_task_ids,
            initial_stop_reason=initial_stop_reason,
            lazy_session=initial_stop_reason is not None,
        )
        region_outcomes.append(outcome)

        if initial_stop_reason is None and (
            outcome.interrupted or (context.fail_fast and outcome.failed)
        ):
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
    dependency_activated_task_ids: frozenset[str] | None = None,
    initial_stop_reason: str | None = None,
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
                and (
                    initial_stop_reason is not None
                    or (
                        not context.cancel_event.is_set()
                        and not context.fail_fast_event.is_set()
                    )
                )
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
                    dependency_activated_task_ids=dependency_activated_task_ids,
                    initial_stop_reason=initial_stop_reason,
                    lazy_session=initial_stop_reason is not None,
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

                if initial_stop_reason is None and (
                    outcome.interrupted or (context.fail_fast and outcome.failed)
                ):
                    target_cancel_event.set()
                    pending_regions.clear()

            if initial_stop_reason is None and (
                target_cancel_event.is_set()
                or context.cancel_event.is_set()
                or context.fail_fast_event.is_set()
            ):
                for future in active_futures:
                    future.cancel()

    return region_outcomes


def _execute_provider_task_graph(
    *,
    provider: Provider,
    target: TargetDescriptor,
    context: ExecutionContext,
    execution_targets: list[ExecutionTarget],
    configured_execution_target: ExecutionTarget | None,
    benchmark_data: dict[str, object] | None,
) -> TargetResult:
    """Execute a task graph using provider-owned runtime identities."""

    plan = plan_task_instances(
        tasks=context.tasks,
        execution_targets=execution_targets,
        configured_target=configured_execution_target,
    )
    task_order = {task.id: index for index, task in enumerate(context.tasks)}
    task_result_stages = _task_result_barrier_stages(context.tasks)
    target_order = {
        execution_target.id: index
        for index, execution_target in enumerate(execution_targets)
    }
    admission_region_order = {
        (execution_target.id, region): index
        for execution_target in execution_targets
        for index, region in enumerate(execution_target.regions)
    }

    def admission_order(instance: TaskInstance) -> tuple[int, int, int]:
        return (
            target_order[instance.execution_target.id],
            admission_region_order[(instance.execution_target.id, instance.region)],
            task_order[instance.task.id],
        )

    # Prefer completing ready work for one ordinary target-region coordinate
    # before opening its next coordinate. Dependency checks still release
    # cross-region fan-in when required. Configured-target instances retain
    # their declaration-relative positions.
    ordered_ordinary_instances = iter(
        sorted(
            (
                instance
                for instance in plan.instances
                if instance.task.scope is not TaskScope.CONFIGURED_TARGET
            ),
            key=admission_order,
        )
    )
    plan = replace(
        plan,
        instances=tuple(
            instance
            if instance.task.scope is TaskScope.CONFIGURED_TARGET
            else next(ordered_ordinary_instances)
            for instance in plan.instances
        ),
    )
    runtime_cache = _SingleFlightCache()
    session_cache = _SingleFlightCache()
    created_runtimes: dict[tuple[bool, str], ProviderExecutionRuntime] = {}
    created_sessions: set[tuple[bool, str, str]] = set()
    runtime_benchmarks: dict[tuple[bool, str], dict[str, object]] = {}
    runtime_started_at: dict[tuple[bool, str], str] = {}
    runtime_started_perf: dict[tuple[bool, str], float] = {}
    runtime_ended_at: dict[tuple[bool, str], str] = {}
    runtime_duration_seconds: dict[tuple[bool, str], float] = {}
    lifecycle_lock = threading.Lock()
    lifecycle_states: dict[tuple[bool, str, str], CoordinateLifecycleState] = {}

    def lifecycle_key(instance: TaskInstance) -> tuple[bool, str, str]:
        return (
            instance.task.scope is TaskScope.CONFIGURED_TARGET,
            instance.execution_target.id,
            instance.region,
        )

    for planned_instance in plan.instances:
        key = lifecycle_key(planned_instance)
        lifecycle_states.setdefault(
            key, CoordinateLifecycleState()
        ).remaining_instances += 1

    def runtime_for_instance(instance: TaskInstance) -> ProviderExecutionRuntime:
        is_configured = instance.task.scope is TaskScope.CONFIGURED_TARGET
        runtime_key = (is_configured, instance.execution_target.id)

        def create_runtime() -> object:
            started_at = datetime.datetime.now(datetime.UTC).isoformat()
            started_perf = time.perf_counter()
            if is_configured:
                runtime = cast(
                    ConfiguredTargetProvider, provider
                ).prepare_configured_target_runtime(
                    target=target,
                    execution_target=instance.execution_target,
                    context=context,
                )
            else:
                runtime = provider.prepare_execution_runtime(
                    target=target,
                    execution_target=instance.execution_target,
                    context=context,
                )
            with lifecycle_lock:
                created_runtimes[runtime_key] = runtime
                runtime_started_at[runtime_key] = started_at
                runtime_started_perf[runtime_key] = started_perf
            return runtime

        runtime, _cache_hit, _shared_wait = runtime_cache.get_or_create(
            key=runtime_key, create=create_runtime
        )
        return cast(ProviderExecutionRuntime, runtime)

    def session_for_instance(instance: TaskInstance) -> object:
        is_configured = instance.task.scope is TaskScope.CONFIGURED_TARGET
        session_key = (is_configured, instance.execution_target.id, instance.region)
        session_request_started_perf = time.perf_counter()

        def create_session() -> object:
            runtime = runtime_for_instance(instance)
            started_perf = time.perf_counter()
            session = runtime.build_session(region=instance.region)
            with lifecycle_lock:
                created_sessions.add(session_key)
                lifecycle_states[session_key].started_perf = started_perf
            return session

        session, cache_hit, _shared_wait = session_cache.get_or_create(
            key=session_key, create=create_session
        )
        if instance.task.scope is TaskScope.REGION:
            with lifecycle_lock:
                lifecycle_state = lifecycle_states[session_key]
                if lifecycle_state.region_started_perf is None:
                    lifecycle_state.region_started_perf = (
                        time.perf_counter()
                        if cache_hit
                        else session_request_started_perf
                    )
        return session

    def execute_instance(
        instance: TaskInstance, dependency_results: DependencyResults
    ) -> TaskResult:
        return _run_task_instance(
            instance=instance,
            dependency_results=dependency_results,
            session=session_for_instance(instance),
            context=context,
        )

    def record_settled_instance(instance: TaskInstance, result: TaskResult) -> None:
        key = lifecycle_key(instance)
        with lifecycle_lock:
            lifecycle_state = lifecycle_states[key]
            settled = lifecycle_state.record_settlement(
                result=result,
                region_scoped=instance.task.scope is TaskScope.REGION,
                ended_perf=time.perf_counter(),
            )
            if not settled or key not in created_sessions:
                return
            is_configured, execution_target_id, region = key
            runtime = created_runtimes[(is_configured, execution_target_id)]
            started_perf = lifecycle_state.started_perf
            failed = lifecycle_state.failed
            interrupted = lifecycle_state.interrupted
            if started_perf is None:
                raise RuntimeError("Created session has no lifecycle start time")
        ended_perf = time.perf_counter()
        runtime.record_region_outcome(
            region=region,
            duration_seconds=ended_perf - started_perf,
            failed=failed,
            interrupted=interrupted,
        )

    recorder = BenchmarkRecorder(data=benchmark_data)
    try:
        with recorder.phase("entity_execution_seconds"):
            schedule = execute_task_instance_plan(
                plan=plan,
                execute=execute_instance,
                max_workers=max(1, target.max_workers * context.max_parallel_regions),
                max_active_execution_targets=target.max_workers,
                max_active_coordinates_per_execution_target=(
                    context.max_parallel_regions
                ),
                cancel_event=context.cancel_event,
                fail_fast=context.fail_fast,
                external_fail_fast_event=context.fail_fast_event,
                on_instance_settled=record_settled_instance,
            )
        for runtime_key, runtime in created_runtimes.items():
            benchmark = getattr(runtime, "benchmark", None)
            if callable(benchmark):
                benchmark = benchmark()
            if isinstance(benchmark, dict):
                runtime_benchmarks[runtime_key] = benchmark
    finally:
        for runtime_key, runtime in list(created_runtimes.items()):
            try:
                runtime.close()
            finally:
                runtime_ended_at[runtime_key] = datetime.datetime.now(
                    datetime.UTC
                ).isoformat()
                runtime_duration_seconds[runtime_key] = (
                    time.perf_counter() - runtime_started_perf[runtime_key]
                )

    task_results_by_execution_target: dict[str, list[ScheduledTaskResult]] = {}
    configured_results: list[TaskResult] = []
    for scheduled_result in schedule.results:
        if scheduled_result.key.scope is TaskScope.CONFIGURED_TARGET:
            configured_results.append(scheduled_result.result)
            continue
        task_results_by_execution_target.setdefault(
            scheduled_result.key.execution_target_id, []
        ).append(scheduled_result)

    entity_results: list[EntityResult] = []
    for execution_target in execution_targets:
        target_task_results = task_results_by_execution_target.get(
            execution_target.id, []
        )
        if not target_task_results:
            continue

        region_order = {
            region: index for index, region in enumerate(execution_target.regions)
        }
        target_task_results.sort(
            key=lambda item: (
                task_result_stages[item.key.task_id],
                0 if item.key.scope is TaskScope.TARGET else 1,
                region_order.get(item.key.region, len(region_order)),
                task_order[item.key.task_id],
            )
        )
        results = [item.result for item in target_task_results]
        status = _aggregate_task_result_status(results)
        target_scope_results = [
            item.result
            for item in target_task_results
            if item.key.scope is TaskScope.TARGET
        ]
        target_outcome = (
            _provider_region_outcome(
                region=target_scope_results[0].region,
                task_results=target_scope_results,
                duration_seconds=sum(
                    result.duration_seconds for result in target_scope_results
                ),
            )
            if target_scope_results
            else None
        )
        region_results_by_region: dict[str, list[TaskResult]] = {}
        for item in target_task_results:
            if item.key.scope is TaskScope.REGION:
                region_results_by_region.setdefault(item.key.region, []).append(
                    item.result
                )

        region_outcomes: list[_ProviderRegionOutcome] = []
        region_lifecycle_states: list[CoordinateLifecycleState] = []
        for region in execution_target.regions:
            lifecycle_state = lifecycle_states.get((False, execution_target.id, region))
            if (
                lifecycle_state is None
                or lifecycle_state.region_started_perf is None
                or lifecycle_state.region_ended_perf is None
            ):
                continue
            region_results = region_results_by_region.get(region, [])
            if not region_results:
                continue
            region_outcomes.append(
                _provider_region_outcome(
                    region=region,
                    task_results=region_results,
                    duration_seconds=(
                        lifecycle_state.region_ended_perf
                        - lifecycle_state.region_started_perf
                    ),
                )
            )
            region_lifecycle_states.append(lifecycle_state)

        provider_benchmark = runtime_benchmarks.get((False, execution_target.id))
        region_execution_seconds = (
            max(
                lifecycle_state.region_ended_perf
                for lifecycle_state in region_lifecycle_states
                if lifecycle_state.region_ended_perf is not None
            )
            - min(
                lifecycle_state.region_started_perf
                for lifecycle_state in region_lifecycle_states
                if lifecycle_state.region_started_perf is not None
            )
            if region_lifecycle_states
            else 0.0
        )
        runtime_benchmark = (
            _augment_provider_runtime_benchmark(
                benchmark=provider_benchmark,
                region_outcomes=region_outcomes,
                region_execution_seconds=region_execution_seconds,
                target_outcome=target_outcome,
                target_execution_seconds=(
                    target_outcome.duration_seconds
                    if target_outcome is not None
                    else 0.0
                ),
            )
            if provider_benchmark is not None
            else None
        )

        runtime_key = (False, execution_target.id)
        started_at = (
            runtime_started_at[runtime_key]
            if runtime_key in runtime_started_at
            else min(result.started_at for result in results)
        )
        ended_at = (
            runtime_ended_at[runtime_key]
            if runtime_key in runtime_ended_at
            else max(result.ended_at for result in results)
        )
        entity_results.append(
            EntityResult(
                id=execution_target.id,
                name=execution_target.name,
                type=execution_target.type,
                provider=execution_target.provider,
                metadata=dict(execution_target.metadata),
                status=status,
                started_at=started_at,
                ended_at=ended_at,
                duration_seconds=(
                    runtime_duration_seconds[runtime_key]
                    if runtime_key in runtime_duration_seconds
                    else (
                        datetime.datetime.fromisoformat(ended_at)
                        - datetime.datetime.fromisoformat(started_at)
                    ).total_seconds()
                ),
                tasks=results,
                benchmark=runtime_benchmark,
            )
        )

    entity_results.sort(key=lambda result: (result.name.lower(), result.id))
    _record_entity_execution_metrics(
        recorder=recorder,
        entity_results=entity_results,
        submitted_entity_count=(
            len(execution_targets)
            if any(
                task.scope is not TaskScope.CONFIGURED_TARGET for task in context.tasks
            )
            else 0
        ),
        target=target,
        context=context,
    )
    return TargetResult.create(
        target_name=target.name,
        provider=target.provider,
        dry_run=context.dry_run,
        entities=entity_results,
        tasks=configured_results,
        benchmark=recorder.data,
    )


def _execute_provider_targets(
    *,
    provider: Provider,
    target: TargetDescriptor,
    context: ExecutionContext,
    execution_targets: list[ExecutionTarget],
    benchmark_data: dict[str, object] | None,
    configured_execution_target: ExecutionTarget | None = None,
) -> TargetResult:
    has_configured_tasks = any(
        task.scope is TaskScope.CONFIGURED_TARGET for task in context.tasks
    )
    ordinary_tasks = [
        task for task in context.tasks if task.scope is not TaskScope.CONFIGURED_TARGET
    ]
    if has_configured_tasks or _requires_task_instance_scheduler(ordinary_tasks):
        if has_configured_tasks and configured_execution_target is None:
            raise ValueError(
                "configured-target tasks require a provider-owned execution identity"
            )
        return _execute_provider_task_graph(
            provider=provider,
            target=target,
            context=context,
            execution_targets=execution_targets,
            configured_execution_target=configured_execution_target,
            benchmark_data=benchmark_data,
        )

    entity_results: list[EntityResult] = []
    recorder = BenchmarkRecorder(data=benchmark_data)

    with recorder.phase("entity_execution_seconds"):
        pending_targets = deque(execution_targets)
        with ThreadPoolExecutor(max_workers=target.max_workers) as executor:
            active_futures: dict[Future[EntityResult], ExecutionTarget] = {}

            try:
                while pending_targets or active_futures:
                    while (
                        pending_targets
                        and not context.cancel_event.is_set()
                        and not context.fail_fast_event.is_set()
                        and len(active_futures) < target.max_workers
                    ):
                        execution_target = pending_targets.popleft()
                        future = executor.submit(
                            _execute_provider_execution_target,
                            provider=provider,
                            target=target,
                            execution_target=execution_target,
                            context=context,
                        )
                        active_futures[future] = execution_target

                    if not active_futures:
                        break

                    completed, _ = wait(active_futures, return_when=FIRST_COMPLETED)
                    for future in completed:
                        active_futures.pop(future)
                        if future.cancelled():
                            continue

                        try:
                            entity_result = future.result()
                        except CancelledError:
                            continue

                        entity_results.append(entity_result)
                        if context.fail_fast and entity_result.status.is_unsuccessful:
                            context.fail_fast_event.set()
                            pending_targets.clear()
                            for active_future in active_futures:
                                active_future.cancel()
            except Exception:
                executor.shutdown(cancel_futures=True)
                raise

    entity_results.sort(key=lambda result: (result.name.lower(), result.id))
    _record_entity_execution_metrics(
        recorder=recorder,
        entity_results=entity_results,
        submitted_entity_count=len(execution_targets),
        target=target,
        context=context,
    )

    return TargetResult.create(
        target_name=target.name,
        provider=target.provider,
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


def _record_entity_execution_metrics(
    *,
    recorder: BenchmarkRecorder,
    entity_results: list[EntityResult],
    submitted_entity_count: int,
    target: TargetDescriptor,
    context: ExecutionContext,
) -> None:
    """Record shared target-worker metrics for either execution strategy."""

    if not recorder.enabled:
        return
    entity_execution_window_seconds = _entity_execution_window_seconds(entity_results)
    sum_entity_duration_seconds = sum(
        result.duration_seconds for result in entity_results
    )
    recorder.update(
        {
            "submitted_entity_count": submitted_entity_count,
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
            "entity_region_limit": target.max_workers * context.max_parallel_regions,
        }
    )


def run_prepared_target(*, prepared_target: PreparedTarget) -> TargetExecutionOutcome:
    if prepared_target.context is None:
        raise ValueError("Prepared target is not runnable.")

    target: TargetDescriptor = prepared_target.effective_target
    context: ExecutionContext = prepared_target.context
    if prepared_target.preflight_error is not None:
        return TargetExecutionOutcome(
            index=prepared_target.index,
            target_result=TargetResult.create(
                target_name=target.name,
                provider=target.provider,
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

    provider = prepared_target.provider
    try:
        with sink.phase("resolve_execution_targets_seconds"):
            execution_plan = provider.resolve_execution_targets(
                target=target,
                regions=context.regions,
                include=prepared_target.effective_include,
                exclude=prepared_target.effective_exclude,
                preparation=prepared_target.provider_preflight,
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
            configured_execution_target=execution_plan.configured_target,
            benchmark_data=benchmark_data,
            context=context,
        )
    except Exception as error:
        target_result = TargetResult.create(
            target_name=target.name,
            provider=target.provider,
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
    return prepared_target.exclusive_execution_keys


def run_auth_checks(*, targets: list[TargetDescriptor]) -> EngineResult:
    """
    Run authentication checks only. Does not resolve tasks or execute targets.
    """
    auth_results: list[AuthResult] = []
    auth_cache = AuthCheckCache()

    with ThreadPoolExecutor(
        max_workers=max(1, min(DEFAULT_AUTH_CHECK_MAX_WORKERS, len(targets)))
    ) as executor:
        futures: list[Future[AuthResult]] = [
            executor.submit(
                _run_cached_provider_auth_check_for_target,
                provider=_load_provider(target.provider),
                target=target,
                auth_cache=auth_cache,
            )
            for target in targets
        ]

        for target, future in zip(targets, futures, strict=True):
            auth_result = future.result()
            auth_results.append(auth_result)

    return EngineResult.create(
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
    preparation_cache = _SingleFlightCache()
    auth_cache = AuthCheckCache()

    if targets:
        worker_limit = max(1, min(max_parallel_targets, len(targets)))

        with (
            ThreadPoolExecutor(max_workers=worker_limit) as prepare_executor,
            ThreadPoolExecutor(max_workers=worker_limit) as execute_executor,
        ):
            preflight_futures: dict[
                Future[PreparedTarget | TargetExecutionOutcome], int
            ] = {
                cast(
                    Future[PreparedTarget | TargetExecutionOutcome],
                    prepare_executor.submit(
                        prepare_target,
                        index=index,
                        target=target,
                        cli_dry_run=cli_dry_run,
                        cli_include=cli_include,
                        cli_exclude=cli_exclude,
                        preparation_cache=preparation_cache,
                        auth_cache=auth_cache,
                        benchmark_enabled=benchmark_enabled,
                    ),
                ): index
                for index, target in enumerate(targets)
            }
            execution_futures: dict[
                Future[PreparedTarget | TargetExecutionOutcome], PreparedTarget
            ] = {}

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

                    future = cast(
                        Future[PreparedTarget | TargetExecutionOutcome],
                        execute_executor.submit(
                            run_prepared_target, prepared_target=next_target
                        ),
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
                        prepared_result = future.result()
                        if not isinstance(prepared_result, PreparedTarget):
                            raise TypeError(
                                "target preparation returned an execution outcome"
                            )
                        prepared_target = prepared_result
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
                    if not isinstance(outcome, TargetExecutionOutcome):
                        raise TypeError("target execution returned a prepared target")
                    target_results_by_index[outcome.index] = outcome.target_result

                    if outcome.target_result.has_failures:
                        execution_state = _elevate_state(
                            execution_state, EngineState.COMPLETED_WITH_FAILURES
                        )
                    elif outcome.cancelled:
                        execution_state = _elevate_state(
                            execution_state, EngineState.CANCELLED
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
        state=engine_state,
        auth_results=auth_results,
        target_results=target_results,
        benchmark=recorder.data,
    )
