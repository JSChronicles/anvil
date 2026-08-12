"""
Example task that returns structured result data without ActionRecorder.
"""

from __future__ import annotations

import logging

from anvil.actions import ActionRecorder

__LOGGER__ = logging.getLogger(__name__)


def cleanup_user_resources(
    iam_client, user_name: str, dry_run: bool
) -> dict[str, object]:
    """
    Return JSON-serializable cleanup details instead of recording actions.
    """
    group_results: list[dict[str, object]] = []
    access_key_results: list[dict[str, object]] = []

    # groups
    for group in iam_client.list_groups_for_user(UserName=user_name)["Groups"]:
        group_name = group["GroupName"]
        if dry_run:
            message = f"(dry-run) Would remove user from group: {group_name}"
            status = "planned"
        else:
            iam_client.remove_user_from_group(GroupName=group_name, UserName=user_name)
            message = f"Removed user from group: {group_name}"
            status = "completed"

        __LOGGER__.debug(message)
        group_results.append(
            {"group_name": group_name, "status": status, "message": message}
        )

    # access keys
    for key in iam_client.list_access_keys(UserName=user_name)["AccessKeyMetadata"]:
        key_id = key["AccessKeyId"]
        if dry_run:
            message = f"(dry-run) Would delete access key: {key_id}"
            status = "planned"
        else:
            iam_client.delete_access_key(UserName=user_name, AccessKeyId=key_id)
            message = f"Deleted access key: {key_id}"
            status = "completed"

        __LOGGER__.debug(message)
        access_key_results.append(
            {"access_key_id": key_id, "status": status, "message": message}
        )

    return {
        "user_name": user_name,
        "dry_run": dry_run,
        "groups": group_results,
        "access_keys": access_key_results,
        "summary": {
            "groups": len(group_results),
            "access_keys": len(access_key_results),
        },
    }


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
    """Return JSON-serializable task data for normal Anvil result output.

    Args:
        provider: Provider name for the execution target.
        execution_target_id: Provider-owned target identifier.
        execution_target_name: Target display name.
        execution_target_type: Provider-owned target type.
        region: Concrete execution region.
        session: AWS session scoped to the target and region.
        dry_run: Whether mutations must be simulated.
        metadata: Static task configuration requiring `user_name`.
        dependency_data: Runtime dependency inputs; unused by this task.
        actions: Engine-provided action recorder.

    Returns:
        Planned or completed IAM cleanup details.

    Raises:
        RuntimeError: If `metadata.user_name` is not a string.
    """
    user_name = metadata.get("user_name")
    if not isinstance(user_name, str):
        raise RuntimeError("example_cleanup requires metadata.user_name to be a string")

    iam = session.client("iam")
    return cleanup_user_resources(iam_client=iam, user_name=user_name, dry_run=dry_run)
