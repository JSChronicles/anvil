import logging

from anvil.actions import ActionRecorder

LOGGER = logging.getLogger(__name__)


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
        actions.record(f"Would check IAM user: {user_name}")
        return

    try:
        iam.get_user(UserName=user_name)
        actions.record(f"IAM user exists: {user_name}")
    except Exception:
        actions.record(f"IAM user not found: {user_name}")
