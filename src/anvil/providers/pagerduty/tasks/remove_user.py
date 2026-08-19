"""Remove a PagerDuty user from the current account."""

import logging
from anvil.actions import ActionRecorder
from anvil.providers.tasks._task_helpers import metadata_string, require_provider

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
    """Remove one PagerDuty user by ID with dry-run protection.

    Args:
        dry_run: When true, report the planned deletion without calling PagerDuty.
        metadata: Requires ``user_id`` containing the PagerDuty user ID.
        session: PagerDuty REST session for the current account.

    Returns:
        User ID plus planned/deleted status.

    Raises:
        RuntimeError: If ``metadata.user_id`` is absent or the client cannot delete.
    """
    require_provider(task_name="remove_user", provider=provider, expected="pagerduty")
    user_id = metadata_string(
        task_name="remove_user", metadata=metadata, key="user_id", required=True
    )
    assert user_id is not None
    if dry_run:
        message = f"(dry-run) Would remove PagerDuty user {user_id} from account {execution_target_id}"
        __LOGGER__.info(message)
        actions.record(message)
        return {"user_id": user_id, "planned": True, "deleted": False}
    operation = getattr(session.client, "rdelete", None)
    if not callable(operation):
        raise RuntimeError("remove_user requires PagerDuty client.rdelete()")
    operation(f"users/{user_id}")
    message = f"Removed PagerDuty user {user_id} from account {execution_target_id}"
    __LOGGER__.info(message)
    actions.record(message)
    return {"user_id": user_id, "planned": False, "deleted": True}
