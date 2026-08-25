"""Remove a PagerDuty user from the current account."""

import logging
from anvil.actions import ActionRecorder
from anvil.providers.tasks._task_helpers import metadata_string_array, require_provider
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
    """Remove one or more PagerDuty users by ID with dry-run protection.

    Args:
        dry_run: When true, report the planned deletion without calling PagerDuty.
        metadata: Requires ``users`` as an array of PagerDuty user IDs.
        session: PagerDuty REST session for the current account.

    Returns:
        Per-user planned, removed, or failed status plus summary counts.

    Raises:
        RuntimeError: If ``metadata.users`` is absent or invalid.
        TaskExecutionError: If one or more PagerDuty deletions fail.
    """
    require_provider(task_name="remove_user", provider=provider, expected="pagerduty")
    users = metadata_string_array(
        task_name="remove_user", metadata=metadata, key="users", required=True
    )
    assert users is not None
    results: list[dict[str, object]] = []
    if dry_run:
        for user_id in users:
            message = (
                f"(dry-run) Would remove PagerDuty user {user_id} from account "
                f"{execution_target_id}"
            )
            __LOGGER__.info(message)
            actions.record(message)
            results.append({"id": user_id, "status": "planned"})
        return {
            "requested_count": len(users),
            "planned_count": len(users),
            "removed_count": 0,
            "failed_count": 0,
            "users": results,
        }
    operation = getattr(session.client, "rdelete", None)
    if not callable(operation):
        raise RuntimeError("remove_user requires PagerDuty client.rdelete()")
    for user_id in users:
        try:
            operation(f"users/{user_id}")
        except Exception as error:
            __LOGGER__.error(
                f"Failed to remove PagerDuty user {user_id}: {type(error).__name__}"
            )
            results.append(
                {"id": user_id, "status": "failed", "error": type(error).__name__}
            )
            continue
        message = f"Removed PagerDuty user {user_id} from account {execution_target_id}"
        __LOGGER__.info(message)
        actions.record(message)
        results.append({"id": user_id, "status": "removed"})

    removed_count = sum(item["status"] == "removed" for item in results)
    failed_count = len(results) - removed_count
    result = {
        "requested_count": len(users),
        "planned_count": 0,
        "removed_count": removed_count,
        "failed_count": failed_count,
        "users": results,
    }
    if failed_count:
        raise TaskExecutionError(
            "remove_user failed for one or more PagerDuty users", partial_result=result
        )
    return result
