"""
List GitHub code scanning alerts for the current organization or repository target.
"""

from __future__ import annotations

import logging

from anvil.actions import ActionRecorder
from anvil.providers.github.tasks._rest import (
    DEFAULT_MAX_RESULTS,
    alert_endpoint,
    list_rest_items,
    metadata_params,
    require_github_provider,
)
from anvil.providers.tasks._task_helpers import metadata_int

__LOGGER__ = logging.getLogger(__name__)

TASK_NAME = "list_code_scanning_alert"
ALLOWED_FILTERS = (
    "tool_name",
    "tool_guid",
    "ref",
    "state",
    "severity",
    "sort",
    "direction",
)


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
    """List code scanning alerts for a GitHub target."""

    require_github_provider(task_name=TASK_NAME, provider=provider)
    max_results = metadata_int(
        task_name=TASK_NAME,
        metadata=metadata,
        key="max_results",
        default=DEFAULT_MAX_RESULTS,
    )
    params = metadata_params(
        task_name=TASK_NAME, metadata=metadata, allowed_keys=ALLOWED_FILTERS
    )
    endpoint = alert_endpoint(
        task_name=TASK_NAME,
        execution_target_id=execution_target_id,
        execution_target_type=execution_target_type,
        suffix="code-scanning/alerts",
    )
    alerts = list_rest_items(
        session=session, path=endpoint, params=params, max_results=max_results
    )

    __LOGGER__.info(
        f"Listed {len(alerts)} GitHub code scanning alert(s) for "
        f"{execution_target_type} {execution_target_name} region={region}"
    )
    actions.record(
        f"Listed {len(alerts)} GitHub code scanning alert(s) for "
        f"{execution_target_type} {execution_target_id} region {region}"
    )
    return {"alert_count": len(alerts), "alerts": alerts, "filters": params}
