"""List GitLab secret-detection vulnerabilities for the current project."""

import logging
from anvil.actions import ActionRecorder
from anvil.providers.gitlab.tasks._api import list_vulnerability_alerts

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
    """List secret vulnerabilities analogous to GitHub secret-scanning alerts.

    Args:
        metadata: Optional ``state``, ``severity``, and ``max_results`` filters.
        session: GitLab session scoped to the current project.

    Returns:
        Alert count and secret-detection vulnerability records.
    """
    alerts = list_vulnerability_alerts(
        task_name="list_secret_scanning_alert",
        report_type="secret_detection",
        provider=provider,
        execution_target_id=execution_target_id,
        execution_target_type=execution_target_type,
        session=session,
        metadata=metadata,
    )
    __LOGGER__.info(
        f"Listed {len(alerts)} GitLab secret-scanning alert(s) for {execution_target_name}"
    )
    actions.record(
        f"Listed {len(alerts)} GitLab secret-scanning alert(s) for {execution_target_id} region {region}"
    )
    return {
        "alert_count": len(alerts),
        "alerts": alerts,
        "report_type": "secret_detection",
    }
