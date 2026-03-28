from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from anvil.auth import auth_check, infer_auth_source
from anvil.execution_context import ExecutionContext
from anvil.organization import Organization
from anvil.results import AuthResult, EngineResult, EngineState, OrgResult
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


def _run_auth_check_for_org(organization) -> AuthResult:
    """
    Run an auth check for a single organization descriptor.
    """
    auth_source = infer_auth_source(organization.profile)

    return auth_check(
        org_name=organization.name,
        profile=organization.profile,
        auth_source=auth_source,
    )


def _get_auth_check_pool_size(org_count: int) -> int:
    """
    Return the bounded worker count used for auth-only checks.
    """
    return max(1, min(DEFAULT_AUTH_CHECK_MAX_WORKERS, org_count))


def run_auth_checks(*, orgs: list) -> EngineResult:
    """
    Run authentication checks only. Does not resolve tasks or execute organizations.
    """
    engine_state = EngineState.COMPLETED_SUCCESS
    auth_results: list[AuthResult] = []

    with ThreadPoolExecutor(
        max_workers=_get_auth_check_pool_size(len(orgs))
    ) as executor:
        futures = [
            executor.submit(_run_auth_check_for_org, organization)
            for organization in orgs
        ]

        for organization, future in zip(orgs, futures, strict=True):
            auth_result = future.result()
            auth_results.append(auth_result)

            if auth_result.is_error:
                if organization.fail_fast:
                    engine_state = _elevate_state(engine_state, EngineState.AUTH_FAILED)
                    break

                engine_state = _elevate_state(
                    engine_state, EngineState.COMPLETED_WITH_FAILURES
                )

    return EngineResult.create(
        state=engine_state, auth_results=auth_results, organization_results=[]
    )


def run_multiple_orgs(
    *,
    orgs: list,
    cli_dry_run: bool | None,
    cli_include: list[str] | None,
    cli_exclude: list[str] | None,
) -> EngineResult:

    engine_state = EngineState.COMPLETED_SUCCESS
    auth_results: list[AuthResult] = []
    org_results: list[OrgResult] = []

    for organization in orgs:
        auth_source = infer_auth_source(organization.profile)

        auth_result = auth_check(
            org_name=organization.name,
            profile=organization.profile,
            auth_source=auth_source,
        )

        auth_results.append(auth_result)

        if auth_result.is_error:
            if organization.fail_fast:
                engine_state = _elevate_state(engine_state, EngineState.AUTH_FAILED)
                break
            engine_state = _elevate_state(
                engine_state, EngineState.COMPLETED_WITH_FAILURES
            )
            continue

        execution = resolve_tasks(task_specs=organization.tasks)
        tasks = execution.ordered

        effective_dry_run = (
            cli_dry_run if cli_dry_run is not None else organization.dry_run
        )

        effective_include_ids = (
            cli_include if cli_include is not None else organization.include
        )

        effective_exclude_ids = (
            cli_exclude if cli_exclude is not None else organization.exclude
        )

        context = ExecutionContext(
            region=organization.region,
            role_name=organization.role_name,
            dry_run=effective_dry_run,
            tasks=tasks,
            metadata=organization.metadata,
            fail_fast=organization.fail_fast,
        )

        org_runner = Organization(
            name=organization.name,
            profile_name=organization.profile,
            max_workers=organization.max_workers,
            include_ids=effective_include_ids,
            exclude_ids=effective_exclude_ids,
            context=context,
        )

        org_result = org_runner.execute()
        org_results.append(org_result)

        if context.cancel_event.is_set():
            engine_state = _elevate_state(engine_state, EngineState.CANCELLED)
            break

        if engine_state is EngineState.COMPLETED_SUCCESS and org_result.has_failures:
            engine_state = _elevate_state(
                engine_state, EngineState.COMPLETED_WITH_FAILURES
            )

    return EngineResult.create(
        state=engine_state, auth_results=auth_results, organization_results=org_results
    )
