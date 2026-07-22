from __future__ import annotations

import logging

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from anvil.actions import ActionRecorder

__LOGGER__ = logging.getLogger(__name__)

BOTO_CONFIG = Config(max_pool_connections=40)


def _get_active_sso_instance(sso_admin_client) -> tuple[str, str, str]:
    response = sso_admin_client.list_instances()
    instances = response.get("Instances", [])

    active_instance = next(
        (instance for instance in instances if instance.get("Status") == "ACTIVE"), None
    )

    if not active_instance:
        raise ValueError("No active IAM Identity Center (SSO) instance found")

    return (
        active_instance["InstanceArn"],
        active_instance["IdentityStoreId"],
        active_instance["OwnerAccountId"],
    )


def _get_account_cache(org_client) -> dict[str, dict[str, str]]:
    account_cache: dict[str, dict[str, str]] = {}

    paginator = org_client.get_paginator("list_accounts")

    for page in paginator.paginate():
        for account in page.get("Accounts", []):
            account_cache[account["Id"]] = {
                "Id": account["Id"],
                "Name": account.get("Name", account["Id"]),
            }

    return account_cache


def _get_permission_set_name_cache(
    sso_admin_client, instance_arn: str
) -> dict[str, str]:
    permission_set_cache: dict[str, str] = {}

    paginator = sso_admin_client.get_paginator("list_permission_sets")

    for page in paginator.paginate(InstanceArn=instance_arn):
        for permission_set_arn in page.get("PermissionSets", []):
            try:
                description = sso_admin_client.describe_permission_set(
                    InstanceArn=instance_arn, PermissionSetArn=permission_set_arn
                )["PermissionSet"]

                permission_set_cache[permission_set_arn] = description.get(
                    "Name", permission_set_arn
                )

            except ClientError as error:
                __LOGGER__.warning(
                    f"Could not describe permission set '{permission_set_arn}': {error}"
                )
                permission_set_cache[permission_set_arn] = permission_set_arn

    return permission_set_cache


def _collect_group_assignments(
    sso_admin_client,
    instance_arn: str,
    account_cache: dict[str, dict[str, str]],
    permission_set_cache: dict[str, str],
) -> list[dict[str, str]]:
    assignments: list[dict[str, str]] = []

    for permission_set_arn, permission_set_name in permission_set_cache.items():
        paginator = sso_admin_client.get_paginator(
            "list_accounts_for_provisioned_permission_set"
        )

        for page in paginator.paginate(
            InstanceArn=instance_arn, PermissionSetArn=permission_set_arn
        ):
            for account_id in page.get("AccountIds", []):
                account_name = account_cache.get(account_id, {}).get("Name", account_id)

                assignment_paginator = sso_admin_client.get_paginator(
                    "list_account_assignments"
                )

                for assignment_page in assignment_paginator.paginate(
                    InstanceArn=instance_arn,
                    AccountId=account_id,
                    PermissionSetArn=permission_set_arn,
                ):
                    for assignment in assignment_page.get("AccountAssignments", []):
                        if assignment.get("PrincipalType") == "GROUP":
                            assignments.append(
                                {
                                    "PermissionSetArn": permission_set_arn,
                                    "PermissionSetName": permission_set_name,
                                    "AccountId": account_id,
                                    "AccountName": account_name,
                                    "GroupId": assignment["PrincipalId"],
                                }
                            )

    return assignments


def _validate_groups(
    identitystore_client, identity_store_id: str, group_ids: set[str]
) -> dict[str, bool]:
    group_existence: dict[str, bool] = {}

    for group_id in group_ids:
        try:
            identitystore_client.describe_group(
                IdentityStoreId=identity_store_id, GroupId=group_id
            )
            group_existence[group_id] = True

        except identitystore_client.exceptions.ResourceNotFoundException:
            group_existence[group_id] = False

        except ClientError as error:
            __LOGGER__.error(f"Error validating group '{group_id}': {error}")
            group_existence[group_id] = False

    return group_existence


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
) -> dict[str, object]:
    """Remove IAM Identity Center group assignments for missing groups.

    This AWS task runs in the IAM Identity Center owner account. It finds account
    assignments whose principal type is GROUP, verifies that the referenced
    Identity Store groups still exist, and removes assignments for groups that
    no longer exist. In dry-run mode it reports planned removals without
    deleting assignments.

    Metadata:
        identity_center_region: Optional AWS region for IAM Identity Center and
            Identity Store clients. Defaults to the current session region.

    Args:
        provider: Provider name for the current execution target.
        execution_target_id: Target AWS account ID.
        execution_target_name: Friendly name for the target account.
        execution_target_type: Provider target type.
        region: Current AWS region.
        session: Boto3 session scoped to the current region.
        dry_run: Whether execution is running in dry-run mode.
        metadata: Task metadata containing optional Identity Center region.
        actions: Action recorder provided by the engine.

    Returns:
        A payload containing Identity Center region, missing assignment count,
        removed assignment count, and missing assignment details, or
        `{"skipped": True}` for non-owner accounts.

    Raises:
        ValueError: If metadata.identity_center_region is not a string.
        RuntimeError: If no active IAM Identity Center instance exists.
    """

    raw_region = metadata.get("identity_center_region")
    account_id = execution_target_id

    if raw_region is None:
        identity_center_region = region

    elif isinstance(raw_region, str):
        identity_center_region = raw_region

    else:
        raise ValueError("metadata.identity_center_region must be a string")

    __LOGGER__.info(f"Using Identity Center region '{identity_center_region}'")

    sso_admin_client = session.client(
        "sso-admin", region_name=identity_center_region, config=BOTO_CONFIG
    )

    identitystore_client = session.client(
        "identitystore", region_name=identity_center_region, config=BOTO_CONFIG
    )

    org_client = session.client("organizations", config=BOTO_CONFIG)

    instances_response = sso_admin_client.list_instances()
    instances = instances_response.get("Instances", [])

    active_instance = next(
        (instance for instance in instances if instance.get("Status") == "ACTIVE"), None
    )

    if not active_instance:
        raise RuntimeError(
            f"No active Identity Center instance found in region "
            f"'{identity_center_region}'"
        )

    instance_arn = active_instance["InstanceArn"]
    identity_store_id = active_instance["IdentityStoreId"]
    owner_account_id = active_instance["OwnerAccountId"]

    if account_id != owner_account_id:
        __LOGGER__.info(
            f"Skipping account '{account_id}' because it is not "
            f"the Identity Center owner account"
        )
        return {"skipped": True}

    __LOGGER__.info(
        f"Scanning Identity Center instance '{instance_arn}' "
        f"in owner account '{account_id}'"
    )

    account_cache = _get_account_cache(org_client)

    permission_set_cache = _get_permission_set_name_cache(
        sso_admin_client, instance_arn
    )

    assignments = _collect_group_assignments(
        sso_admin_client, instance_arn, account_cache, permission_set_cache
    )

    unique_group_ids = {assignment["GroupId"] for assignment in assignments}

    group_existence = _validate_groups(
        identitystore_client, identity_store_id, unique_group_ids
    )

    missing_assignments = [
        assignment
        for assignment in assignments
        if not group_existence.get(assignment["GroupId"], False)
    ]

    for missing in missing_assignments:
        __LOGGER__.warning(
            f"Missing group '{missing['GroupId']}' "
            f"for permission set '{missing['PermissionSetName']}' "
            f"in account '{missing['AccountName']}'"
        )

    removed: list[dict[str, str]] = []

    for entry in missing_assignments:
        if dry_run:
            __LOGGER__.info(
                f"(Dry run) Would remove GROUP '{entry['GroupId']}' "
                f"from permission set '{entry['PermissionSetName']}' "
                f"in account '{entry['AccountName']}'"
            )
            continue

        try:
            sso_admin_client.delete_account_assignment(
                InstanceArn=instance_arn,
                TargetId=entry["AccountId"],
                TargetType="AWS_ACCOUNT",
                PermissionSetArn=entry["PermissionSetArn"],
                PrincipalType="GROUP",
                PrincipalId=entry["GroupId"],
            )

            removed.append(entry)

            __LOGGER__.info(
                f"Removed GROUP '{entry['GroupId']}' "
                f"from permission set '{entry['PermissionSetName']}' "
                f"in account '{entry['AccountName']}'"
            )

        except ClientError as error:
            __LOGGER__.error(
                f"Failed to remove GROUP '{entry['GroupId']}' "
                f"from permission set '{entry['PermissionSetName']}' "
                f"in account '{entry['AccountName']}': {error}"
            )

    actions.record(f"Missing group assignments detected: {len(missing_assignments)}")

    return {
        "identity_center_region": identity_center_region,
        "missing_count": len(missing_assignments),
        "removed_count": len(removed),
        "missing": missing_assignments,
    }
