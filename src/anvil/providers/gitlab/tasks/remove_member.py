"""Remove members from the current GitLab group or project."""

import logging
from anvil.actions import ActionRecorder
from anvil.providers.gitlab.tasks._membership import resource_for_task
from anvil.providers.tasks._task_helpers import metadata_string_array
from anvil.task_errors import TaskExecutionError

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
    """Remove one or more direct GitLab members by numeric user ID.

    Args:
        metadata: Requires ``members`` as a non-empty user-ID array.
        dry_run: Report planned removals without calling GitLab when true.

    Returns:
        Per-member statuses and summary counts.
    """
    resource = resource_for_task(
        task_name="remove_member",
        provider=provider,
        execution_target_id=execution_target_id,
        execution_target_type=execution_target_type,
        session=session,
    )
    selectors = metadata_string_array(
        task_name="remove_member", metadata=metadata, key="members", required=True
    )
    assert selectors is not None
    operation = getattr(getattr(resource, "members", None), "delete", None)
    if not callable(operation):
        raise RuntimeError("remove_member requires python-gitlab members.delete()")
    results: list[dict[str, object]] = []
    for member_id in selectors:
        if not member_id.isdigit() or int(member_id) <= 0:
            raise RuntimeError(
                "remove_member metadata.members must contain numeric GitLab user IDs"
            )
        if dry_run:
            status = "planned"
            message = f"(dry-run) Would remove GitLab member {member_id} from {execution_target_id}"
        else:
            try:
                operation(int(member_id))
            except Exception as error:
                results.append(
                    {"id": member_id, "status": "failed", "error": type(error).__name__}
                )
                continue
            status = "removed"
            message = f"Removed GitLab member {member_id} from {execution_target_id}"
        __LOGGER__.info(message)
        actions.record(message)
        results.append({"id": member_id, "status": status})
    failed = sum(item["status"] == "failed" for item in results)
    result = {
        "requested_count": len(selectors),
        "planned_count": sum(item["status"] == "planned" for item in results),
        "removed_count": sum(item["status"] == "removed" for item in results),
        "failed_count": failed,
        "members": results,
    }
    if failed:
        raise TaskExecutionError(
            "remove_member failed for one or more GitLab members", partial_result=result
        )
    return result
