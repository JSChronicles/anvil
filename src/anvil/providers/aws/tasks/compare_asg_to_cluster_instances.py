from __future__ import annotations

import logging

import boto3
from botocore.exceptions import ClientError

from anvil.actions import ActionRecorder

__LOGGER__ = logging.getLogger(__name__)


def _validate_clusters(value: object) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("metadata.clusters must be a list[str]")

    clusters: list[str] = []

    for entry in value:
        if not isinstance(entry, str):
            raise ValueError("metadata.clusters must contain only strings")
        clusters.append(entry)

    if not clusters:
        raise ValueError("metadata.clusters must not be empty")

    return clusters


def run(
    *,
    provider: str,
    execution_target_id: str,
    execution_target_name: str,
    execution_target_type: str,
    region: str,
    session: boto3.Session,
    dry_run: bool,
    metadata: dict[str, object],
    actions: ActionRecorder,
) -> None:
    """Compare ECS container instances to corresponding Auto Scaling Groups.

    For each configured ECS cluster, the task compares EC2 instance IDs
    registered with the cluster against instances in an Auto Scaling Group named
    `<cluster>-asg`. It logs instances that are present in ECS but missing from
    the Auto Scaling Group, and logs ECS instances with zero running tasks.

    Metadata:
        clusters: Required list of ECS cluster names to inspect.
        ecs_region: Optional AWS region for ECS and Auto Scaling clients.
            Defaults to the current session region.

    Args:
        provider: Provider name for the current execution target.
        execution_target_id: Target AWS account ID.
        execution_target_name: Friendly name for the target account.
        execution_target_type: Provider target type.
        region: Current AWS region.
        session: Boto3 session scoped to the current region.
        dry_run: Whether execution is running in dry-run mode.
        metadata: Task metadata containing cluster configuration.
        actions: Action recorder provided by the engine.

    Raises:
        ValueError: If required metadata is missing or invalid.
        botocore.exceptions.ClientError: If AWS API calls fail.
    """

    raw_clusters = metadata.get("clusters")
    if raw_clusters is None:
        raise ValueError("metadata.clusters is required")

    cluster_names = _validate_clusters(raw_clusters)

    raw_region = metadata.get("ecs_region")

    if raw_region is None:
        ecs_region = region
    elif isinstance(raw_region, str):
        ecs_region = raw_region
    else:
        raise ValueError("metadata.ecs_region must be a string")

    __LOGGER__.debug(f"Creating AWS clients in region '{ecs_region}'")

    autoscaling_client = session.client("autoscaling", region_name=ecs_region)
    ecs_client = session.client("ecs", region_name=ecs_region)

    for cluster in cluster_names:
        __LOGGER__.debug(
            f"Gathering autoscale group information for cluster '{cluster}'"
        )

        try:
            auto_scaling_groups = autoscaling_client.describe_auto_scaling_groups(
                AutoScalingGroupNames=[f"{cluster}-asg"]
            )
        except ClientError as error:
            __LOGGER__.exception(f"Error describing ASG '{cluster}-asg': {error}")
            raise

        asg_instance_ids = {
            instance["InstanceId"]
            for auto_scaling_group in auto_scaling_groups["AutoScalingGroups"]
            for instance in auto_scaling_group["Instances"]
        }

        list_container_instances = ecs_client.list_container_instances(cluster=cluster)

        if list_container_instances["containerInstanceArns"]:
            __LOGGER__.debug(
                f"Gathering container instance information for cluster '{cluster}'"
            )

            describe_container_instances = ecs_client.describe_container_instances(
                cluster=cluster,
                containerInstances=list_container_instances["containerInstanceArns"],
            )

            __LOGGER__.debug(f"Gathering ec2InstanceIds for cluster '{cluster}'")

            ecs_instance_ids = {
                container_instance["ec2InstanceId"]
                for container_instance in describe_container_instances[
                    "containerInstances"
                ]
            }

            __LOGGER__.debug(
                f"Initializing list for instances with 0 running tasks "
                f"in cluster '{cluster}'"
            )

            instances_with_zero_tasks: list[str] = []

            for container_instance in describe_container_instances[
                "containerInstances"
            ]:
                if container_instance["runningTasksCount"] == 0:
                    instances_with_zero_tasks.append(
                        container_instance["ec2InstanceId"]
                    )

            diff_instance_ids = ecs_instance_ids - asg_instance_ids

            if diff_instance_ids:
                __LOGGER__.info(
                    f"EC2 Instance IDs in ECS but not in ASG for cluster '{cluster}':"
                )
                for instance_id in diff_instance_ids:
                    __LOGGER__.info(f" - {instance_id}")

            else:
                __LOGGER__.info(
                    f"All ECS instances in cluster '{cluster}' "
                    f"are part of the ASG '{cluster}-asg'."
                )
                __LOGGER__.info(
                    f"ECS instances with 0 running tasks "
                    f"in cluster '{cluster}': {instances_with_zero_tasks}"
                )

        else:
            __LOGGER__.info(f"No container instances found in cluster '{cluster}'.")

    actions.record(f"Completed ASG vs ECS comparison for {len(cluster_names)} clusters")
