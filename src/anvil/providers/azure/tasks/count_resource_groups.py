"""
Count Azure resource groups in the current subscription.
"""

import logging
from collections.abc import Mapping

from anvil.actions import ActionRecorder

__LOGGER__ = logging.getLogger(__name__)
AZURE_EXTRA_REMEDIATION = (
    "Install Azure dependencies with 'uv sync --extra azure' for a source checkout "
    "or 'pip install \"anvil[azure]\"' for an installed package."
)

MAX_LISTED_RESOURCE_GROUPS = 100


def _get_text_value(resource_group: object, key: str) -> str | None:
    """Read a text value from an Azure SDK model or mapping."""

    if isinstance(resource_group, Mapping):
        value = resource_group.get(key)
    else:
        value = getattr(resource_group, key, None)
    return value if isinstance(value, str) else None


def _resource_group_summary(resource_group: object) -> dict[str, str | None]:
    """Return JSON-serializable summary data for one resource group."""

    return {
        "name": _get_text_value(resource_group, "name"),
        "location": _get_text_value(resource_group, "location"),
        "id": _get_text_value(resource_group, "id"),
    }


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
) -> dict[str, object]:
    """Count resource groups in the current Azure subscription.

    This is a read-only Azure task. It lists resource groups for the current
    subscription and includes individual resource group summaries when the count
    is at or below the task's listing threshold. Task metadata is ignored.

    Args:
        provider: Provider name for the current execution target.
        execution_target_id: Current subscription ID.
        execution_target_name: Current subscription display name or ID.
        execution_target_type: Provider target type.
        region: Current Anvil execution region value.
        session: Azure session scoped to the subscription and region.
        dry_run: Whether execution is running in dry-run mode.
        metadata: Arbitrary config metadata for the task.
        actions: Action recorder provided by the engine.

    Returns:
        Structured resource group count data for the subscription.

    Raises:
        RuntimeError: If the task is used outside Azure subscription execution
            or if the Azure resource SDK dependency is unavailable.
    """

    if provider != "azure":
        raise RuntimeError("count_resource_groups requires the azure provider")
    if execution_target_type != "subscription":
        raise RuntimeError(
            "count_resource_groups requires an Azure subscription execution target"
        )

    subscription_id = getattr(session, "subscription_id", execution_target_id)
    credential = getattr(session, "credential", None)
    if not isinstance(subscription_id, str) or not subscription_id.strip():
        raise RuntimeError("count_resource_groups requires an Azure subscription ID")
    if credential is None:
        raise RuntimeError("count_resource_groups requires an Azure session credential")

    try:
        from azure.mgmt.resource import ResourceManagementClient
    except ImportError as error:
        raise RuntimeError(
            "count_resource_groups requires optional dependency "
            f"'azure-mgmt-resource'. {AZURE_EXTRA_REMEDIATION}"
        ) from error

    client = ResourceManagementClient(credential, subscription_id.strip())
    resource_groups = list(client.resource_groups.list())
    resource_group_count = len(resource_groups)

    result: dict[str, object] = {
        "subscription_id": subscription_id.strip(),
        "region": region,
        "resource_group_count": resource_group_count,
    }
    if resource_group_count <= MAX_LISTED_RESOURCE_GROUPS:
        result["resource_groups"] = [
            _resource_group_summary(resource_group)
            for resource_group in resource_groups
        ]

    dry_run_suffix = " during dry-run" if dry_run else ""
    __LOGGER__.info(
        f"Counted {resource_group_count} Azure resource group(s) in subscription "
        f"{execution_target_name} ({subscription_id}) region={region}"
        f"{dry_run_suffix}; no mutations are performed by this read-only task"
    )
    actions.record(
        f"Counted {resource_group_count} Azure resource group(s) in subscription "
        f"{subscription_id.strip()} region {region}"
    )

    return result
