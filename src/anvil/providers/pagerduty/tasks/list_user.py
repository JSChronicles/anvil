"""List PagerDuty users for the current account."""

import logging

from anvil.actions import ActionRecorder
from anvil.providers.pagerduty.tasks._rest import list_resources
from anvil.providers.tasks._task_helpers import (
    mapping_identifier,
    metadata_string_array,
    require_provider,
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
    """List PagerDuty users, optionally restricted by user IDs.

    Args:
        metadata: Optional ``users`` array of PagerDuty user IDs and optional
            ``max_results``.
        session: PagerDuty REST client session for the current account.

    Returns:
        Matching users and selectors that were not found.

    Raises:
        RuntimeError: If ``metadata.users`` is not an array of strings.
    """

    require_provider(task_name="list_user", provider=provider, expected="pagerduty")
    selectors = metadata_string_array(
        task_name="list_user", metadata=metadata, key="users"
    )
    users = list_resources(
        task_name="list_user", session=session, resource="users", metadata=metadata
    )
    if selectors is None:
        matched = users
        unmatched: list[str] = []
    else:
        by_id = {
            identifier: user
            for user in users
            if (identifier := mapping_identifier(user)) is not None
        }
        matched = [by_id[user_id] for user_id in selectors if user_id in by_id]
        unmatched = [user_id for user_id in selectors if user_id not in by_id]

    __LOGGER__.info(
        f"Listed {len(matched)} PagerDuty user(s) for {execution_target_name}"
    )
    actions.record(
        f"Listed {len(matched)} PagerDuty user(s) for {execution_target_id} region {region}"
    )
    return {"user_count": len(matched), "users": matched, "unmatched_users": unmatched}
