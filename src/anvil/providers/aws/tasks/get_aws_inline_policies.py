from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import boto3
from botocore.client import BaseClient
from botocore.exceptions import ClientError

from anvil.actions import ActionRecorder
from anvil.task_errors import TaskExecutionError

__LOGGER__ = logging.getLogger(__name__)
TASK_SCOPE = "target"
MAX_IAM_WORKERS = 5


@dataclass(frozen=True, slots=True)
class _IamPolicyResource:
    """Describe one IAM inline-policy resource API family."""

    selector: str
    result_key: str
    list_operation: str
    list_result_key: str
    name_key: str
    list_policy_operation: str
    get_policy_operation: str


RESOURCE_SPECS = {
    spec.selector: spec
    for spec in (
        _IamPolicyResource(
            "user",
            "User",
            "list_users",
            "Users",
            "UserName",
            "list_user_policies",
            "get_user_policy",
        ),
        _IamPolicyResource(
            "role",
            "Role",
            "list_roles",
            "Roles",
            "RoleName",
            "list_role_policies",
            "get_role_policy",
        ),
        _IamPolicyResource(
            "group",
            "Group",
            "list_groups",
            "Groups",
            "GroupName",
            "list_group_policies",
            "get_group_policy",
        ),
    )
}


def _collect_resource_policies(
    iam_client: BaseClient, spec: _IamPolicyResource
) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
    """Collect one IAM resource kind while retaining per-entity failures."""

    names: list[str] = []
    paginator = iam_client.get_paginator(spec.list_operation)
    for page in paginator.paginate():
        for resource in page.get(spec.list_result_key, []):
            name = resource.get(spec.name_key)
            if isinstance(name, str):
                names.append(name)

    def collect_entity(
        entity_name: str,
    ) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
        policies: list[dict[str, object]] = []
        errors: list[dict[str, str]] = []
        request = {spec.name_key: entity_name}
        try:
            policy_paginator = iam_client.get_paginator(spec.list_policy_operation)
            policy_names = [
                policy_name
                for page in policy_paginator.paginate(**request)
                for policy_name in page.get("PolicyNames", [])
                if isinstance(policy_name, str)
            ]
            operation = getattr(iam_client, spec.get_policy_operation)
            for policy_name in policy_names:
                try:
                    response = operation(**request, PolicyName=policy_name)
                    policies.append(
                        {
                            "EntityType": spec.result_key,
                            "EntityName": entity_name,
                            "PolicyType": "Inline",
                            "PolicyName": policy_name,
                            "PolicyDocument": response["PolicyDocument"],
                        }
                    )
                except ClientError as error:
                    errors.append(
                        {
                            "entity_type": spec.result_key,
                            "entity_name": entity_name,
                            "policy_name": policy_name,
                            "error": str(error),
                        }
                    )
        except ClientError as error:
            errors.append(
                {
                    "entity_type": spec.result_key,
                    "entity_name": entity_name,
                    "error": str(error),
                }
            )
        return policies, errors

    policies: list[dict[str, object]] = []
    errors: list[dict[str, str]] = []
    with ThreadPoolExecutor(
        max_workers=min(MAX_IAM_WORKERS, max(1, len(names)))
    ) as executor:
        for entity_policies, entity_errors in executor.map(collect_entity, names):
            policies.extend(entity_policies)
            errors.extend(entity_errors)
    policies.sort(key=lambda item: (str(item["EntityName"]), str(item["PolicyName"])))
    errors.sort(
        key=lambda item: (
            item.get("entity_type", ""),
            item.get("entity_name", ""),
            item.get("policy_name", ""),
        )
    )
    return policies, errors


def _requested_types(metadata: dict[str, object]) -> list[str]:
    """Validate and normalize requested IAM policy resource types."""

    raw_types = metadata.get("types", list(RESOURCE_SPECS))
    if not isinstance(raw_types, list) or not raw_types:
        raise RuntimeError(
            "get_aws_inline_policies expects metadata.types to be a non-empty array"
        )
    requested: list[str] = []
    for value in raw_types:
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError(
                "get_aws_inline_policies expects metadata.types to contain strings"
            )
        normalized = value.strip().lower()
        if normalized not in RESOURCE_SPECS:
            raise RuntimeError(
                f"Unsupported IAM policy type {value!r}; expected one of "
                f"{sorted(RESOURCE_SPECS)}. Use get_aws_sso_inline_policies for "
                "Identity Center permission sets."
            )
        if normalized not in requested:
            requested.append(normalized)
    return requested


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
    """Gather IAM inline policies once for each resolved AWS account.

    Metadata:
        types: Optional non-empty array containing `user`, `role`, or `group`.
            Defaults to all three. Use `get_aws_sso_inline_policies` for IAM
            Identity Center permission-set policies.

    Args:
        provider: Provider name for the current execution target.
        execution_target_id: Target AWS account ID.
        execution_target_name: Friendly name for the target account.
        execution_target_type: Provider target type.
        region: First resolved AWS region; IAM itself is account-wide.
        session: Boto3 session scoped to the resolved AWS account.
        dry_run: Whether execution is running in dry-run mode.
        metadata: Task metadata containing optional policy type filters.
        dependency_data: Runtime dependency data; this task requires none.
        actions: Action recorder provided by the engine.

    Returns:
        Policies grouped by IAM resource type plus collection error details.

    Raises:
        RuntimeError: If metadata.types is invalid.
        TaskExecutionError: If collection is partial; partial_result retains
            all successfully collected policies.
    """

    iam_client: BaseClient = session.client("iam")
    policies: dict[str, list[dict[str, object]]] = {}
    errors: list[dict[str, str]] = []
    for resource_type in _requested_types(metadata):
        spec = RESOURCE_SPECS[resource_type]
        try:
            collected, collection_errors = _collect_resource_policies(iam_client, spec)
        except ClientError as error:
            collected = []
            collection_errors = [{"entity_type": spec.result_key, "error": str(error)}]
        policies[spec.result_key] = collected
        errors.extend(collection_errors)

    policy_count = sum(len(items) for items in policies.values())
    result: dict[str, object] = {
        "policies": policies,
        "policy_count": policy_count,
        "error_count": len(errors),
        "errors": errors,
    }
    actions.record(
        f"Collected {policy_count} IAM inline polic(ies) with {len(errors)} error(s)"
    )
    if errors:
        raise TaskExecutionError(
            f"get_aws_inline_policies encountered {len(errors)} collection error(s)",
            partial_result=result,
        )
    return result
