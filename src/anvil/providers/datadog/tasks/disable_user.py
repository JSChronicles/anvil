"""Disable users in the configured Datadog organization."""

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
    """Disable one or more Datadog users.

    Args:
        metadata: Requires ``users`` as a non-empty array of Datadog user IDs.
        dry_run: Report planned disables without calling Datadog when true.

    Returns:
        Per-user statuses and summary counts.

    Raises:
        RuntimeError: If ``metadata.users`` is invalid.
        TaskExecutionError: If one or more disable operations fail.
    """

    require_provider(task_name="disable_user", provider=provider, expected="datadog")
    users = metadata_string_array(
        task_name="disable_user", metadata=metadata, key="users", required=True
    )
    assert users is not None
    if not dry_run:
        from datadog_api_client.v2.api.users_api import UsersApi

        api = UsersApi(session.client)
    results: list[dict[str, object]] = []
    for user_id in users:
        if dry_run:
            status = "planned"
            message = f"(dry-run) Would disable Datadog user {user_id}"
        else:
            try:
                api.disable_user(user_id)
            except Exception as error:
                results.append(
                    {"id": user_id, "status": "failed", "error": type(error).__name__}
                )
                continue
            status = "disabled"
            message = f"Disabled Datadog user {user_id}"
        __LOGGER__.info(message)
        actions.record(message)
        results.append({"id": user_id, "status": status})
    disabled = sum(item["status"] == "disabled" for item in results)
    failed = sum(item["status"] == "failed" for item in results)
    result = {
        "requested_count": len(users),
        "planned_count": len(users) if dry_run else 0,
        "disabled_count": disabled,
        "failed_count": failed,
        "users": results,
    }
    if failed:
        raise TaskExecutionError(
            "disable_user failed for one or more Datadog users", partial_result=result
        )
    return result
