"""List DNS records for the current Cloudflare zone target."""

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
    """List DNS records for a Cloudflare zone.

    Args:
        metadata: Optional ``max_results`` limits returned records.
        session: Cloudflare session scoped to the current zone.

    Returns:
        DNS-record count and JSON-serializable records.

    Raises:
        RuntimeError: If invoked outside a Cloudflare zone target.
    """
    require_provider(
        task_name="list_dns_record", provider=provider, expected="cloudflare"
    )
    require_target_type(
        task_name="list_dns_record",
        execution_target_type=execution_target_type,
        expected="zone",
    )
    max_results = metadata_int(metadata=metadata, key="max_results")
    dns = getattr(session.client, "dns", None)
    records = getattr(dns, "records", None)
    operation = getattr(records, "list", None)
    if not callable(operation):
        raise RuntimeError("list_dns_record requires Cloudflare dns.records.list()")
    items = bounded(
        operation(zone_id=execution_target_id, per_page=100), max_results=max_results
    )
    __LOGGER__.info(
        f"Listed {len(items)} Cloudflare DNS record(s) for zone {execution_target_name}"
    )
    actions.record(
        f"Listed {len(items)} Cloudflare DNS record(s) for zone {execution_target_id} region {region}"
    )
    return {"record_count": len(items), "records": items}
