"""
Audit security-related settings for the current GitHub repository target.
"""

from __future__ import annotations

import logging

from anvil.actions import ActionRecorder
from anvil.providers.github.tasks._rest import (
    require_github_provider,
    require_repository_target,
    rest_get,
)

__LOGGER__ = logging.getLogger(__name__)

TASK_NAME = "audit_repo_security_settings"
REPOSITORY_SETTING_KEYS = (
    "full_name",
    "private",
    "visibility",
    "archived",
    "disabled",
    "default_branch",
    "has_issues",
    "has_projects",
    "has_wiki",
    "allow_forking",
    "allow_squash_merge",
    "allow_merge_commit",
    "allow_rebase_merge",
    "allow_auto_merge",
    "delete_branch_on_merge",
    "web_commit_signoff_required",
    "security_and_analysis",
)


def run(
    *,
    provider: str,
    execution_target_id: str,
    execution_target_name: str,
    execution_target_type: str,
    region: str,
    location: str,
    session,
    dry_run: bool,
    metadata: dict[str, object],
    actions: ActionRecorder,
) -> dict[str, object]:
    """Read security-relevant repository settings from GitHub."""

    require_github_provider(task_name=TASK_NAME, provider=provider)
    owner, repo = require_repository_target(
        task_name=TASK_NAME,
        execution_target_id=execution_target_id,
        execution_target_type=execution_target_type,
    )
    repository = rest_get(session=session, path=f"/repos/{owner}/{repo}")
    if not isinstance(repository, dict):
        raise RuntimeError(f"{TASK_NAME} could not read repository metadata")

    settings = {
        key: repository.get(key)
        for key in REPOSITORY_SETTING_KEYS
        if key in repository
    }

    __LOGGER__.info(
        f"Audited GitHub repository security settings for {execution_target_name} "
        f"location={location or region}"
    )
    actions.record(
        f"Audited GitHub repository security settings for {execution_target_id} "
        f"location {location or region}"
    )
    return {"settings": settings}
