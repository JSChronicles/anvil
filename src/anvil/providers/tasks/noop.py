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
    provider: str,
    execution_target_id: str,
    execution_target_name: str,
    execution_target_type: str,
    region: str,
    session,
    dry_run: bool,
    metadata: dict[str, object],
    actions: ActionRecorder,
) -> dict:
    """Run a no-op task for validation, smoke tests, and framework checks.

    The task performs no provider API mutations and ignores task metadata.

    Args:
        provider: Provider name for the current execution target.
        execution_target_id: Provider-specific target ID.
        execution_target_name: Display name for the current target.
        execution_target_type: Provider-specific target type.
        region: Current execution region.
        session: Provider session scoped to the current region.
        dry_run: Whether execution is running in dry-run mode.
        metadata: Arbitrary config metadata for the task.
        actions: Action recorder provided by the engine.

    Returns:
        A small payload confirming execution context and dry-run state.
    """

    __LOGGER__.info(
        f"No-op task executed for {provider} {execution_target_type} "
        f"{execution_target_name} ({execution_target_id}), "
        f"region={region}, dry_run={dry_run}"
    )
    return {
        "message": "noop",
        "execution_target_id": execution_target_id,
        "dry_run": dry_run,
    }
