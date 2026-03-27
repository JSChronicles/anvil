from __future__ import annotations

import logging

from botocore.exceptions import ClientError

from anvil.task_definition import ActionRecorder

__LOGGER__ = logging.getLogger(__name__)


def cleanup_user_resources(
    iam_client, user_name: str, dry_run: bool, actions: ActionRecorder
) -> None:
    # Groups
    try:
        groups_response = iam_client.list_groups_for_user(UserName=user_name)
    except ClientError as error:
        if error.response["Error"]["Code"] == "NoSuchEntity":
            groups_response = {"Groups": []}
        else:
            raise

    for group in groups_response.get("Groups", []):
        name = group["GroupName"]
        if dry_run:
            __LOGGER__.debug(f"(dry-run) Would remove user from group: {name}")
        else:
            iam_client.remove_user_from_group(GroupName=name, UserName=user_name)
            __LOGGER__.debug(f"Removed user from group: {name}")

    # Access Keys
    try:
        access_keys = iam_client.list_access_keys(UserName=user_name)
    except ClientError as error:
        if error.response["Error"]["Code"] == "NoSuchEntity":
            access_keys = {"AccessKeyMetadata": []}
        else:
            raise

    for key in access_keys.get("AccessKeyMetadata", []):
        key_id = key["AccessKeyId"]
        if dry_run:
            __LOGGER__.debug(f"(dry-run) Would delete access key: {key_id}")
        else:
            iam_client.delete_access_key(UserName=user_name, AccessKeyId=key_id)
            __LOGGER__.debug(f"Deleted access key: {key_id}")

    # MFA Devices
    try:
        mfa_list = iam_client.list_mfa_devices(UserName=user_name)
    except ClientError as error:
        if error.response["Error"]["Code"] == "NoSuchEntity":
            mfa_list = {"MFADevices": []}
        else:
            raise

    for device in mfa_list.get("MFADevices", []):
        serial = device["SerialNumber"]
        if dry_run:
            __LOGGER__.debug(f"(dry-run) Would deactivate MFA device: {serial}")
        else:
            iam_client.deactivate_mfa_device(UserName=user_name, SerialNumber=serial)
            if serial.startswith("arn:aws:iam"):
                iam_client.delete_virtual_mfa_device(SerialNumber=serial)
            __LOGGER__.debug(f"Deleted MFA device: {serial}")

    # SSH Keys
    try:
        ssh_list = iam_client.list_ssh_public_keys(UserName=user_name)
    except ClientError as error:
        if error.response["Error"]["Code"] == "NoSuchEntity":
            ssh_list = {"SSHPublicKeys": []}
        else:
            raise

    for ssh in ssh_list.get("SSHPublicKeys", []):
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
    try:
        certs = iam_client.list_signing_certificates(UserName=user_name)
    except ClientError as error:
        if error.response["Error"]["Code"] == "NoSuchEntity":
            certs = {"Certificates": []}
        else:
            raise

    for cert in certs.get("Certificates", []):
        cert_id = cert["CertificateId"]
        if dry_run:
            __LOGGER__.debug(f"(dry-run) Would delete certificate: {cert_id}")
        else:
            iam_client.delete_signing_certificate(
                UserName=user_name, CertificateId=cert_id
            )
            __LOGGER__.debug(f"Deleted certificate: {cert_id}")

    # Attached Policies
    try:
        attached = iam_client.list_attached_user_policies(UserName=user_name)
    except ClientError as error:
        if error.response["Error"]["Code"] == "NoSuchEntity":
            attached = {"AttachedPolicies": []}
        else:
            raise

    for policy in attached.get("AttachedPolicies", []):
        arn = policy["PolicyArn"]
        if dry_run:
            __LOGGER__.debug(f"(dry-run) Would detach policy: {arn}")
        else:
            iam_client.detach_user_policy(UserName=user_name, PolicyArn=arn)
            __LOGGER__.debug(f"Detached policy: {arn}")

    # Inline Policies
    try:
        inline = iam_client.list_user_policies(UserName=user_name)
    except ClientError as error:
        if error.response["Error"]["Code"] == "NoSuchEntity":
            inline = {"PolicyNames": []}
        else:
            raise

    for name in inline.get("PolicyNames", []):
        if dry_run:
            __LOGGER__.debug(f"(dry-run) Would delete inline policy: {name}")
        else:
            iam_client.delete_user_policy(UserName=user_name, PolicyName=name)
            __LOGGER__.debug(f"Deleted inline policy: {name}")

    # Tags
    try:
        tags = iam_client.list_user_tags(UserName=user_name)
    except ClientError as error:
        if error.response["Error"]["Code"] == "NoSuchEntity":
            tags = {"Tags": []}
        else:
            raise

    tag_keys = [t["Key"] for t in tags.get("Tags", [])]
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
    account_id: str,
    account_alias: str,
    session,
    dry_run: bool,
    metadata: dict[str, object],
    actions: ActionRecorder,
) -> None:
    user_name = metadata.get("user_name")

    if not isinstance(user_name, str):
        raise RuntimeError("remove_iam_user requires metadata.user_name to be a string")

    if not dry_run:
        __LOGGER__.info(
            f"Cleaning IAM user '{user_name}' in account {account_alias} ({account_id})"
        )

    iam_client = session.client("iam")

    cleanup_user_resources(
        iam_client=iam_client, user_name=user_name, dry_run=True, actions=actions
    )

    actions.record("Removed IAM user resources")
