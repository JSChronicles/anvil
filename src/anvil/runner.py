from __future__ import annotations

import logging
import threading
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field, replace

from boto3.session import Session

from anvil.account import Account
from anvil.account_resolver import AccountResolver
from anvil.auth import AuthSource, auth_check, infer_auth_source
from anvil.descriptors import ConfigBranch, TargetDescriptor
from anvil.execution_context import ExecutionContext
from anvil.executor import execute_accounts
from anvil.organization import OrganizationResolver
from anvil.results import AuthResult, EngineResult, EngineState, TargetResult
from anvil.session import SessionFactory
from anvil.task_loader import ResolvedExecution, ResolvedTask, resolve_tasks

__LOGGER__ = logging.getLogger(__name__)


STATE_PRECEDENCE: dict[EngineState, int] = {
    EngineState.AUTH_FAILED: 4,
    EngineState.CANCELLED: 3,
    EngineState.COMPLETED_WITH_FAILURES: 2,
    EngineState.COMPLETED_SUCCESS: 1,
}

DEFAULT_AUTH_CHECK_MAX_WORKERS = 4


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
    discovered_accounts: dict[str, dict[str, str]] | None = None
    enabled_regions: list[str] | None = None

    @property
    def runnable(self) -> bool:
        return self.context is not None


@dataclass(frozen=True, slots=True)
class TargetExecutionOutcome:
    index: int
    target_result: TargetResult
    cancelled: bool


@dataclass(frozen=True, slots=True)
class OrganizationRunCacheEntry:
    management_account_id: str
    discovered_accounts: dict[str, dict[str, str]]
    enabled_regions: list[str]


class OrganizationRunCache:
    def __init__(self) -> None:
        self._entries: dict[str, OrganizationRunCacheEntry] = {}
        self._lock = threading.Lock()

    def get(self, organization_id: str) -> OrganizationRunCacheEntry | None:
        with self._lock:
            return self._entries.get(organization_id)

    def put_if_absent(
        self, organization_id: str, entry: OrganizationRunCacheEntry
    ) -> OrganizationRunCacheEntry:
        with self._lock:
            existing = self._entries.get(organization_id)
            if existing is not None:
                return existing

            self._entries[organization_id] = entry
            return entry


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


def _run_auth_check_for_target(target: TargetDescriptor) -> AuthResult:
    """
    Run an auth check for a single target descriptor.
    """
    auth_source: AuthSource = infer_auth_source(target.profile)

    return auth_check(
        target_name=target.name, profile=target.profile, auth_source=auth_source
    )


def _resolve_effective_account_filters(
    *,
    target: TargetDescriptor,
    cli_include: list[str] | None,
    cli_exclude: list[str] | None,
) -> tuple[list[str] | None, list[str] | None]:
    if target.is_accounts_config:
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

    return replace(
        target,
        dry_run=effective_dry_run,
        include=effective_include,
        exclude=effective_exclude,
    )


def _build_execution_context(
    *, target: TargetDescriptor, tasks: list[ResolvedTask]
) -> ExecutionContext:
    return ExecutionContext(
        regions=target.regions,
        role_name=target.role_name,
        dry_run=target.dry_run,
        tasks=tasks,
        metadata=target.metadata,
        fail_fast=target.fail_fast,
    )


def _preflight_organization(
    *,
    target: TargetDescriptor,
    context: ExecutionContext,
    session_factory: SessionFactory,
    organization_cache: OrganizationRunCache,
) -> tuple[Session, str, str, dict[str, dict[str, str]], list[str]]:
    base_session: Session = session_factory.create_base_session(
        profile_name=target.profile, region_name=context.regions[0]
    )
    organization_id, management_account_id = OrganizationResolver.describe_organization(
        base_session
    )

    cached_entry = organization_cache.get(organization_id)
    if cached_entry is None:
        cached_entry = organization_cache.put_if_absent(
            organization_id,
            OrganizationRunCacheEntry(
                management_account_id=management_account_id,
                discovered_accounts=OrganizationResolver.discover_accounts(
                    base_session
                ),
                enabled_regions=OrganizationResolver.discover_enabled_regions(
                    base_session
                ),
            ),
        )

    return (
        base_session,
        organization_id,
        cached_entry.management_account_id,
        cached_entry.discovered_accounts,
        cached_entry.enabled_regions,
    )


def prepare_target(
    *,
    index: int,
    target: TargetDescriptor,
    cli_dry_run: bool | None,
    cli_include: list[str] | None,
    cli_exclude: list[str] | None,
    organization_cache: OrganizationRunCache,
) -> PreparedTarget:
    session_factory = SessionFactory()
    auth_result: AuthResult = _run_auth_check_for_target(target)
    effective_target: TargetDescriptor = _build_effective_target(
        target=target,
        cli_dry_run=cli_dry_run,
        cli_include=cli_include,
        cli_exclude=cli_exclude,
    )

    if auth_result.is_error:
        return PreparedTarget(
            index=index,
            effective_target=effective_target,
            auth_result=auth_result,
            context=None,
            session_factory=session_factory,
        )

    execution: ResolvedExecution = resolve_tasks(task_specs=effective_target.tasks)
    tasks: list[ResolvedTask] = execution.ordered
    context: ExecutionContext = _build_execution_context(
        target=effective_target, tasks=tasks
    )

    base_session: Session | None = None
    organization_id: str | None = None
    management_account_id: str | None = None
    discovered_accounts: dict[str, dict[str, str]] | None = None
    enabled_regions: list[str] | None = None
    if effective_target.is_organization_config:
        (
            base_session,
            organization_id,
            management_account_id,
            discovered_accounts,
            enabled_regions,
        ) = _preflight_organization(
            target=effective_target,
            context=context,
            session_factory=session_factory,
            organization_cache=organization_cache,
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
        discovered_accounts=discovered_accounts,
        enabled_regions=enabled_regions,
    )


def run_prepared_target(*, prepared_target: PreparedTarget) -> TargetExecutionOutcome:
    if prepared_target.context is None:
        raise ValueError("Prepared target is not runnable.")

    target: TargetDescriptor = prepared_target.effective_target
    context: ExecutionContext = prepared_target.context

    if target.is_organization_config:
        resolver = OrganizationResolver(
            descriptor=target,
            context=context,
            management_account_id=prepared_target.management_account_id,
            session_factory=prepared_target.session_factory,
            base_session=prepared_target.base_session,
            discovered_accounts=prepared_target.discovered_accounts,
            enabled_regions=prepared_target.enabled_regions,
        )
    else:
        resolver = AccountResolver(
            descriptor=target,
            context=context,
            session_factory=prepared_target.session_factory,
        )

    try:
        accounts: list[Account] = resolver.resolve_accounts()
        target_result: TargetResult = execute_accounts(
            name=target.name,
            config_branch=target.config_branch,
            max_workers=target.max_workers,
            context=context,
            accounts=accounts,
        )
    except ValueError as error:
        target_result: TargetResult = TargetResult.create(
            config_branch=target.config_branch,
            target_name=target.name,
            dry_run=context.dry_run,
            account_results=[],
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
        targets[0].config_branch if targets else ConfigBranch.ORGANIZATIONS
    )
    auth_results: list[AuthResult] = []

    with ThreadPoolExecutor(
        max_workers=max(1, min(DEFAULT_AUTH_CHECK_MAX_WORKERS, len(targets)))
    ) as executor:
        futures: list[Future[AuthResult]] = [
            executor.submit(_run_auth_check_for_target, target) for target in targets
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
) -> EngineResult:
    config_branch: ConfigBranch = (
        targets[0].config_branch if targets else ConfigBranch.ORGANIZATIONS
    )
    auth_results, target_results, engine_state = _run_target_pipeline(
        targets=targets,
        max_parallel_targets=max_parallel_targets,
        cli_dry_run=cli_dry_run,
        cli_include=cli_include,
        cli_exclude=cli_exclude,
    )

    return EngineResult.create(
        config_branch=config_branch,
        state=engine_state,
        auth_results=auth_results,
        target_results=target_results,
    )
