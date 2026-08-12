from __future__ import annotations

import logging
from typing import Any

from botocore.exceptions import ClientError

from anvil.actions import ActionRecorder

__LOGGER__ = logging.getLogger(__name__)


def _list_paginated_user_resources(
    iam_client, *, operation_name: str, result_key: str, user_name: str
) -> list[Any]:
    """Return every page of one IAM user resource collection."""

    try:
        paginator = iam_client.get_paginator(operation_name)
        resources: list[Any] = []
        for page in paginator.paginate(UserName=user_name):
            resources.extend(page.get(result_key, []))
        return resources
    except ClientError as error:
        if error.response["Error"]["Code"] == "NoSuchEntity":
            return []
        raise


def cleanup_user_resources(
    iam_client, user_name: str, dry_run: bool, actions: ActionRecorder
) -> None:
    # Groups
    groups = _list_paginated_user_resources(
        iam_client,
        operation_name="list_groups_for_user",
        result_key="Groups",
        user_name=user_name,
    )
    for group in groups:
        name = group["GroupName"]
        if dry_run:
            __LOGGER__.debug(f"(dry-run) Would remove user from group: {name}")
        else:
            iam_client.remove_user_from_group(GroupName=name, UserName=user_name)
            __LOGGER__.debug(f"Removed user from group: {name}")

    # Access Keys
    access_keys = _list_paginated_user_resources(
        iam_client,
        operation_name="list_access_keys",
        result_key="AccessKeyMetadata",
        user_name=user_name,
    )
    for key in access_keys:
        key_id = key["AccessKeyId"]
        if dry_run:
            __LOGGER__.debug(f"(dry-run) Would delete access key: {key_id}")
        else:
            iam_client.delete_access_key(UserName=user_name, AccessKeyId=key_id)
            __LOGGER__.debug(f"Deleted access key: {key_id}")

    # MFA Devices
    mfa_devices = _list_paginated_user_resources(
        iam_client,
        operation_name="list_mfa_devices",
        result_key="MFADevices",
        user_name=user_name,
    )
    for device in mfa_devices:
        serial = device["SerialNumber"]
        if dry_run:
            __LOGGER__.debug(f"(dry-run) Would deactivate MFA device: {serial}")
        else:
            iam_client.deactivate_mfa_device(UserName=user_name, SerialNumber=serial)
            if serial.startswith("arn:aws:iam"):
                iam_client.delete_virtual_mfa_device(SerialNumber=serial)
            __LOGGER__.debug(f"Deleted MFA device: {serial}")

    # SSH Keys
    ssh_keys = _list_paginated_user_resources(
        iam_client,
        operation_name="list_ssh_public_keys",
        result_key="SSHPublicKeys",
        user_name=user_name,
    )
    for ssh in ssh_keys:
        ssh_id = ssh["SSHPublicKeyId"]
        if dry_run:
            __LOGGER__.debug(f"(dry-run) Would delete SSH key: {ssh_id}")
        else:
            iam_client.delete_ssh_public_key(UserName=user_name, SSHPublicKeyId=ssh_id)
            __LOGGER__.debug(f"Deleted SSH key: {ssh_id}")

    # Service Credentials
    try:
        svc_creds = iam_client.list_service_specific_credentials(UserName=user_name)
    except ClientError as error:
        if error.response["Error"]["Code"] == "NoSuchEntity":
            svc_creds = {"ServiceSpecificCredentials": []}
        else:
            raise

    for cred in svc_creds.get("ServiceSpecificCredentials", []):
        cred_id = cred["ServiceSpecificCredentialId"]
        if dry_run:
            __LOGGER__.debug(f"(dry-run) Would delete service credential: {cred_id}")
        else:
            iam_client.delete_service_specific_credential(
                UserName=user_name, ServiceSpecificCredentialId=cred_id
            )
            __LOGGER__.debug(f"Deleted service credential: {cred_id}")

    # Certificates
    certificates = _list_paginated_user_resources(
        iam_client,
        operation_name="list_signing_certificates",
        result_key="Certificates",
        user_name=user_name,
    )
    for cert in certificates:
        cert_id = cert["CertificateId"]
        if dry_run:
            __LOGGER__.debug(f"(dry-run) Would delete certificate: {cert_id}")
        else:
            iam_client.delete_signing_certificate(
                UserName=user_name, CertificateId=cert_id
            )
            __LOGGER__.debug(f"Deleted certificate: {cert_id}")

    # Attached Policies
    attached_policies = _list_paginated_user_resources(
        iam_client,
        operation_name="list_attached_user_policies",
        result_key="AttachedPolicies",
        user_name=user_name,
    )
    for policy in attached_policies:
        arn = policy["PolicyArn"]
        if dry_run:
            __LOGGER__.debug(f"(dry-run) Would detach policy: {arn}")
        else:
            iam_client.detach_user_policy(UserName=user_name, PolicyArn=arn)
            __LOGGER__.debug(f"Detached policy: {arn}")

    # Inline Policies
    inline_policy_names = _list_paginated_user_resources(
        iam_client,
        operation_name="list_user_policies",
        result_key="PolicyNames",
        user_name=user_name,
    )
    for name in inline_policy_names:
        if dry_run:
            __LOGGER__.debug(f"(dry-run) Would delete inline policy: {name}")
        else:
            iam_client.delete_user_policy(UserName=user_name, PolicyName=name)
            __LOGGER__.debug(f"Deleted inline policy: {name}")

    # Tags
    tags = _list_paginated_user_resources(
        iam_client,
        operation_name="list_user_tags",
        result_key="Tags",
        user_name=user_name,
    )
    tag_keys = [tag["Key"] for tag in tags]
    if tag_keys:
        if dry_run:
            __LOGGER__.debug(f"(dry-run) Would remove tags: {tag_keys}")
        else:
            iam_client.untag_user(UserName=user_name, TagKeys=tag_keys)
            __LOGGER__.debug(f"Removed tags: {tag_keys}")

    # Login Profile
    try:
        iam_client.get_login_profile(UserName=user_name)
        if dry_run:
            __LOGGER__.debug("(dry-run) Would delete login profile")
        else:
            iam_client.delete_login_profile(UserName=user_name)
            __LOGGER__.debug("Deleted login profile")
    except ClientError as error:
        if error.response["Error"]["Code"] != "NoSuchEntity":
            __LOGGER__.error(f"Error deleting login profile for {user_name}: {error}")
            raise


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
) -> None:
    """Remove IAM resources attached to a configured IAM user.

    This AWS task deletes user-attached resources before user deletion workflows.
    It removes group memberships, access keys, MFA devices, SSH public keys,
    service-specific credentials, signing certificates, attached policies,
    inline policies, tags, and the console login profile. In dry-run mode it
    logs planned deletions without mutating IAM resources.

    Metadata:
        user_name: Required IAM user name whose attached resources should be
            removed.

    Args:
        provider: Provider name for the current execution target.
        execution_target_id: Target AWS account ID.
        execution_target_name: Friendly name for the target account.
        execution_target_type: Provider target type.
        region: Current AWS region.
        session: Boto3 session scoped to the current region.
        dry_run: Whether execution is running in dry-run mode.
        metadata: Task metadata containing the IAM user name.
        dependency_data: Runtime data selected from declared task dependencies.
        actions: Action recorder provided by the engine.

    Raises:
        RuntimeError: If metadata.user_name is missing or not a string.
        botocore.exceptions.ClientError: If an unexpected AWS API error occurs.
    """
    user_name = metadata.get("user_name")

    if not isinstance(user_name, str):
        raise RuntimeError("remove_iam_user requires metadata.user_name to be a string")

    if not dry_run:
        __LOGGER__.info(
            f"Cleaning IAM user '{user_name}' in account "
            f"{execution_target_name} ({execution_target_id})"
        )

    iam_client = session.client("iam")

    cleanup_user_resources(
        iam_client=iam_client, user_name=user_name, dry_run=dry_run, actions=actions
    )

    if dry_run:
        actions.record(f"(dry-run) Would remove IAM user resources for {user_name}")
    else:
        actions.record(f"Removed IAM user resources for {user_name}")
