"""Remove members from the current Cloudflare account."""

import logging

from anvil.actions import ActionRecorder
from anvil.providers.tasks._task_helpers import (
    metadata_string_array,
    require_provider,
    require_target_type,
)
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
    """Remove one or more Cloudflare account members.

    Args:
        metadata: Requires ``members`` as a non-empty array of member IDs.
        dry_run: Report removals without calling Cloudflare when true.

    Returns:
        Per-member statuses and removal summary counts.

    Raises:
        RuntimeError: If selectors or the SDK operation are unavailable.
        TaskExecutionError: If one or more member removals fail.
    """

    require_provider(
        task_name="remove_account_member", provider=provider, expected="cloudflare"
    )
    require_target_type(
        task_name="remove_account_member",
        execution_target_type=execution_target_type,
        expected="account",
    )
    members = metadata_string_array(
        task_name="remove_account_member",
        metadata=metadata,
        key="members",
        required=True,
    )
    assert members is not None
    operation = getattr(
        getattr(getattr(session.client, "accounts", None), "members", None),
        "delete",
        None,
    )
    if not callable(operation):
        raise RuntimeError(
            "remove_account_member requires Cloudflare accounts.members.delete()"
        )
    results: list[dict[str, object]] = []
    for member_id in members:
        if dry_run:
            status = "planned"
            message = (
                f"(dry-run) Would remove Cloudflare account member {member_id} "
                f"from account {execution_target_id}"
            )
        else:
            try:
                operation(member_id, account_id=execution_target_id)
            except Exception as error:
                results.append(
                    {"id": member_id, "status": "failed", "error": type(error).__name__}
                )
                continue
            status = "removed"
            message = (
                f"Removed Cloudflare account member {member_id} from account "
                f"{execution_target_id}"
            )
        __LOGGER__.info(message)
        actions.record(message)
        results.append({"id": member_id, "status": status})
    removed = sum(item["status"] == "removed" for item in results)
    failed = sum(item["status"] == "failed" for item in results)
    result = {
        "requested_count": len(members),
        "planned_count": len(members) if dry_run else 0,
        "removed_count": removed,
        "failed_count": failed,
        "members": results,
    }
    if failed:
        raise TaskExecutionError(
            "remove_account_member failed for one or more Cloudflare members",
            partial_result=result,
        )
    return result
