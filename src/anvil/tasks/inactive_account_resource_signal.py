from __future__ import annotations

import logging
from collections.abc import Callable

from botocore.exceptions import BotoCoreError, ClientError

from anvil.actions import ActionRecorder

__LOGGER__ = logging.getLogger(__name__)

NEUTRAL_SCORE = 50


def get_resource_signal(session) -> dict[str, object]:
    """Count common deployed resources in the current execution region."""
    warnings: list[str] = []
    resource_counts = {
        "ec2_instances": count_ec2_instances(session, warnings),
        "ebs_volumes": count_ebs_volumes(session, warnings),
        "rds_instances": count_rds_instances(session, warnings),
        "lambda_functions": count_lambda_functions(session, warnings),
        "ecs_clusters": count_ecs_clusters(session, warnings),
        "ecs_services": count_ecs_services(session, warnings),
        "eks_clusters": count_eks_clusters(session, warnings),
        "load_balancers": count_load_balancers(session, warnings),
    }
    resource_count = sum(resource_counts.values())

    return {
        "resource_count": resource_count,
        "resource_counts": resource_counts,
        "resource_score": score_resources(resource_count, warnings),
        "warnings": warnings,
    }


def count_ec2_instances(session, warnings: list[str]) -> int:
    """Count non-terminated EC2 instances."""

    def collect() -> int:
        ec2_client = session.client("ec2")
        paginator = ec2_client.get_paginator("describe_instances")
        instance_count = 0

        for page in paginator.paginate(
            Filters=[
                {
                    "Name": "instance-state-name",
                    "Values": ["pending", "running", "stopping", "stopped"],
                }
            ]
        ):
            for reservation in page.get("Reservations", []):
                instance_count += len(reservation.get("Instances", []))

        return instance_count

    return count_service("EC2 instances", collect, warnings)


def count_ebs_volumes(session, warnings: list[str]) -> int:
    """Count EBS volumes."""

    def collect() -> int:
        ec2_client = session.client("ec2")
        paginator = ec2_client.get_paginator("describe_volumes")
        return sum(len(page.get("Volumes", [])) for page in paginator.paginate())

    return count_service("EBS volumes", collect, warnings)


def count_rds_instances(session, warnings: list[str]) -> int:
    """Count RDS DB instances."""

    def collect() -> int:
        rds_client = session.client("rds")
        paginator = rds_client.get_paginator("describe_db_instances")
        return sum(len(page.get("DBInstances", [])) for page in paginator.paginate())

    return count_service("RDS instances", collect, warnings)


def count_lambda_functions(session, warnings: list[str]) -> int:
    """Count Lambda functions."""

    def collect() -> int:
        lambda_client = session.client("lambda")
        paginator = lambda_client.get_paginator("list_functions")
        return sum(len(page.get("Functions", [])) for page in paginator.paginate())

    return count_service("Lambda functions", collect, warnings)


def count_ecs_clusters(session, warnings: list[str]) -> int:
    """Count ECS clusters."""

    def collect() -> int:
        ecs_client = session.client("ecs")
        paginator = ecs_client.get_paginator("list_clusters")
        return sum(len(page.get("clusterArns", [])) for page in paginator.paginate())

    return count_service("ECS clusters", collect, warnings)


def count_ecs_services(session, warnings: list[str]) -> int:
    """Count ECS services across ECS clusters."""

    def collect() -> int:
        ecs_client = session.client("ecs")
        cluster_paginator = ecs_client.get_paginator("list_clusters")
        service_count = 0

        for cluster_page in cluster_paginator.paginate():
            for cluster_arn in cluster_page.get("clusterArns", []):
                service_paginator = ecs_client.get_paginator("list_services")
                for service_page in service_paginator.paginate(cluster=cluster_arn):
                    service_count += len(service_page.get("serviceArns", []))

        return service_count

    return count_service("ECS services", collect, warnings)


def count_eks_clusters(session, warnings: list[str]) -> int:
    """Count EKS clusters."""

    def collect() -> int:
        eks_client = session.client("eks")
        paginator = eks_client.get_paginator("list_clusters")
        return sum(len(page.get("clusters", [])) for page in paginator.paginate())

    return count_service("EKS clusters", collect, warnings)


def count_load_balancers(session, warnings: list[str]) -> int:
    """Count Elastic Load Balancing v2 load balancers."""

    def collect() -> int:
        elbv2_client = session.client("elbv2")
        paginator = elbv2_client.get_paginator("describe_load_balancers")
        return sum(len(page.get("LoadBalancers", [])) for page in paginator.paginate())

    return count_service("load balancers", collect, warnings)


def count_service(
    service_label: str, collect: Callable[[], int], warnings: list[str]
) -> int:
    """Run one resource counter and return 0 with a warning on AWS errors."""
    try:
        return collect()
    except (BotoCoreError, ClientError) as error:
        warning = f"Unable to count {service_label}: {error}"
        __LOGGER__.warning(warning)
        warnings.append(warning)
        return 0


def score_resources(resource_count: int, warnings: list[str]) -> int:
    """Score deployed resource count where higher means more likely inactive."""
    if warnings:
        return NEUTRAL_SCORE
    if resource_count == 0:
        return 100
    if resource_count <= 2:
        return 85
    if resource_count <= 5:
        return 70
    if resource_count <= 10:
        return 50
    if resource_count <= 25:
        return 30
    if resource_count <= 50:
        return 15
    return 0


def run(
    *,
    account_id: str,
    account_alias: str,
    session,
    dry_run: bool,
    metadata: dict[str, object],
    actions: ActionRecorder,
) -> dict[str, object]:
    """Collect regional resource-count signal for inactive account assessment."""
    region_name = session.region_name
    signal = get_resource_signal(session)
    warnings = signal.pop("warnings", [])

    actions.record(f"Collected inactive-account resource signal for {account_id}")
    __LOGGER__.info(
        f"Collected inactive-account resource signal for "
        f"{account_alias} ({account_id}), region={region_name}, dry_run={dry_run}"
    )

    result: dict[str, object] = {
        "record_type": "inactive_account_resource_signal",
        "account_id": account_id,
        "account_alias": account_alias,
        "region": region_name,
        "signals": signal,
    }
    if warnings:
        result["warnings"] = warnings

    return result
