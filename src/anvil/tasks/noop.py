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
    """
    No-op task used for validation and testing.

    This task performs no AWS actions.
    """

    __LOGGER__.info(
        f"No-op task executed for account {account_alias} ({account_id}), "
        f"region={session.region_name}, dry_run={dry_run}"
    )
    return {"message": "noop", "account_id": account_id, "dry_run": dry_run}
