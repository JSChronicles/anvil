"""List Datadog dashboards for the configured organization."""

import logging
from collections.abc import Iterable
from anvil.actions import ActionRecorder
from anvil.providers.tasks._task_helpers import (
    bounded,
    json_safe,
    metadata_int,
    require_provider,
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
    """List Datadog dashboards for auditing.

    Args:
        metadata: Optional ``max_results`` limits returned dashboards.
        session: Datadog session for the configured organization.

    Returns:
        Dashboard count and JSON-serializable dashboard summaries.
    """
    require_provider(task_name="list_dashboards", provider=provider, expected="datadog")
    from datadog_api_client.v1.api.dashboards_api import DashboardsApi

    max_results = metadata_int(metadata=metadata, key="max_results")
    response = DashboardsApi(session.client).list_dashboards(count=max_results)
    raw = getattr(response, "dashboards", None)
    if raw is None:
        serialized = json_safe(response)
        raw = serialized.get("dashboards", []) if isinstance(serialized, dict) else []
    if not isinstance(raw, Iterable) or isinstance(raw, str | bytes | dict):
        raise RuntimeError("list_dashboards received an invalid dashboard collection")
    dashboards = bounded(raw, max_results=max_results)
    __LOGGER__.info(
        f"Listed {len(dashboards)} Datadog dashboard(s) for {execution_target_name}"
    )
    actions.record(
        f"Listed {len(dashboards)} Datadog dashboard(s) for {execution_target_id} region {region}"
    )
    return {"dashboard_count": len(dashboards), "dashboards": dashboards}
