from __future__ import annotations

import logging
from typing import Any

from botocore.exceptions import ClientError

from anvil.actions import ActionRecorder
from anvil.providers.tasks._task_helpers import metadata_string_array
from anvil.task_errors import TaskExecutionError

__LOGGER__ = logging.getLogger(__name__)
TASK_SCOPE = "target"


def _list_paginated_role_resources(
    iam_client, *, operation_name: str, result_key: str, role_name: str
) -> list[Any]:
    """Return every page of one IAM role resource collection."""

    try:
        paginator = iam_client.get_paginator(operation_name)
        resources: list[Any] = []
        for page in paginator.paginate(RoleName=role_name):
            resources.extend(page.get(result_key, []))
        return resources
    except ClientError as error:
        if error.response["Error"]["Code"] == "NoSuchEntity":
            return []
        raise


def cleanup_role_resources(
    iam_client, role_name: str, dry_run: bool, actions: ActionRecorder
) -> int:
    """Remove or plan removal of resources attached to one IAM role.

    Returns:
        The number of attached resources discovered for removal.
    """

    resource_count = 0

    # Instance Profiles
    instance_profiles = _list_paginated_role_resources(
        iam_client,
        operation_name="list_instance_profiles_for_role",
        result_key="InstanceProfiles",
        role_name=role_name,
    )
    for profile in instance_profiles:
        resource_count += 1
        profile_name = profile["InstanceProfileName"]
        if dry_run:
            __LOGGER__.debug(
                f"(dry-run) Would remove role from instance profile: {profile_name}"
            )
        else:
            iam_client.remove_role_from_instance_profile(
                InstanceProfileName=profile_name, RoleName=role_name
            )
            __LOGGER__.debug(f"Removed role from instance profile: {profile_name}")

    # Attached Policies
    attached_policies = _list_paginated_role_resources(
        iam_client,
        operation_name="list_attached_role_policies",
        result_key="AttachedPolicies",
        role_name=role_name,
    )
    for policy in attached_policies:
        resource_count += 1
        arn = policy["PolicyArn"]
        if dry_run:
            __LOGGER__.debug(f"(dry-run) Would detach policy: {arn}")
        else:
            iam_client.detach_role_policy(RoleName=role_name, PolicyArn=arn)
            __LOGGER__.debug(f"Detached policy: {arn}")

    # Inline Policies
    inline_policy_names = _list_paginated_role_resources(
        iam_client,
        operation_name="list_role_policies",
        result_key="PolicyNames",
        role_name=role_name,
    )
    for name in inline_policy_names:
        resource_count += 1
        if dry_run:
            __LOGGER__.debug(f"(dry-run) Would delete inline policy: {name}")
        else:
            iam_client.delete_role_policy(RoleName=role_name, PolicyName=name)
            __LOGGER__.debug(f"Deleted inline policy: {name}")

    # Tags
    tags = _list_paginated_role_resources(
        iam_client,
        operation_name="list_role_tags",
        result_key="Tags",
        role_name=role_name,
    )
    tag_keys = [tag["Key"] for tag in tags]
    if tag_keys:
        resource_count += len(tag_keys)
        if dry_run:
            __LOGGER__.debug(f"(dry-run) Would remove tags: {tag_keys}")
        else:
            iam_client.untag_role(RoleName=role_name, TagKeys=tag_keys)
            __LOGGER__.debug(f"Removed tags: {tag_keys}")

    # Permissions Boundary
    try:
        role_detail = iam_client.get_role(RoleName=role_name)["Role"]
    except ClientError as error:
        if error.response["Error"]["Code"] == "NoSuchEntity":
            role_detail = {}
        else:
            raise

    if role_detail.get("PermissionsBoundary"):
        resource_count += 1
        if dry_run:
            __LOGGER__.debug("(dry-run) Would delete permissions boundary")
        else:
            iam_client.delete_role_permissions_boundary(RoleName=role_name)
            __LOGGER__.debug("Deleted permissions boundary")

    return resource_count


def _selected_roles(metadata: dict[str, object]) -> list[str]:
    """Return the required, normalized IAM role selectors."""

    return metadata_string_array(
        task_name="remove_iam_role", metadata=metadata, key="roles", required=True
    )


def _role_exists(iam_client, role_name: str) -> bool:
    """Return whether an IAM role currently exists."""

    try:
        iam_client.get_role(RoleName=role_name)
    except ClientError as error:
        if error.response["Error"]["Code"] == "NoSuchEntity":
            return False
        raise
    return True


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
    """Remove selected IAM roles after cleaning their attached resources.

    This target-scoped AWS task runs once per resolved account and removes
    instance profile associations, attached managed policies, inline
    policies, tags, and any permissions boundary, then deletes each IAM
    role. In dry-run mode it reports planned deletions without mutating IAM.

    Metadata:
        roles: Required non-empty array of IAM role names to remove.

    Args:
        provider: Provider name for the current execution target.
        execution_target_id: Target AWS account ID.
        execution_target_name: Friendly name for the target account.
        execution_target_type: Provider target type.
        region: Current AWS region.
        session: Boto3 session scoped to the current region.
        dry_run: Whether execution is running in dry-run mode.
        metadata: Task metadata containing IAM role selectors.
        dependency_data: Runtime data selected from declared task dependencies.
        actions: Action recorder provided by the engine.

    Returns:
        A payload containing planned, removed, skipped, and failed IAM roles
        plus discovered attached-resource counts.

    Raises:
        RuntimeError: If ``metadata.roles`` is missing or invalid.
        botocore.exceptions.ClientError: If an unexpected AWS API error occurs.
        TaskExecutionError: If one or more selected roles fail to be removed.
    """
    role_names = _selected_roles(metadata)
    iam_client = session.client("iam")
    planned: list[dict[str, object]] = []
    removed: list[dict[str, object]] = []
    skipped: list[dict[str, object]] = []
    failed: list[dict[str, object]] = []

    for role_name in role_names:
        try:
            if not _role_exists(iam_client, role_name):
                skipped.append({"role_name": role_name, "reason": "not_found"})
                __LOGGER__.info(f"IAM role '{role_name}' does not exist; skipping")
                continue

            resource_count = cleanup_role_resources(
                iam_client=iam_client,
                role_name=role_name,
                dry_run=dry_run,
                actions=actions,
            )
            role_result: dict[str, object] = {
                "role_name": role_name,
                "attached_resource_count": resource_count,
            }
            if dry_run:
                planned.append(role_result)
                __LOGGER__.info(f"(dry-run) Would remove IAM role '{role_name}'")
            else:
                iam_client.delete_role(RoleName=role_name)
                removed.append(role_result)
                __LOGGER__.info(f"Removed IAM role '{role_name}'")
        except ClientError as error:
            failed.append({"role_name": role_name, "error": str(error)})
            __LOGGER__.warning(f"Failed to remove IAM role '{role_name}': {error}")

    result: dict[str, object] = {
        "selected_count": len(role_names),
        "planned_count": len(planned),
        "removed_count": len(removed),
        "skipped_count": len(skipped),
        "failed_count": len(failed),
        "planned_roles": planned,
        "removed_roles": removed,
        "skipped_roles": skipped,
        "failed_roles": failed,
    }
    if dry_run:
        actions.record(f"(dry-run) Would remove {len(planned)} IAM role(s)")
    else:
        actions.record(f"Removed {len(removed)} IAM role(s)")

    if failed:
        raise TaskExecutionError(
            f"remove_iam_role failed to remove {len(failed)} of "
            f"{len(role_names)} selected role(s)",
            partial_result=result,
        )
    return result
