"""
A noop task is useful for:
- Validating org access (STS + Organizations)
- Testing include/exclude logic
- Testing concurrency behavior
- Testing logging and output shape
- CI smoke tests
- Running the framework without any side effects
"""

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
) -> dict:
    """Raise an intentional failure for error-path validation.

    The task performs no provider API mutations and always raises before returning.

    Args:
        account_id: Target account or execution target ID.
        account_alias: Friendly name for the target account.
        session: Provider session scoped to the current region or location.
        dry_run: Whether execution is running in dry-run mode.
        metadata: Arbitrary config metadata for the task.
        actions: Action recorder provided by the engine.

    Raises:
        RuntimeError: Always raised to exercise task failure handling.

    Returns:
        This task never returns successfully.
    """
    raise RuntimeError("Intentional noop failure for testing")
    __LOGGER__.info(
        f"Noop_fail task executed for account {account_alias} ({account_id}), "
        f"region={session.region_name}, dry_run={dry_run}"
    )

    return {"message": "noop", "account_id": account_id, "dry_run": dry_run}
