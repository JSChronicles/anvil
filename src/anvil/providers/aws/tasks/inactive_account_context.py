"""Collect AWS account and ownership context for inactivity reporting."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING


from anvil.actions import ActionRecorder

if TYPE_CHECKING:
    from botocore.client import BaseClient

__LOGGER__ = logging.getLogger(__name__)
TASK_SCOPE = "configured_target"
OPERATIONS_CONTACT_TYPE = "OPERATIONS"

DEFAULT_OWNER_TAG_KEYS = (
    "Owner",
    "TechnicalOwner",
    "BusinessOwner",
    "Application",
    "App",
    "CostCenter",
    "Purpose",
)


def _owner_tag_keys(metadata: dict[str, object]) -> tuple[str, ...]:
    """Return validated ownership tag keys from task metadata."""
    configured = metadata.get("owner_tag_keys")
    if configured is None:
        return DEFAULT_OWNER_TAG_KEYS
    if not isinstance(configured, list) or not all(
        isinstance(value, str) and value.strip() for value in configured
    ):
        raise RuntimeError(
            "inactive_account_context metadata.owner_tag_keys must be a list of "
            "non-empty strings"
        )
    return tuple(dict.fromkeys(value.strip() for value in configured))


def _list_tags(org_client: BaseClient, account_id: str) -> dict[str, str]:
    """Return all Organizations tags attached to one account."""
    tags: dict[str, str] = {}
    paginator = org_client.get_paginator("list_tags_for_resource")
    for page in paginator.paginate(ResourceId=account_id):
        for tag in page.get("Tags", []):
            key = tag.get("Key")
            value = tag.get("Value")
            if isinstance(key, str) and isinstance(value, str):
                tags[key] = value
    return tags


def _operations_contact(
    account_client: BaseClient, account_id: str, management_account_id: str | None
) -> dict[str, str | None] | None:
    """Return one account's Operations alternate contact, or None if unset.

    The management account cannot name itself in ``AccountId``, so its own
    contact is read from the caller's context instead.

    Raises:
        BotoCoreError, ClientError: For any failure other than "no contact set".
    """
    from botocore.exceptions import ClientError

    request: dict[str, str] = {"AlternateContactType": OPERATIONS_CONTACT_TYPE}
    if account_id != management_account_id:
        request["AccountId"] = account_id

    try:
        response = account_client.get_alternate_contact(**request)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
            return None
        raise

    contact = response.get("AlternateContact")
    if not isinstance(contact, dict):
        return None

    def _text(key: str) -> str | None:
        value = contact.get(key)
        return value.strip() if isinstance(value, str) and value.strip() else None

    result = {
        "name": _text("Name"),
        "title": _text("Title"),
        "email": _text("EmailAddress"),
    }
    return result if any(result.values()) else None


def collect_accounts(
    org_client: BaseClient,
    owner_tag_keys: tuple[str, ...],
    account_client: BaseClient | None = None,
    management_account_id: str | None = None,
) -> list[dict[str, object]]:
    """Return active organization accounts with account-level ownership context.

    When ``account_client`` is provided, each account's Operations alternate
    contact is collected as ``operations_contact``; otherwise it is ``None``.
    """
    from botocore.exceptions import BotoCoreError, ClientError

    accounts: list[dict[str, object]] = []
    paginator = org_client.get_paginator("list_accounts")
    for page in paginator.paginate():
        for account in page.get("Accounts", []):
            account_id = account.get("Id")
            if not isinstance(account_id, str):
                raise RuntimeError(
                    "AWS Organizations returned an account without an Id"
                )

            state = account.get("State", account.get("Status"))
            if state not in {None, "ACTIVE"}:
                continue

            warnings: list[str] = []
            try:
                tags = _list_tags(org_client, account_id)
            except (BotoCoreError, ClientError) as error:
                warning = (
                    f"Unable to collect Organizations tags for account "
                    f"{account_id}: {error}"
                )
                __LOGGER__.warning(warning)
                warnings.append(warning)
                tags = {}
            ownership_tags = {
                key: tags[key]
                for key in owner_tag_keys
                if key in tags and tags[key].strip()
            }
            operations_contact = None
            if account_client is not None:
                try:
                    operations_contact = _operations_contact(
                        account_client, account_id, management_account_id
                    )
                except (BotoCoreError, ClientError) as error:
                    warning = (
                        f"Unable to collect Operations contact for account "
                        f"{account_id}: {error}"
                    )
                    __LOGGER__.warning(warning)
                    warnings.append(warning)
            accounts.append(
                {
                    "account_id": account_id,
                    "account_name": str(account.get("Name", account_id)),
                    "email": account.get("Email"),
                    "state": state,
                    "tags": tags,
                    "ownership_tags": ownership_tags,
                    "operations_contact": operations_contact,
                    "warnings": warnings,
                }
            )

    return sorted(accounts, key=lambda account: str(account["account_id"]))


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
    """Collect organization accounts and account-level ownership context.

    This read-only task runs once for the configured AWS target and uses its
    Organizations control plane. Account tags and each account's Operations
    alternate contact are returned as report context; they are not an
    inactivity scoring input. Reading member-account contacts needs
    ``account:GetAlternateContact`` and trusted access for AWS Account
    Management enabled in the organization.

    Args:
        provider: Provider name for the configured target.
        execution_target_id: Configured AWS account ID.
        execution_target_name: Friendly configured-target name.
        execution_target_type: Provider target type.
        region: Region used to construct the Organizations client.
        session: Boto3 session for the configured target.
        dry_run: Whether Anvil is running in dry-run mode.
        metadata: Optional ``owner_tag_keys`` list overriding recognized tag keys.
        dependency_data: Runtime dependency inputs; unused by this task.
        actions: Action recorder provided by the engine.

    Returns:
        A payload containing active organization accounts and their tags.

    Raises:
        RuntimeError: If ownership tag metadata or an AWS account record is invalid.
        botocore.exceptions.ClientError: If Organizations cannot be queried.
    """
    accounts = collect_accounts(
        session.client("organizations"),
        _owner_tag_keys(metadata),
        account_client=session.client("account"),
        management_account_id=execution_target_id,
    )
    actions.record(f"Collected ownership context for {len(accounts)} AWS account(s)")
    __LOGGER__.info(
        f"Collected inactive-account context from {execution_target_name} "
        f"({execution_target_id}) using region {region}"
    )
    return {"accounts": accounts}
