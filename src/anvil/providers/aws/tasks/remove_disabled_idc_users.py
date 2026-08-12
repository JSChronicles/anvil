from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from anvil.actions import ActionRecorder
from anvil.task_errors import TaskExecutionError

__LOGGER__ = logging.getLogger(__name__)

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


def _list_disabled_users(
    identitystore_client, identity_store_id: str
) -> list[dict[str, object]]:
    disabled_users: list[dict[str, object]] = []

    paginator = identitystore_client.get_paginator("list_users")

    for page in paginator.paginate(IdentityStoreId=identity_store_id):
        for user in page.get("Users", []):
            if user.get("UserStatus") != "DISABLED":
                continue

            user_id = user["UserId"]

            emails = [
                email.get("Value")
                for email in user.get("Emails", [])
                if email.get("Value")
            ]

            disabled_users.append(
                {
                    "UserId": user_id,
                    "UserName": user.get("UserName"),
                    "Emails": emails,
                    "UserType": user.get("UserType"),
                    "UserStatus": user.get("UserStatus"),
                }
            )

            __LOGGER__.debug(f"Disabled user found: {user.get('UserName')} ({user_id})")

    return disabled_users


def _resolve_user_id_filters(
    metadata: dict[str, object],
) -> tuple[set[str] | None, set[str] | None]:
    include_raw = metadata.get("include_user_ids")
    exclude_raw = metadata.get("exclude_user_ids")

    if include_raw is not None and exclude_raw is not None:
        raise RuntimeError(
            "remove_disabled_idc_users requires only one of "
            "metadata.include_user_ids or metadata.exclude_user_ids to be set"
        )

    def _as_id_set(raw: object, key: str) -> set[str] | None:
        if raw is None:
            return None

        if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
            raise RuntimeError(
                f"remove_disabled_idc_users requires metadata.{key} to be a "
                "list of UserId strings"
            )

        return set(raw)

    return (
        _as_id_set(include_raw, "include_user_ids"),
        _as_id_set(exclude_raw, "exclude_user_ids"),
    )


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
    """Remove disabled users from IAM Identity Center.

    This AWS task runs in the IAM Identity Center owner account. It enumerates
    every user in the Identity Store via `list_users`, filters to users whose
    `UserStatus` is `DISABLED`, optionally filter further with
    `include_user_ids` or `exclude_user_ids`, and deletes every user that
    remains. Deletions run concurrently. A failure deleting one user does not stop the others;
    failures are collected and raised together at the end via `TaskExecutionError`.
    dry-run supported.

    Metadata:
        identity_center_region: Optional AWS region for the IAM Identity
            Center and Identity Store clients. Defaults to the current
            session region.
        include_user_ids: Optional list of Identity Store `UserId` strings.
            When set, only disabled users in this list are actioned; all
            other disabled users are left alone. Mutually exclusive with
            `exclude_user_ids`.
        exclude_user_ids: Optional list of Identity Store `UserId` strings.
            When set, disabled users in this list are left alone and every
            other disabled user is actioned. Mutually exclusive with
            `include_user_ids`.

    Args:
        provider: Provider name for the current execution target.
        execution_target_id: Target AWS account ID.
        execution_target_name: Friendly name for the target account.
        execution_target_type: Provider target type.
        region: Current AWS region.
        session: Boto3 session scoped to the current region.
        dry_run: Whether execution is running in dry-run mode.
        metadata: Task metadata containing optional Identity Center region
            and optional include/exclude UserId lists.
        dependency_data: Runtime data selected from declared task dependencies.
            This task requires none.
        actions: Action recorder provided by the engine.

    Returns:
        A payload containing the Identity Center region, disabled user
        count, targeted user count (after include/exclude filtering),
        removed count, failed count, failed user details (with an `error`
        message per entry), and disabled user details (UserId, UserName,
        Emails, UserType, UserStatus), or `{"skipped": True}` for non-owner
        accounts.

    Raises:
        ValueError: If metadata.identity_center_region is not a string.
        RuntimeError: If no active IAM Identity Center instance exists, if
            both metadata.include_user_ids and metadata.exclude_user_ids are
            set, or if either is set to something other than a list of
            strings.
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

    include_ids, exclude_ids = _resolve_user_id_filters(metadata)

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

    disabled_users = _list_disabled_users(identitystore_client, identity_store_id)

    if include_ids is not None:
        targeted_users = [
            user for user in disabled_users if user["UserId"] in include_ids
        ]
    elif exclude_ids is not None:
        targeted_users = [
            user for user in disabled_users if user["UserId"] not in exclude_ids
        ]
    else:
        targeted_users = disabled_users

    removed: list[dict[str, object]] = []
    failed: list[dict[str, object]] = []

    if dry_run:
        for user in targeted_users:
            __LOGGER__.info(
                f"(dry-run) Would remove disabled user '{user['UserName']}' "
                f"({user['UserId']})"
            )
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
                __LOGGER__.info(f"Removed disabled user '{user_name}' ({user_id})")

    if dry_run:
        actions.record(
            f"(dry-run) Would remove {len(targeted_users)} disabled "
            "Identity Center user(s)"
        )
    else:
        actions.record(
            f"Removed {len(removed)} disabled Identity Center user(s), "
            f"{len(failed)} failed"
        )

    result = {
        "identity_center_region": identity_center_region,
        "disabled_count": len(disabled_users),
        "targeted_count": len(targeted_users),
        "removed_count": len(removed),
        "failed_count": len(failed),
        "failed_users": failed,
        "disabled_users": disabled_users,
    }

    if failed:
        raise TaskExecutionError(
            f"remove_disabled_idc_users failed to remove {len(failed)} of "
            f"{len(targeted_users)} targeted user(s)",
            partial_result=result,
        )

    return result
