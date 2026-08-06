from __future__ import annotations

import json
import logging
from typing import Any

import boto3
from botocore.client import BaseClient
from botocore.exceptions import ClientError

from anvil.actions import ActionRecorder

__LOGGER__ = logging.getLogger(__name__)

SUPPORTED_POLICY_TYPES: set[str] = {"user", "role", "group", "sso"}


def _list_user_policies(iam_client: BaseClient) -> list[dict[str, Any]]:
    policies: list[dict[str, Any]] = []

    user_paginator = iam_client.get_paginator("list_users")

    for user_page in user_paginator.paginate():
        for user_entry in user_page.get("Users", []):
            username = user_entry["UserName"]

            policy_paginator = iam_client.get_paginator("list_user_policies")

            for policy_page in policy_paginator.paginate(UserName=username):
                for policy_name in policy_page.get("PolicyNames", []):
                    try:
                        policy_response = iam_client.get_user_policy(
                            UserName=username, PolicyName=policy_name
                        )

                        policies.append(
                            {
                                "EntityType": "User",
                                "EntityName": username,
                                "PolicyType": "Inline",
                                "PolicyName": policy_name,
                                "PolicyDocument": policy_response["PolicyDocument"],
                            }
                        )

                    except ClientError as error:
                        __LOGGER__.warning(
                            f"Failed to fetch inline policy '{policy_name}' "
                            f"for user '{username}': {error}"
                        )

    return policies


def _list_role_policies(iam_client: BaseClient) -> list[dict[str, Any]]:
    policies: list[dict[str, Any]] = []

    role_paginator = iam_client.get_paginator("list_roles")

    for role_page in role_paginator.paginate():
        for role_entry in role_page.get("Roles", []):
            role_name = role_entry["RoleName"]

            policy_paginator = iam_client.get_paginator("list_role_policies")

            for policy_page in policy_paginator.paginate(RoleName=role_name):
                for policy_name in policy_page.get("PolicyNames", []):
                    try:
                        policy_response = iam_client.get_role_policy(
                            RoleName=role_name, PolicyName=policy_name
                        )

                        policies.append(
                            {
                                "EntityType": "Role",
                                "EntityName": role_name,
                                "PolicyType": "Inline",
                                "PolicyName": policy_name,
                                "PolicyDocument": policy_response["PolicyDocument"],
                            }
                        )

                    except ClientError as error:
                        __LOGGER__.warning(
                            f"Failed to fetch inline policy '{policy_name}' "
                            f"for role '{role_name}': {error}"
                        )

    return policies


def _list_group_policies(iam_client: BaseClient) -> list[dict[str, Any]]:
    policies: list[dict[str, Any]] = []

    group_paginator = iam_client.get_paginator("list_groups")

    for group_page in group_paginator.paginate():
        for group_entry in group_page.get("Groups", []):
            group_name = group_entry["GroupName"]

            policy_paginator = iam_client.get_paginator("list_group_policies")

            for policy_page in policy_paginator.paginate(GroupName=group_name):
                for policy_name in policy_page.get("PolicyNames", []):
                    try:
                        policy_response = iam_client.get_group_policy(
                            GroupName=group_name, PolicyName=policy_name
                        )

                        policies.append(
                            {
                                "EntityType": "Group",
                                "EntityName": group_name,
                                "PolicyType": "Inline",
                                "PolicyName": policy_name,
                                "PolicyDocument": policy_response["PolicyDocument"],
                            }
                        )

                    except ClientError as error:
                        __LOGGER__.warning(
                            f"Failed to fetch inline policy '{policy_name}' "
                            f"for group '{group_name}': {error}"
                        )

    return policies


# SSO Inline Policy Collector (Management Account Only)
def _list_sso_policies(session: boto3.Session) -> list[dict[str, Any]]:
    policies: list[dict[str, Any]] = []

    sso_admin_client = session.client("sso-admin")

    try:
        instances = sso_admin_client.list_instances().get("Instances", [])
    except ClientError as error:
        __LOGGER__.warning(f"Unable to list SSO instances: {error}")
        return policies

    for instance in instances:
        instance_arn = instance["InstanceArn"]

        permission_set_paginator = sso_admin_client.get_paginator(
            "list_permission_sets"
        )

        for permission_set_page in permission_set_paginator.paginate(
            InstanceArn=instance_arn
        ):
            for permission_set_arn in permission_set_page.get("PermissionSets", []):
                try:
                    permission_set_description = (
                        sso_admin_client.describe_permission_set(
                            InstanceArn=instance_arn,
                            PermissionSetArn=permission_set_arn,
                        )["PermissionSet"]
                    )

                    permission_set_name = permission_set_description.get(
                        "Name", permission_set_arn
                    )

                except ClientError:
                    permission_set_name = permission_set_arn

                try:
                    inline_policy_json = (
                        sso_admin_client.get_inline_policy_for_permission_set(
                            InstanceArn=instance_arn,
                            PermissionSetArn=permission_set_arn,
                        ).get("InlinePolicy")
                    )

                    if inline_policy_json:
                        policies.append(
                            {
                                "EntityType": "SSOPermissionSet",
                                "EntityName": permission_set_name,
                                "PolicyType": "Inline",
                                "PermissionSetArn": permission_set_arn,
                                "PolicyDocument": json.loads(inline_policy_json),
                            }
                        )

                except ClientError:
                    continue

    return policies


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
    """Gather AWS inline policies for IAM identities and Identity Center.

    This is a read-only AWS task. By default it collects inline policies from
    IAM users, roles, groups, and IAM Identity Center permission sets. Identity
    Center permission set policies are collected only when the current account
    is the AWS Organizations management account.

    Metadata:
        types: Optional list of policy categories to collect. Supported values
            are `user`, `role`, `group`, and `sso`. Defaults to all categories.

    Args:
        provider: Provider name for the current execution target.
        execution_target_id: Target AWS account ID.
        execution_target_name: Friendly name for the target account.
        execution_target_type: Provider target type.
        region: Current AWS region.
        session: Boto3 session scoped to the current region.
        dry_run: Whether execution is running in dry-run mode.
        metadata: Task metadata containing optional policy type filters.
        dependency_data: Runtime data selected from declared task dependencies.
        actions: Action recorder provided by the engine.

    Returns:
        A payload with account context and collected policies grouped by
        policy category.

    Raises:
        ValueError: If metadata.types is not a list of supported strings.
    """
    account_id = execution_target_id
    account_alias = execution_target_name

    raw_types = metadata.get("types")

    if raw_types is None:
        requested_types: list[str] = ["user", "role", "group", "sso"]

    elif isinstance(raw_types, list):
        requested_types = []

        for entry in raw_types:
            if not isinstance(entry, str):
                raise ValueError("metadata.types must contain only strings")
            requested_types.append(entry)

    else:
        raise ValueError("metadata.types must be a list[str]")

    normalized_types = {policy_type.lower() for policy_type in requested_types}

    invalid_types = normalized_types - SUPPORTED_POLICY_TYPES
    if invalid_types:
        raise ValueError(f"Unsupported policy types requested: {sorted(invalid_types)}")

    iam_client = session.client("iam")

    result: dict[str, object] = {
        "account_id": account_id,
        "account_alias": account_alias,
        "policies": {},
    }

    __LOGGER__.info(
        f"Gathering inline policies in account '{account_alias}' ({account_id})"
    )

    if "user" in normalized_types:
        actions.record("Gathering user inline policies")
        result["policies"]["User"] = _list_user_policies(iam_client)

    if "role" in normalized_types:
        actions.record("Gathering role inline policies")
        result["policies"]["Role"] = _list_role_policies(iam_client)

    if "group" in normalized_types:
        actions.record("Gathering group inline policies")
        result["policies"]["Group"] = _list_group_policies(iam_client)

    if "sso" in normalized_types:
        try:
            org_client = session.client("organizations")
            organization = org_client.describe_organization()["Organization"]

            if account_id == organization["MasterAccountId"]:
                actions.record("Gathering SSO permission set inline policies")
                result["policies"]["SSOPermissionSet"] = _list_sso_policies(session)
            else:
                __LOGGER__.info(
                    f"Skipping SSO inline policies in non-management account "
                    f"'{account_id}'"
                )

        except ClientError as error:
            __LOGGER__.warning(
                f"Unable to determine management account for SSO policy collection: {error}"
            )

    policy_summary = {
        policy_category: len(policy_list)
        for policy_category, policy_list in result["policies"].items()
        if isinstance(policy_list, list)
    }

    actions.record(f"Policy summary: {policy_summary}")

    return result
