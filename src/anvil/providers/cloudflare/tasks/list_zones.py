"""List zones available to the current Cloudflare account target."""

import logging

from anvil.actions import ActionRecorder
from anvil.providers.tasks._task_helpers import (
    bounded,
    metadata_int,
    require_provider,
    require_target_type,
)

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
) -> dict[str, object]:
    """List Cloudflare zones for an account target.

    Args:
        metadata: Optional ``max_results`` limits returned zones.
        session: Cloudflare session scoped to the current account.

    Returns:
        Zone count and JSON-serializable zone records.

    Raises:
        RuntimeError: If invoked outside a Cloudflare account target.
    """
    require_provider(task_name="list_zones", provider=provider, expected="cloudflare")
    require_target_type(
        task_name="list_zones",
        execution_target_type=execution_target_type,
        expected="account",
    )
    max_results = metadata_int(metadata=metadata, key="max_results")
    resource = getattr(session.client, "zones", None)
    operation = getattr(resource, "list", None)
    if not callable(operation):
        raise RuntimeError("list_zones requires Cloudflare zones.list()")
    zones = bounded(
        operation(account={"id": execution_target_id}, per_page=50),
        max_results=max_results,
    )
    __LOGGER__.info(
        f"Listed {len(zones)} Cloudflare zone(s) for account {execution_target_name}"
    )
    actions.record(
        f"Listed {len(zones)} Cloudflare zone(s) for account {execution_target_id} region {region}"
    )
    return {"zone_count": len(zones), "zones": zones}
