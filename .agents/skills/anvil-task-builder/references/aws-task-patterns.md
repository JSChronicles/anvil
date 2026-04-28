# AWS Task Patterns

## Read-Only Inventory Tasks

- Use the scoped session to build clients.
- Use paginators where available.
- Return counts and identifiers that are useful in downstream JSON output.
- Record a concise action summary.

## Mutating Tasks

- Gather current state first.
- Record what would change.
- If `dry_run` is true, return planned changes without calling mutating APIs.
- If `dry_run` is false, execute the smallest necessary AWS calls.
- Return both changed and skipped items where practical.
- Put detailed per-resource messages in helper functions at `debug` level when they are useful for troubleshooting.
- Record the high-level planned or completed action once with `actions.record`.

## Cleanup-Style Task Pattern

```python
def cleanup_user_resources(iam_client, user_name: str, dry_run: bool) -> None:
    for key in iam_client.list_access_keys(UserName=user_name).get(
        "AccessKeyMetadata", []
    ):
        key_id = key["AccessKeyId"]
        if dry_run:
            __LOGGER__.debug(f"(dry-run) Would delete access key: {key_id}")
        else:
            iam_client.delete_access_key(UserName=user_name, AccessKeyId=key_id)
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
        raise RuntimeError("remove_iam_user requires metadata.user_name to be a string")

    iam_client = session.client("iam")
    cleanup_user_resources(iam_client=iam_client, user_name=user_name, dry_run=dry_run)

    if dry_run:
        actions.record(f"(dry-run) Would remove IAM user resources for {user_name}")
    else:
        actions.record(f"Removed IAM user resources for {user_name}")
```