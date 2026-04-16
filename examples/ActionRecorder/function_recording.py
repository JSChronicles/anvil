import logging

from anvil.actions import ActionRecorder

__LOGGER__ = logging.getLogger(__name__)


def cleanup_user_resources(iam_client, user_name: str, dry_run: bool) -> None:
    # groups
    for group in iam_client.list_groups_for_user(UserName=user_name)["Groups"]:
        group_name = group["GroupName"]
        if dry_run:
            __LOGGER__.debug(f"(dry-run) Would remove user from group: {group_name}")
        else:
            iam_client.remove_user_from_group(
                GroupName=group_name, UserName=user_name
            )
            __LOGGER__.debug(f"Removed user from group: {group_name}")

    # access keys
    for key in iam_client.list_access_keys(UserName=user_name)["AccessKeyMetadata"]:
        key_id = key["AccessKeyId"]
        if dry_run:
            __LOGGER__.debug(f"(dry-run) Would delete access key: {key_id}")
        else:
            iam_client.delete_access_key(
                UserName=user_name, AccessKeyId=key_id
            )
            __LOGGER__.debug(f"Deleted access key: {key_id}")


def run(
    *,
    account_id: str,
    account_alias: str,
    session,
    dry_run: bool,
    metadata: dict[str, object],
    actions: ActionRecorder,
) -> None:
    user_name = metadata.get("user_name")
    if not isinstance(user_name, str):
        raise RuntimeError("example_cleanup requires metadata.user_name to be a string")

    iam = session.client("iam")
    cleanup_user_resources(iam_client=iam, user_name=user_name, dry_run=dry_run)

    if dry_run:
        actions.record(f"(dry-run) Would clean IAM resources for {user_name}")
    else:
        actions.record(f"Cleaned IAM resources for {user_name}")
