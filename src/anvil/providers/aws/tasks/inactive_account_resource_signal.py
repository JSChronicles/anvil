"""Collect regional resource and resource-ownership inactivity signals."""

from __future__ import annotations

import logging
from collections.abc import Callable

from botocore.client import BaseClient
from botocore.exceptions import BotoCoreError, ClientError

from anvil.actions import ActionRecorder
from anvil.providers.aws.tasks.inactive_account_context import _owner_tag_keys

__LOGGER__ = logging.getLogger(__name__)
NEUTRAL_SCORE = 50
RESOURCE_TYPE_FILTERS = (
    "ec2:instance",
    "ec2:volume",
    "rds:db",
    "lambda:function",
    "ecs:cluster",
    "ecs:service",
    "eks:cluster",
    "elasticloadbalancing:loadbalancer",
)


def _count(label: str, collect: Callable[[], int], warnings: list[str]) -> int:
    """Run one resource counter and return zero with a visible warning on failure."""
    try:
        return collect()
    except (BotoCoreError, ClientError) as error:
        warning = f"Unable to count {label}: {error}"
        __LOGGER__.warning(warning)
        warnings.append(warning)
        return 0


def _sum_pages(client: BaseClient, operation: str, result_key: str) -> int:
    """Count records from a standard AWS paginator result list."""
    paginator = client.get_paginator(operation)
    return sum(len(page.get(result_key, [])) for page in paginator.paginate())


def _count_instances(session) -> int:
    """Count non-terminated EC2 instances."""
    paginator = session.client("ec2").get_paginator("describe_instances")
    return sum(
        len(reservation.get("Instances", []))
        for page in paginator.paginate(
            Filters=[
                {
                    "Name": "instance-state-name",
                    "Values": ["pending", "running", "stopping", "stopped"],
                }
            ]
        )
        for reservation in page.get("Reservations", [])
    )


def _count_ecs_services(session) -> int:
    """Count services across all ECS clusters."""
    client = session.client("ecs")
    clusters = client.get_paginator("list_clusters")
    service_count = 0
    for cluster_page in clusters.paginate():
        for cluster_arn in cluster_page.get("clusterArns", []):
            services = client.get_paginator("list_services")
            service_count += sum(
                len(page.get("serviceArns", []))
                for page in services.paginate(cluster=cluster_arn)
            )
    return service_count


def _resource_counts(session, warnings: list[str]) -> dict[str, int]:
    """Count the resource types used by the inactivity model."""
    return {
        "ec2_instances": _count(
            "EC2 instances", lambda: _count_instances(session), warnings
        ),
        "ebs_volumes": _count(
            "EBS volumes",
            lambda: _sum_pages(session.client("ec2"), "describe_volumes", "Volumes"),
            warnings,
        ),
        "rds_instances": _count(
            "RDS instances",
            lambda: _sum_pages(
                session.client("rds"), "describe_db_instances", "DBInstances"
            ),
            warnings,
        ),
        "lambda_functions": _count(
            "Lambda functions",
            lambda: _sum_pages(session.client("lambda"), "list_functions", "Functions"),
            warnings,
        ),
        "ecs_clusters": _count(
            "ECS clusters",
            lambda: _sum_pages(session.client("ecs"), "list_clusters", "clusterArns"),
            warnings,
        ),
        "ecs_services": _count(
            "ECS services", lambda: _count_ecs_services(session), warnings
        ),
        "eks_clusters": _count(
            "EKS clusters",
            lambda: _sum_pages(session.client("eks"), "list_clusters", "clusters"),
            warnings,
        ),
        "load_balancers": _count(
            "load balancers",
            lambda: _sum_pages(
                session.client("elbv2"), "describe_load_balancers", "LoadBalancers"
            ),
            warnings,
        ),
    }


def _resource_type(resource_arn: str) -> str:
    """Return a compact service/resource type label from an ARN."""
    parts = resource_arn.split(":", maxsplit=5)
    if len(parts) < 6:
        return "unknown"
    service = parts[2]
    resource = parts[5]
    separator = "/" if "/" in resource else ":"
    return f"{service}:{resource.split(separator, maxsplit=1)[0]}"


def collect_resource_ownership(
    tagging_client: BaseClient, owner_tag_keys: tuple[str, ...]
) -> dict[str, object]:
    """Collect recognized ownership tags for supported regional resources."""
    owner_counts: dict[str, int] = {}
    owned_resources: list[dict[str, object]] = []
    tagged_resource_count = 0
    paginator = tagging_client.get_paginator("get_resources")
    for page in paginator.paginate(ResourceTypeFilters=list(RESOURCE_TYPE_FILTERS)):
        for mapping in page.get("ResourceTagMappingList", []):
            resource_arn = mapping.get("ResourceARN")
            if not isinstance(resource_arn, str):
                continue
            tagged_resource_count += 1
            tags = {
                tag["Key"]: tag["Value"]
                for tag in mapping.get("Tags", [])
                if isinstance(tag.get("Key"), str) and isinstance(tag.get("Value"), str)
            }
            ownership_tags = {
                key: tags[key]
                for key in owner_tag_keys
                if key in tags and tags[key].strip()
            }
            if not ownership_tags:
                continue
            primary_owner = next(iter(ownership_tags.values()))
            owner_counts[primary_owner] = owner_counts.get(primary_owner, 0) + 1
            owned_resources.append(
                {
                    "resource_arn": resource_arn,
                    "resource_type": _resource_type(resource_arn),
                    "ownership_tags": ownership_tags,
                }
            )
    return {
        "owner_counts": dict(sorted(owner_counts.items())),
        "tagged_resource_count": tagged_resource_count,
        "recognized_owner_resource_count": len(owned_resources),
        "owned_resources": sorted(
            owned_resources, key=lambda resource: str(resource["resource_arn"])
        ),
    }


def score_resources(resource_count: int, warnings: list[str]) -> int:
    """Score fewer deployed resources as stronger evidence of inactivity."""
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
    """Collect regional resource counts and resource-level ownership context.

    This read-only task runs once per AWS account and region. Ownership tags are
    returned to help route account-review decisions, but do not affect the
    resource or final inactivity score.

    Args:
        provider: Provider name for the current execution target.
        execution_target_id: AWS account ID being scanned.
        execution_target_name: Friendly account name.
        execution_target_type: Provider target type.
        region: AWS region being scanned.
        session: Boto3 session scoped to the account and region.
        dry_run: Whether Anvil is running in dry-run mode.
        metadata: Optional ``owner_tag_keys`` list overriding recognized keys.
        dependency_data: Runtime dependency inputs; unused by this task.
        actions: Action recorder provided by the engine.

    Returns:
        Regional counts, resource score, ownership context, and warnings.

    Raises:
        RuntimeError: If ``metadata.owner_tag_keys`` is invalid.
    """
    count_warnings: list[str] = []
    counts = _resource_counts(session, count_warnings)
    total = sum(counts.values())
    resource_score = score_resources(total, count_warnings)
    warnings = list(count_warnings)
    try:
        ownership = collect_resource_ownership(
            session.client("resourcegroupstaggingapi"), _owner_tag_keys(metadata)
        )
    except (BotoCoreError, ClientError) as error:
        warning = f"Unable to collect resource ownership tags: {error}"
        __LOGGER__.warning(warning)
        warnings.append(warning)
        ownership = {
            "owner_counts": {},
            "tagged_resource_count": 0,
            "recognized_owner_resource_count": 0,
            "owned_resources": [],
        }
    ownership["resources_without_recognized_owner_count"] = max(
        0,
        total
        - (
            ownership["recognized_owner_resource_count"]
            if isinstance(ownership["recognized_owner_resource_count"], int)
            else 0
        ),
    )
    actions.record(
        f"Collected {total} resource(s) for account {execution_target_id} in {region}"
    )
    return {
        "account_id": execution_target_id,
        "account_name": execution_target_name,
        "region": region,
        "resource_count": total,
        "resource_counts": counts,
        "resource_score": resource_score,
        "resource_count_complete": not count_warnings,
        "resource_ownership": ownership,
        "warnings": warnings,
    }
