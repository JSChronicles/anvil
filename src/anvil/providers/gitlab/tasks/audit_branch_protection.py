"""Audit protected branches for the current GitLab project."""

import logging
from anvil.actions import ActionRecorder
from anvil.providers.gitlab.tasks._api import list_manager, project_for_task

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
    """Audit GitLab protected-branch rules.

    Args:
        metadata: Optional ``max_results`` limits returned rules.
        session: GitLab session scoped to the current project.

    Returns:
        Protected-branch rule count and definitions.
    """
    project = project_for_task(
        task_name="audit_branch_protection",
        provider=provider,
        execution_target_id=execution_target_id,
        execution_target_type=execution_target_type,
        session=session,
    )
    rules = list_manager(
        manager=getattr(project, "protectedbranches", None), metadata=metadata
    )
    __LOGGER__.info(
        f"Audited {len(rules)} GitLab protected branch rule(s) for {execution_target_name}"
    )
    actions.record(
        f"Audited {len(rules)} GitLab protected branch rule(s) for {execution_target_id} region {region}"
    )
    return {"protected_branch_count": len(rules), "protected_branches": rules}
