"""Audit GitLab project push, approval, and protected-branch rules."""

import logging
from anvil.actions import ActionRecorder
from anvil.providers.gitlab.tasks._api import list_manager, project_for_task
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
    """Audit GitLab project rules analogous to GitHub rulesets.

    Args:
        metadata: Optional ``max_results`` limits list-style rule collections.
        session: GitLab session scoped to the current project.

    Returns:
        Push rule, approval rules, and protected-branch rules when available.
    """
    project = project_for_task(
        task_name="audit_rulesets",
        provider=provider,
        execution_target_id=execution_target_id,
        execution_target_type=execution_target_type,
        session=session,
    )
    protected = list_manager(
        task_name="audit_rulesets",
        manager=getattr(project, "protectedbranches", None),
        metadata=metadata,
    )
    approvals_manager = getattr(project, "approvalrules", None)
    approvals = (
        list_manager(
            task_name="audit_rulesets", manager=approvals_manager, metadata=metadata
        )
        if approvals_manager is not None
        else []
    )
    push_rule = None
    push_manager = getattr(project, "pushrules", None)
    get_push_rule = getattr(push_manager, "get", None)
    if callable(get_push_rule):
        try:
            push_rule = json_safe(get_push_rule())
        except Exception as error:
            if getattr(error, "response_code", None) != 404:
                raise
    __LOGGER__.info(f"Audited GitLab project rules for {execution_target_name}")
    actions.record(
        f"Audited GitLab project rules for {execution_target_id} region {region}"
    )
    return {
        "push_rule": push_rule,
        "approval_rules": approvals,
        "protected_branches": protected,
    }
