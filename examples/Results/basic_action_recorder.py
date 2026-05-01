import logging

from anvil.actions import ActionRecorder

__LOGGER__ = logging.getLogger(__name__)


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
    user_name = metadata.get("user_name", "example")

    if dry_run:
        message = f"(dry-run) Would check IAM user: {user_name}"
        __LOGGER__.info(message)
        actions.record(message)
        return

    try:
        iam.get_user(UserName=user_name)
        __LOGGER__.info(f"IAM user exists: {user_name}")
        actions.record(f"IAM user exists: {user_name}")
    except Exception:
        __LOGGER__.info(f"IAM user not found: {user_name}")
        actions.record(f"IAM user not found: {user_name}")
