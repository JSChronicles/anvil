"""
Count subnets in the current execution region with timing instrumentation.
"""

from __future__ import annotations

import logging
import time

from anvil.actions import ActionRecorder

__LOGGER__ = logging.getLogger(__name__)


def _elapsed_seconds(start: float) -> float:
    return round(time.perf_counter() - start, 3)


def _page_retry_attempts(page: dict[str, object]) -> int:
    response_metadata = page.get("ResponseMetadata", {})
    if not isinstance(response_metadata, dict):
        return 0

    retry_attempts = 0
    for key, value in response_metadata.items():
        if key == "RetryAttempts" and isinstance(value, int):
            retry_attempts = value
            break

    return retry_attempts


def _get_subnet_details(ec2_client) -> tuple[list[dict[str, object]], int]:
    subnets: list[dict[str, object]] = []
    retry_attempts = 0
    paginator = ec2_client.get_paginator("describe_subnets")

    for page in paginator.paginate():
        retry_attempts += _page_retry_attempts(page)

        for subnet in page.get("Subnets", []):
            subnet_id = subnet.get("SubnetId")
            vpc_id = subnet.get("VpcId")
            if not subnet_id or not vpc_id:
                continue

            subnets.append(
                {
                    "subnet_id": subnet_id,
                    "vpc_id": vpc_id,
                    "availability_zone": subnet.get("AvailabilityZone", ""),
                    "cidr_block": subnet.get("CidrBlock", ""),
                    "available_ip_address_count": subnet.get(
                        "AvailableIpAddressCount", 0
                    ),
                    "map_public_ip_on_launch": subnet.get("MapPublicIpOnLaunch", False),
                }
            )

    return subnets, retry_attempts


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
    """Count subnets in the session's current AWS region with timing data.

    This is a read-only AWS task. It uses EC2 `describe_subnets` pagination,
    records API retry counts from response metadata, and ignores task metadata.

    Args:
        provider: Provider name for the current execution target.
        execution_target_id: Target AWS account ID.
        execution_target_name: Friendly name for the target account.
        execution_target_type: Provider target type.
        region: Current AWS region.
        session: Boto3 session scoped to the current region.
        dry_run: Whether execution is running in dry-run mode.
        metadata: Arbitrary config metadata for the task.
        dependency_data: Runtime data selected from declared task dependencies.
        actions: Action recorder provided by the engine.

    Returns:
        A payload containing subnet summaries, aggregate counts, and timing data.
    """
    run_start = time.perf_counter()
    account_id = execution_target_id
    account_alias = execution_target_name
    region_name = region

    client_start = time.perf_counter()
    ec2_client = session.client("ec2")
    client_seconds = _elapsed_seconds(client_start)

    describe_subnets_start = time.perf_counter()
    subnets, describe_subnets_retries = _get_subnet_details(ec2_client)
    describe_subnets_seconds = _elapsed_seconds(describe_subnets_start)

    vpc_ids = sorted(
        {subnet["vpc_id"] for subnet in subnets if isinstance(subnet["vpc_id"], str)}
    )
    total_run_seconds = _elapsed_seconds(run_start)

    timing = {
        "client_seconds": client_seconds,
        "describe_subnets_seconds": describe_subnets_seconds,
        "total_run_seconds": total_run_seconds,
        "describe_subnets_retries": describe_subnets_retries,
    }

    __LOGGER__.info(
        f"Counted {len(subnets)} subnet(s) across {len(vpc_ids)} VPC(s) "
        f"in account {account_alias} ({account_id}), "
        f"region={region_name}, dry_run={dry_run}"
    )
    __LOGGER__.info(
        f"Timing account={account_id} region={region_name}: "
        f"client={client_seconds}s "
        f"describe_subnets={describe_subnets_seconds}s "
        f"retries={describe_subnets_retries} "
        f"total={total_run_seconds}s"
    )

    actions.record(
        f"Counted {len(subnets)} subnet(s) in account {account_id} region {region_name}"
    )

    return {
        "summary": {"total_subnets": len(subnets), "total_vpcs": len(vpc_ids)},
        "subnets": subnets,
        "timing": timing,
    }
