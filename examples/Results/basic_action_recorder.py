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
) -> None:
    """Check one IAM user and record the outcome as an audit action.

    Args:
        provider: Provider name for the execution target.
        execution_target_id: Provider-owned target identifier.
        execution_target_name: Target display name.
        execution_target_type: Provider-owned target type.
        region: Concrete execution region.
        session: AWS session scoped to the target and region.
        dry_run: Whether mutations must be simulated.
        metadata: Static task configuration with an optional `user_name`.
        dependency_data: Runtime dependency inputs; unused by this task.
        actions: Engine-provided action recorder.
    """

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
    except iam.exceptions.NoSuchEntityException:
        __LOGGER__.info(f"IAM user not found: {user_name}")
        actions.record(f"IAM user not found: {user_name}")
