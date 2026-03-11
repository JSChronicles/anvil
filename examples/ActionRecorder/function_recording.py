from anvil.task_definition import ActionRecorder


def cleanup_user_resources(iam_client, user_name, dry_run, actions):
    # groups
    for group in iam_client.list_groups_for_user(UserName=user_name)["Groups"]:
        actions.record(f"Remove user from group: {group['GroupName']}")
        if not dry_run:
            iam_client.remove_user_from_group(
                GroupName=group["GroupName"], UserName=user_name
            )

    # access keys
    for key in iam_client.list_access_keys(UserName=user_name)["AccessKeyMetadata"]:
        actions.record(f"Delete access key: {key['AccessKeyId']}")
        if not dry_run:
            iam_client.delete_access_key(
                UserName=user_name, AccessKeyId=key["AccessKeyId"]
            )


def run(
    *,
    account_id: str,
    account_alias: str,
    session,
    dry_run: bool,
    metadata: dict[str, object],
    actions: ActionRecorder,
) -> None:

    iam = session.client("iam")
    cleanup_user_resources(iam, metadata["user_name"], dry_run, actions)
