"""Audit security-relevant settings for the current GitLab project."""

import logging
from anvil.actions import ActionRecorder
from anvil.providers.gitlab.tasks._api import project_for_task
from anvil.providers.tasks._task_helpers import json_safe

__LOGGER__ = logging.getLogger(__name__)


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
    dependency_data: dict[str, object],
    actions: ActionRecorder,
) -> dict[str, object]:
    """Audit GitLab project security and merge settings.

    Args:
        session: GitLab session scoped to the current project.

    Returns:
        JSON-serializable project settings for policy evaluation.
    """
    project = project_for_task(
        task_name="audit_repo_security_settings",
        provider=provider,
        execution_target_id=execution_target_id,
        execution_target_type=execution_target_type,
        session=session,
    )
    settings = json_safe(project)
    __LOGGER__.info(
        f"Audited GitLab project security settings for {execution_target_name}"
    )
    actions.record(
        f"Audited GitLab project security settings for {execution_target_id} region {region}"
    )
    return {"settings": settings}
