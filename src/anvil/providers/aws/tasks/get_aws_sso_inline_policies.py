from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from anvil.actions import ActionRecorder
from anvil.task_errors import TaskExecutionError

TASK_SCOPE = "configured_target"
MAX_IDENTITY_CENTER_WORKERS = 5
BOTO_CONFIG = Config(max_pool_connections=20)


def _identity_center_region(metadata: dict[str, object], default: str) -> str:
    """Return the configured IAM Identity Center endpoint region."""

    value = metadata.get("identity_center_region", default)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(
            "get_aws_sso_inline_policies expects metadata.identity_center_region "
            "to be a non-empty string"
        )
    return value.strip()


def _permission_set_arns(sso_admin_client, instance_arn: str) -> list[str]:
    """Return every permission-set ARN for one Identity Center instance."""

    paginator = sso_admin_client.get_paginator("list_permission_sets")
    return [
        arn
        for page in paginator.paginate(InstanceArn=instance_arn)
        for arn in page.get("PermissionSets", [])
        if isinstance(arn, str)
    ]


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
    dependency_data: dict[str, object],
    actions: ActionRecorder,
) -> dict[str, object]:
    """Gather Identity Center permission-set inline policies once per target.

    Metadata:
        identity_center_region: Optional non-empty AWS region containing IAM
            Identity Center. Defaults to the configured target's first region.

    Args:
        provider: Provider name for the current execution target.
        execution_target_id: Target AWS account ID.
        execution_target_name: Friendly name for the target account.
        execution_target_type: Provider target type.
        region: First resolved AWS region.
        session: Boto3 session scoped to the configured AWS target.
        dry_run: Whether execution is running in dry-run mode.
        metadata: Task metadata containing the optional endpoint region.
        dependency_data: Runtime dependency data; this task requires none.
        actions: Action recorder provided by the engine.

    Returns:
        Permission-set inline policies plus collection error details.

    Raises:
        RuntimeError: If the endpoint region is invalid.
        TaskExecutionError: If collection is partial; partial_result retains
            all successfully collected policies.
    """

    endpoint_region = _identity_center_region(metadata, region)
    client = session.client(
        "sso-admin", region_name=endpoint_region, config=BOTO_CONFIG
    )
    instances = client.list_instances().get("Instances", [])
    work: list[tuple[str, str]] = []
    errors: list[dict[str, str]] = []
    for instance in instances:
        instance_arn = instance.get("InstanceArn")
        if isinstance(instance_arn, str):
            try:
                work.extend(
                    (instance_arn, permission_set_arn)
                    for permission_set_arn in _permission_set_arns(client, instance_arn)
                )
            except ClientError as error:
                errors.append({"instance_arn": instance_arn, "error": str(error)})

    def collect(
        item: tuple[str, str],
    ) -> tuple[dict[str, object] | None, dict[str, str] | None]:
        instance_arn, permission_set_arn = item
        try:
            description = client.describe_permission_set(
                InstanceArn=instance_arn, PermissionSetArn=permission_set_arn
            )["PermissionSet"]
            name = description.get("Name", permission_set_arn)
            inline_policy = client.get_inline_policy_for_permission_set(
                InstanceArn=instance_arn, PermissionSetArn=permission_set_arn
            ).get("InlinePolicy")
            if not inline_policy:
                return None, None
            if not isinstance(inline_policy, str):
                raise RuntimeError("AWS returned a non-string inline policy document")
            return (
                {
                    "EntityType": "SSOPermissionSet",
                    "EntityName": name,
                    "PolicyType": "Inline",
                    "PermissionSetArn": permission_set_arn,
                    "PolicyDocument": json.loads(inline_policy),
                },
                None,
            )
        except (ClientError, json.JSONDecodeError, RuntimeError) as error:
            return None, {"permission_set_arn": permission_set_arn, "error": str(error)}

    policies: list[dict[str, object]] = []
    with ThreadPoolExecutor(
        max_workers=min(MAX_IDENTITY_CENTER_WORKERS, max(1, len(work)))
    ) as executor:
        for policy, error in executor.map(collect, work):
            if policy is not None:
                policies.append(policy)
            if error is not None:
                errors.append(error)

    policies.sort(key=lambda item: str(item.get("PermissionSetArn", "")))
    errors.sort(
        key=lambda item: (
            item.get("instance_arn", ""),
            item.get("permission_set_arn", ""),
        )
    )
    result: dict[str, object] = {
        "identity_center_region": endpoint_region,
        "policies": policies,
        "policy_count": len(policies),
        "error_count": len(errors),
        "errors": errors,
    }
    actions.record(
        f"Collected {len(policies)} Identity Center inline polic(ies) with "
        f"{len(errors)} error(s)"
    )
    if errors:
        raise TaskExecutionError(
            f"get_aws_sso_inline_policies encountered {len(errors)} "
            "collection error(s)",
            partial_result=result,
        )
    return result
