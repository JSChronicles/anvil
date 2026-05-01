import logging

from anvil.actions import ActionRecorder

__LOGGER__ = logging.getLogger(__name__)


def cleanup_user_resources(
    iam_client, user_name: str, dry_run: bool, actions: ActionRecorder
) -> None:
    # groups
    for group in iam_client.list_groups_for_user(UserName=user_name)["Groups"]:
        group_name = group["GroupName"]
        if dry_run:
            message = f"(dry-run) Would remove user from group: {group_name}"
            __LOGGER__.debug(message)
            actions.record(message)
        else:
            iam_client.remove_user_from_group(GroupName=group_name, UserName=user_name)
            message = f"Removed user from group: {group_name}"
            __LOGGER__.debug(message)
            actions.record(message)

    # access keys
    for key in iam_client.list_access_keys(UserName=user_name)["AccessKeyMetadata"]:
        key_id = key["AccessKeyId"]
        if dry_run:
            message = f"(dry-run) Would delete access key: {key_id}"
            __LOGGER__.debug(message)
            actions.record(message)
        else:
            iam_client.delete_access_key(UserName=user_name, AccessKeyId=key_id)
            message = f"Deleted access key: {key_id}"
            __LOGGER__.debug(message)
            actions.record(message)


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
    cleanup_user_resources(
        iam_client=iam, user_name=user_name, dry_run=dry_run, actions=actions
    )
