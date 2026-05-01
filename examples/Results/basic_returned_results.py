"""
Example task that returns structured result data directly from run().
"""

from __future__ import annotations

import logging

__LOGGER__ = logging.getLogger(__name__)


def run(
    *,
    account_id: str,
    account_alias: str,
    session,
    dry_run: bool,
    metadata: dict[str, object],
    actions=None,
) -> dict[str, object]:
    """
    Return JSON-serializable task data for normal Anvil result output.
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
        f"{account_alias} ({account_id}), dry_run={dry_run}"
    )

    return {
        "user_name": user_name,
        "dry_run": dry_run,
        "groups": groups,
        "access_key_ids": access_key_ids,
        "summary": {"groups": len(groups), "access_keys": len(access_key_ids)},
    }
