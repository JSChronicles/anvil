"""List PagerDuty services for the current account."""

import logging
from anvil.actions import ActionRecorder
from anvil.providers.pagerduty.tasks._rest import list_resources
from anvil.providers.tasks._task_helpers import require_provider

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
    """List PagerDuty services.

    Args:
        metadata: Optional ``max_results`` limits returned services.
        session: PagerDuty REST session for the current account.

    Returns:
        Service count and JSON-serializable services.
    """
    require_provider(task_name="list_service", provider=provider, expected="pagerduty")
    items = list_resources(
        task_name="list_service",
        session=session,
        resource="services",
        metadata=metadata,
    )
    __LOGGER__.info(
        f"Listed {len(items)} PagerDuty service(s) for {execution_target_name}"
    )
    actions.record(
        f"Listed {len(items)} PagerDuty service(s) for {execution_target_id} region {region}"
    )
    return {"service_count": len(items), "services": items}
