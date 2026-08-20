"""List members of the current GitLab group or project."""

import logging
from anvil.actions import ActionRecorder
from anvil.providers.gitlab.tasks._membership import list_members, resource_for_task
from anvil.providers.tasks._task_helpers import (
    mapping_identifier,
    metadata_string_array,
)

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
    """List GitLab members, optionally restricted by numeric user ID.

    Args:
        metadata: Optional ``members`` user-ID array and ``max_results``.
        session: GitLab session scoped to a group or project target.

    Returns:
        Matching memberships and unmatched user IDs.
    """
    resource = resource_for_task(
        task_name="list_member",
        provider=provider,
        execution_target_id=execution_target_id,
        execution_target_type=execution_target_type,
        session=session,
    )
    members = list_members(resource=resource, metadata=metadata)
    selectors = metadata_string_array(
        task_name="list_member", metadata=metadata, key="members"
    )
    if selectors is None:
        matched, unmatched = members, []
    else:
        by_id = {
            identifier: item
            for item in members
            if (identifier := mapping_identifier(item)) is not None
        }
        matched = [by_id[item] for item in selectors if item in by_id]
        unmatched = [item for item in selectors if item not in by_id]
    __LOGGER__.info(
        f"Listed {len(matched)} GitLab member(s) for {execution_target_name}"
    )
    actions.record(
        f"Listed {len(matched)} GitLab member(s) for {execution_target_id} region {region}"
    )
    return {
        "member_count": len(matched),
        "members": matched,
        "unmatched_members": unmatched,
    }
