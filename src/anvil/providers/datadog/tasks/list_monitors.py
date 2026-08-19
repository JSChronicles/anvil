"""List Datadog monitors for the configured organization."""

import logging
from anvil.actions import ActionRecorder
from anvil.providers.tasks._task_helpers import bounded, metadata_int, require_provider

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
    """List Datadog monitors for auditing.

    Args:
        metadata: Optional ``max_results`` limits returned monitors.
        session: Datadog session for the configured organization.

    Returns:
        Monitor count and JSON-serializable monitor definitions.
    """
    require_provider(task_name="list_monitors", provider=provider, expected="datadog")
    from datadog_api_client.v1.api.monitors_api import MonitorsApi

    max_results = metadata_int(metadata=metadata, key="max_results")
    monitors = bounded(
        MonitorsApi(session.client).list_monitors(), max_results=max_results
    )
    __LOGGER__.info(
        f"Listed {len(monitors)} Datadog monitor(s) for {execution_target_name}"
    )
    actions.record(
        f"Listed {len(monitors)} Datadog monitor(s) for {execution_target_id} region {region}"
    )
    return {"monitor_count": len(monitors), "monitors": monitors}
