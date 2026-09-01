"""List PagerDuty escalation policies for the current account."""

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
    """List PagerDuty escalation policies.

    Args:
        metadata: Optional ``max_results`` limits returned policies.
        session: PagerDuty REST session for the current account.

    Returns:
        Policy count and JSON-serializable escalation policies.
    """
    require_provider(
        task_name="list_escalation_policy", provider=provider, expected="pagerduty"
    )
    items = list_resources(
        task_name="list_escalation_policy",
        session=session,
        resource="escalation_policies",
        metadata=metadata,
    )
    __LOGGER__.info(
        f"Listed {len(items)} PagerDuty escalation policy/policies for {execution_target_name}"
    )
    actions.record(
        f"Listed {len(items)} PagerDuty escalation policy/policies for {execution_target_id} region {region}"
    )
    return {"escalation_policy_count": len(items), "escalation_policies": items}
