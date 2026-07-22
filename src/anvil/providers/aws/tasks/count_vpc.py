"""
Count VPCs in the current execution region.
"""

import logging

from anvil.actions import ActionRecorder

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
    actions: ActionRecorder,
) -> dict:
    """Count VPCs in the session's current AWS region.

    This is a read-only AWS task. It uses EC2 `describe_vpcs` pagination and
    ignores task metadata.

    Args:
        provider: Provider name for the current execution target.
        execution_target_id: Target AWS account ID.
        execution_target_name: Friendly name for the target account.
        execution_target_type: Provider target type.
        region: Current AWS region.
        session: Boto3 session scoped to the current region.
        dry_run: Whether execution is running in dry-run mode.
        metadata: Arbitrary config metadata for the task.
        actions: Action recorder provided by the engine.

    Returns:
        A payload with the AWS region, total VPC count, and discovered VPC IDs.
    """
    account_id = execution_target_id
    account_alias = execution_target_name
    region_name = region
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
