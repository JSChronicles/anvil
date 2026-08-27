from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from anvil.actions import ActionRecorder
from anvil.providers.tasks._task_helpers import metadata_string_array
from anvil.task_errors import TaskExecutionError

__LOGGER__ = logging.getLogger(__name__)
TASK_SCOPE = "configured_target"

BOTO_CONFIG = Config(max_pool_connections=40)

# DeleteUser has no batch API, so removals happen one call per user.
MAX_DELETE_WORKERS = 3


def _get_active_sso_instance(sso_admin_client) -> tuple[str, str, str]:
    response = sso_admin_client.list_instances()
    instances = response.get("Instances", [])

    active_instance = next(
        (instance for instance in instances if instance.get("Status") == "ACTIVE"), None
    )

    if not active_instance:
        raise RuntimeError("No active IAM Identity Center (SSO) instance found")

    return (
        active_instance["InstanceArn"],
        active_instance["IdentityStoreId"],
        active_instance["OwnerAccountId"],
    )


def _list_users(
    identitystore_client, identity_store_id: str
) -> list[dict[str, object]]:
    users: list[dict[str, object]] = []

    paginator = identitystore_client.get_paginator("list_users")

    for page in paginator.paginate(IdentityStoreId=identity_store_id):
        for user in page.get("Users", []):
            user_id = user["UserId"]

            emails = [
                email.get("Value")
                for email in user.get("Emails", [])
                if email.get("Value")
            ]

            users.append(
                {
                    "UserId": user_id,
                    "UserName": user.get("UserName"),
                    "Emails": emails,
                    "UserType": user.get("UserType"),
                    "UserStatus": user.get("UserStatus"),
                }
            )

            __LOGGER__.debug(
                f"Identity Center user found: {user.get('UserName')} ({user_id})"
            )

    return users


def _resolve_status(metadata: dict[str, object]) -> str | None:
    """Normalize a boolean or Identity Store status selector."""

    raw = metadata.get("status")
    if raw is None:
        return None
    if isinstance(raw, bool):
        return "ENABLED" if raw else "DISABLED"
    if isinstance(raw, str) and raw.strip().upper() in {"ENABLED", "DISABLED"}:
        return raw.strip().upper()
    raise RuntimeError(
        "remove_idc_user expects metadata.status to be true, false, "
        "'ENABLED', or 'DISABLED'"
    )


def _user_selector_values(user: dict[str, object]) -> set[str]:
    """Return case-insensitive ID, username, and email selector values."""

    values = {user.get("UserId"), user.get("UserName")}
    emails = user.get("Emails", [])
    if isinstance(emails, list):
        values.update(emails)
    return {str(value).casefold() for value in values if value is not None}


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
    """Remove selected users from IAM Identity Center.

    This AWS task runs in the IAM Identity Center owner account. It enumerates
    every user in the Identity Store via ``list_users`` and filters by one or
    more user identifiers, status, or both. User selectors match ``UserId``,
    ``UserName``, or email address. Boolean status values map to Identity Store
    status: ``false`` selects ``DISABLED`` and ``true`` selects ``ENABLED``.
    Status-only selection supports bulk cleanup. Deletions run concurrently;
    failures are collected and raised together after all selected users have
    been attempted. Dry-run is supported.

    Metadata:
        identity_center_region: Optional AWS region for the IAM Identity
            Center and Identity Store clients. Defaults to the current
            session region.
        users: Optional non-empty array of Identity Store user IDs, usernames,
            or email addresses. One or more values are supported.
        status: Optional boolean or ``ENABLED``/``DISABLED`` string. ``false``
            selects disabled users and ``true`` selects enabled users. This
            selector can be used without ``users`` for bulk cleanup.

        At least one of ``users`` or ``status`` is required. When both are
        provided, a user must match both filters.

    Args:
        provider: Provider name for the current execution target.
        execution_target_id: Target AWS account ID.
        execution_target_name: Friendly name for the target account.
        execution_target_type: Provider target type.
        region: Current AWS region.
        session: Boto3 session scoped to the current region.
        dry_run: Whether execution is running in dry-run mode.
        metadata: Task metadata containing optional Identity Center region
            and user/status selectors.
        dependency_data: Runtime data selected from declared task dependencies.
            This task requires none.
        actions: Action recorder provided by the engine.

    Returns:
        A payload containing the Identity Center region, applied status,
        discovered and targeted counts, planned/removed/failed counts,
        unmatched selectors, failed user details, and targeted user details,
        or ``{"skipped": True}`` for non-owner accounts.

    Raises:
        ValueError: If metadata.identity_center_region is not a string.
        RuntimeError: If no active IAM Identity Center instance exists, no
            selector is supplied, or a selector has an invalid shape/value.
        TaskExecutionError: If one or more targeted users failed to delete.
            `partial_result` carries the same payload described above,
            including which users succeeded and which failed.
    """

    raw_region = metadata.get("identity_center_region")
    account_id = execution_target_id

    if raw_region is None:
        identity_center_region = region
    elif isinstance(raw_region, str):
        identity_center_region = raw_region
    else:
        raise ValueError("metadata.identity_center_region must be a string")

    user_selectors = metadata_string_array(
        task_name="remove_idc_user", metadata=metadata, key="users"
    )
    selected_status = _resolve_status(metadata)
    if user_selectors is None and selected_status is None:
        raise RuntimeError(
            "remove_idc_user requires metadata.users, metadata.status, or both"
        )

    __LOGGER__.info(f"Using Identity Center region '{identity_center_region}'")

    sso_admin_client = session.client(
        "sso-admin", region_name=identity_center_region, config=BOTO_CONFIG
    )
    identitystore_client = session.client(
        "identitystore", region_name=identity_center_region, config=BOTO_CONFIG
    )

    _, identity_store_id, owner_account_id = _get_active_sso_instance(sso_admin_client)

    if account_id != owner_account_id:
        __LOGGER__.info(
            f"Skipping account '{account_id}' because it is not "
            f"the Identity Center owner account"
        )
        return {"skipped": True}

    users = _list_users(identitystore_client, identity_store_id)
    normalized_selectors = (
        {selector.casefold() for selector in user_selectors}
        if user_selectors is not None
        else None
    )
    matched_selectors = (
        {
            selector
            for user in users
            for selector in normalized_selectors or set()
            if selector in _user_selector_values(user)
        }
        if normalized_selectors is not None
        else set()
    )
    targeted_users = [
        user
        for user in users
        if (
            normalized_selectors is None
            or bool(_user_selector_values(user).intersection(normalized_selectors))
        )
        and (selected_status is None or user.get("UserStatus") == selected_status)
    ]
    unmatched_users = (
        [
            selector
            for selector in user_selectors
            if selector.casefold() not in matched_selectors
        ]
        if user_selectors is not None
        else []
    )

    removed: list[dict[str, object]] = []
    failed: list[dict[str, object]] = []

    if dry_run:
        for user in targeted_users:
            message = (
                f"(dry-run) Would remove Identity Center user "
                f"'{user['UserName']}' ({user['UserId']})"
            )
            __LOGGER__.info(message)
            actions.record(message)
    else:
        with ThreadPoolExecutor(max_workers=MAX_DELETE_WORKERS) as executor:
            future_to_user = {
                executor.submit(
                    identitystore_client.delete_user,
                    IdentityStoreId=identity_store_id,
                    UserId=user["UserId"],
                ): user
                for user in targeted_users
            }

            for future in as_completed(future_to_user):
                user = future_to_user[future]
                user_id = user["UserId"]
                user_name = user["UserName"]

                try:
                    future.result()
                except (ClientError, BotoCoreError) as error:
                    __LOGGER__.warning(
                        f"Failed to remove disabled user '{user_name}' "
                        f"({user_id}): {error}"
                    )
                    failed.append({**user, "error": str(error)})
                    continue

                removed.append(user)
                message = f"Removed Identity Center user '{user_name}' ({user_id})"
                __LOGGER__.info(message)
                actions.record(message)

    result = {
        "identity_center_region": identity_center_region,
        "status": selected_status,
        "user_count": len(users),
        "targeted_count": len(targeted_users),
        "planned_count": len(targeted_users) if dry_run else 0,
        "removed_count": len(removed),
        "failed_count": len(failed),
        "unmatched_users": unmatched_users,
        "failed_users": failed,
        "targeted_users": targeted_users,
    }

    if failed:
        raise TaskExecutionError(
            f"remove_idc_user failed to remove {len(failed)} of "
            f"{len(targeted_users)} targeted user(s)",
            partial_result=result,
        )

    return result
