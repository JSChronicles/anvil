"""
Audit branch protection for the current GitHub repository target.
"""

from __future__ import annotations

import logging

from anvil.actions import ActionRecorder
from anvil.providers.github.tasks._rest import (
    metadata_string,
    require_github_provider,
    require_repository_target,
    rest_get,
    runtime_error_from_provider_error,
)

__LOGGER__ = logging.getLogger(__name__)

TASK_NAME = "audit_branch_protection"


def run(
    *,
    provider: str,
    execution_target_id: str,
    execution_target_name: str,
    execution_target_type: str,
    region: str,
    session,
    dry_run: bool,
    metadata: dict[str, object],
    actions: ActionRecorder,
) -> dict[str, object]:
    """Read branch protection settings for a GitHub repository branch."""

    require_github_provider(task_name=TASK_NAME, provider=provider)
    owner, repo = require_repository_target(
        task_name=TASK_NAME,
        execution_target_id=execution_target_id,
        execution_target_type=execution_target_type,
    )
    branch = metadata_string(task_name=TASK_NAME, metadata=metadata, key="branch")
    if branch is None:
        repository = rest_get(session=session, path=f"/repos/{owner}/{repo}")
        if not isinstance(repository, dict):
            raise RuntimeError(f"{TASK_NAME} could not read repository metadata")
        value = repository.get("default_branch")
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError(f"{TASK_NAME} could not determine default branch")
        branch = value.strip()

    try:
        protection = rest_get(
            session=session, path=f"/repos/{owner}/{repo}/branches/{branch}/protection"
        )
        protected = True
    except Exception as error:
        if not _is_not_found(error):
            raise runtime_error_from_provider_error(error) from error
        protection = None
        protected = False

    __LOGGER__.info(
        f"Audited GitHub branch protection for repository {execution_target_name} "
        f"branch={branch} protected={protected} region={region}"
    )
    actions.record(
        f"Audited GitHub branch protection for repository {execution_target_id} "
        f"branch {branch} region {region}"
    )
    return {"branch": branch, "protected": protected, "protection": protection}


def _is_not_found(error: Exception) -> bool:
    status = getattr(error, "status", None)
    data = getattr(error, "data", None)
    return (
        status == 404
        or "404" in str(error)
        or (isinstance(data, dict) and data.get("status") == "404")
    )
