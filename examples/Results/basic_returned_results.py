"""
Example task that returns structured result data directly from run().
"""

from __future__ import annotations

import logging

from anvil.actions import ActionRecorder

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
        IAM group and access-key inventory for the configured user.

    Raises:
        RuntimeError: If `metadata.user_name` is not a string.
    """
    user_name = metadata.get("user_name")
    if not isinstance(user_name, str):
        raise RuntimeError("example_cleanup requires metadata.user_name to be a string")

    iam = session.client("iam")
    groups = [
        group["GroupName"]
        for group in iam.list_groups_for_user(UserName=user_name)["Groups"]
    ]
    access_key_ids = [
        key["AccessKeyId"]
        for key in iam.list_access_keys(UserName=user_name)["AccessKeyMetadata"]
    ]

    __LOGGER__.info(
        f"Inspected IAM resources for user {user_name} in account "
        f"{execution_target_name} ({execution_target_id}), dry_run={dry_run}"
    )

    return {
        "user_name": user_name,
        "dry_run": dry_run,
        "groups": groups,
        "access_key_ids": access_key_ids,
        "summary": {"groups": len(groups), "access_keys": len(access_key_ids)},
    }
