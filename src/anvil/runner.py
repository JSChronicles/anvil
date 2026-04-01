from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from anvil.account_resolver import AccountResolver
from anvil.auth import auth_check, infer_auth_source
from anvil.descriptors import ConfigBranch, TargetDescriptor
from anvil.execution_context import ExecutionContext
from anvil.executor import execute_accounts
from anvil.organization import OrganizationResolver
from anvil.results import AuthResult, EngineResult, EngineState, TargetResult
from anvil.task_loader import resolve_tasks

__LOGGER__ = logging.getLogger(__name__)


STATE_PRECEDENCE = {
    EngineState.AUTH_FAILED: 4,
    EngineState.CANCELLED: 3,
    EngineState.COMPLETED_WITH_FAILURES: 2,
    EngineState.COMPLETED_SUCCESS: 1,
}

DEFAULT_AUTH_CHECK_MAX_WORKERS = 4


def _elevate_state(current: EngineState, new: EngineState) -> EngineState:
    """
    Elevate engine state based on explicit precedence rules.
    """
    if STATE_PRECEDENCE[new] > STATE_PRECEDENCE[current]:
        return new
    return current


def _run_auth_check_for_target(target: TargetDescriptor) -> AuthResult:
    """
    Run an auth check for a single target descriptor.
    """
    auth_source = infer_auth_source(target.profile)

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
        narrowed_include = [
            account_id for account_id in cli_include if account_id in configured_account_ids
        ]
        return narrowed_include, None

    effective_include = target.include
    effective_exclude = target.exclude

    if cli_include is not None:
        effective_include = cli_include
    if cli_exclude is not None:
        effective_exclude = cli_exclude

    return effective_include, effective_exclude


def run_auth_checks(*, targets: list[TargetDescriptor]) -> EngineResult:
    """
    Run authentication checks only. Does not resolve tasks or execute targets.
    """
    config_branch = targets[0].config_branch if targets else ConfigBranch.ORGANIZATIONS
    engine_state = EngineState.COMPLETED_SUCCESS
    auth_results: list[AuthResult] = []

    with ThreadPoolExecutor(
        max_workers=max(1, min(DEFAULT_AUTH_CHECK_MAX_WORKERS, len(targets)))
    ) as executor:
        futures = [
            executor.submit(_run_auth_check_for_target, target) for target in targets
        ]

        for target, future in zip(targets, futures, strict=True):
            auth_result = future.result()
            auth_results.append(auth_result)

            if auth_result.is_error:
                if target.fail_fast:
                    engine_state = _elevate_state(engine_state, EngineState.AUTH_FAILED)
                    break

                engine_state = _elevate_state(
                    engine_state, EngineState.COMPLETED_WITH_FAILURES
                )

    return EngineResult.create(
        config_branch=config_branch,
        state=engine_state,
        auth_results=auth_results,
        target_results=[],
    )


def run_multiple_targets(
    *,
    targets: list[TargetDescriptor],
    cli_dry_run: bool | None,
    cli_include: list[str] | None,
    cli_exclude: list[str] | None,
) -> EngineResult:
    config_branch = targets[0].config_branch if targets else ConfigBranch.ORGANIZATIONS
    engine_state = EngineState.COMPLETED_SUCCESS
    auth_results: list[AuthResult] = []
    target_results: list[TargetResult] = []

    for target in targets:
        auth_result = _run_auth_check_for_target(target)
        auth_results.append(auth_result)

        if auth_result.is_error:
            if target.fail_fast:
                engine_state = _elevate_state(engine_state, EngineState.AUTH_FAILED)
                break

            engine_state = _elevate_state(
                engine_state, EngineState.COMPLETED_WITH_FAILURES
            )
            continue

        execution = resolve_tasks(task_specs=target.tasks)
        tasks = execution.ordered

        effective_dry_run = cli_dry_run if cli_dry_run is not None else target.dry_run
        effective_include, effective_exclude = _resolve_effective_account_filters(
            target=target, cli_include=cli_include, cli_exclude=cli_exclude
        )

        effective_target = replace(
            target,
            dry_run=effective_dry_run,
            include=effective_include,
            exclude=effective_exclude,
        )

        context = ExecutionContext(
            regions=effective_target.regions,
            role_name=effective_target.role_name,
            dry_run=effective_target.dry_run,
            tasks=tasks,
            metadata=effective_target.metadata,
            fail_fast=effective_target.fail_fast,
        )

        if effective_target.is_organization_config:
            resolver = OrganizationResolver(
                descriptor=effective_target, context=context
            )
        else:
            resolver = AccountResolver(descriptor=effective_target, context=context)

        try:
            accounts = resolver.resolve_accounts()
        except ValueError as error:
            target_results.append(
                TargetResult.create(
                    config_branch=effective_target.config_branch,
                    target_name=effective_target.name,
                    dry_run=context.dry_run,
                    account_results=[],
                    error=str(error),
                )
            )
            engine_state = _elevate_state(
                engine_state, EngineState.COMPLETED_WITH_FAILURES
            )
            continue

        target_result = execute_accounts(
            name=effective_target.name,
            config_branch=effective_target.config_branch,
            max_workers=effective_target.max_workers,
            context=context,
            accounts=accounts,
        )
        target_results.append(target_result)

        if context.cancel_event.is_set():
            engine_state = _elevate_state(engine_state, EngineState.CANCELLED)
            break

        if engine_state is EngineState.COMPLETED_SUCCESS and target_result.has_failures:
            engine_state = _elevate_state(
                engine_state, EngineState.COMPLETED_WITH_FAILURES
            )

    return EngineResult.create(
        config_branch=config_branch,
        state=engine_state,
        auth_results=auth_results,
        target_results=target_results,
    )
