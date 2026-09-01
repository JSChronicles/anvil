"""List Datadog users for the configured organization."""

import logging
from collections.abc import Iterable

from anvil.actions import ActionRecorder
from anvil.providers.tasks._task_helpers import (
    bounded,
    json_safe,
    mapping_identifier,
    metadata_int,
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
    """List Datadog users, optionally restricted by user IDs.

    Args:
        metadata: Optional ``users`` array and ``max_results`` limit.
        session: Datadog SDK session for the configured organization.

    Returns:
        Matching users and unmatched user IDs.
    """

    require_provider(task_name="list_user", provider=provider, expected="datadog")
    from datadog_api_client.v2.api.users_api import UsersApi

    maximum = metadata_int(task_name="list_user", metadata=metadata, key="max_results")
    selectors = metadata_string_array(
        task_name="list_user", metadata=metadata, key="users"
    )
    response = UsersApi(session.client).list_users(page_size=maximum)
    raw = getattr(response, "data", None)
    if raw is None:
        serialized = json_safe(response)
        raw = serialized.get("data", []) if isinstance(serialized, dict) else []
    if not isinstance(raw, Iterable) or isinstance(raw, str | bytes | dict):
        raise RuntimeError("list_user received an invalid Datadog user collection")
    users = bounded(raw, max_results=maximum)
    if selectors is None:
        matched = users
        unmatched: list[str] = []
    else:
        by_id = {
            identifier: user
            for user in users
            if (identifier := mapping_identifier(user)) is not None
        }
        matched = [by_id[item] for item in selectors if item in by_id]
        unmatched = [item for item in selectors if item not in by_id]
    __LOGGER__.info(
        f"Listed {len(matched)} Datadog user(s) for {execution_target_name}"
    )
    actions.record(
        f"Listed {len(matched)} Datadog user(s) for {execution_target_id} region {region}"
    )
    return {"user_count": len(matched), "users": matched, "unmatched_users": unmatched}
