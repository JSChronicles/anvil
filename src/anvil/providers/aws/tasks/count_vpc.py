"""
Count VPCs in the current execution region.
"""

import logging

from anvil.actions import ActionRecorder

__LOGGER__ = logging.getLogger(__name__)


def run(
    *,
    account_id: str,
    account_alias: str,
    session,
    dry_run: bool,
    metadata: dict[str, object],
    actions: ActionRecorder,
) -> dict:
    """
    Count VPCs in the session's current AWS region.

    Args:
        account_id: Target AWS account ID.
        account_alias: Friendly name for the target account.
        session: Boto3 session scoped to the current region.
        dry_run: Whether execution is running in dry-run mode.
        metadata: Arbitrary config metadata for the task.
        actions: Action recorder provided by the engine.

    Returns:
        A dictionary containing the VPC count and basic execution context.
    """
    region_name = session.region_name
    ec2_client = session.client("ec2")

    paginator = ec2_client.get_paginator("describe_vpcs")

    vpc_ids: list[str] = []
    for page in paginator.paginate():
        for vpc in page.get("Vpcs", []):
            vpc_id = vpc.get("VpcId")
            if vpc_id:
                vpc_ids.append(vpc_id)

    vpc_count = len(vpc_ids)

    __LOGGER__.info(
        f"Counted {vpc_count} VPC(s) in account {account_alias} ({account_id}), "
        f"region={region_name}, dry_run={dry_run}"
    )

    actions.record(
        f"Counted {vpc_count} VPC(s) in account {account_id} region {region_name}"
    )

    return {"region": region_name, "vpc_count": vpc_count, "vpc_ids": vpc_ids}
