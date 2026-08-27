from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import boto3
from botocore.client import BaseClient
from botocore.exceptions import ClientError

from anvil.actions import ActionRecorder

__LOGGER__ = logging.getLogger(__name__)
TASK_SCOPE = "configured_target"
MAX_MANAGEMENT_WORKERS = 5


def _list_organizational_units(
    parent_id: str, org_client: BaseClient
) -> list[dict[str, object]]:
    organizational_units: list[dict[str, object]] = []

    paginator = org_client.get_paginator("list_organizational_units_for_parent")

    for page in paginator.paginate(ParentId=parent_id):
        for organizational_unit in page.get("OrganizationalUnits", []):
            organizational_units.append(
                {
                    "Name": organizational_unit["Name"],
                    "Id": organizational_unit["Id"],
                    "ParentId": parent_id,
                }
            )

    return organizational_units


def _list_policies_for_target(
    target_id: str, org_client: BaseClient
) -> list[dict[str, object]]:
    policies: list[dict[str, object]] = []

    paginator = org_client.get_paginator("list_policies_for_target")

    for page in paginator.paginate(TargetId=target_id, Filter="SERVICE_CONTROL_POLICY"):
        policies.extend(page.get("Policies", []))

    return policies


def _list_all_organizational_units(
    root_id: str, org_client: BaseClient
) -> list[dict[str, object]]:
    """Return every OU below the root with parent relationships preserved."""

    organizational_units: list[dict[str, object]] = []
    pending_parent_ids = [root_id]
    while pending_parent_ids:
        parent_id = pending_parent_ids.pop()
        children = _list_organizational_units(parent_id, org_client)
        organizational_units.extend(children)
        pending_parent_ids.extend(
            child_id
            for child in children
            if isinstance((child_id := child.get("Id")), str)
        )
    return organizational_units


def _list_accounts_for_parents(
    org_client: BaseClient, parent_ids: list[str]
) -> list[dict[str, object]]:
    """Return accounts directly contained by the supplied roots and OUs."""

    accounts: list[dict[str, object]] = []

    def list_for_parent(parent_id: str) -> list[dict[str, object]]:
        parent_accounts: list[dict[str, object]] = []
        paginator = org_client.get_paginator("list_accounts_for_parent")
        for page in paginator.paginate(ParentId=parent_id):
            for account in page.get("Accounts", []):
                account_copy = dict(account)
                account_copy["ParentId"] = parent_id
                joined_timestamp = account_copy.get("JoinedTimestamp")
                if joined_timestamp is not None:
                    account_copy["JoinedTimestamp"] = joined_timestamp.isoformat()
                parent_accounts.append(account_copy)
        return parent_accounts

    with ThreadPoolExecutor(
        max_workers=min(MAX_MANAGEMENT_WORKERS, max(1, len(parent_ids)))
    ) as executor:
        for parent_accounts in executor.map(list_for_parent, parent_ids):
            accounts.extend(parent_accounts)

    return accounts


def _enrich_organizational_unit(
    organizational_unit: dict[str, object],
    *,
    org_client: BaseClient,
    control_tower_client: BaseClient,
) -> dict[str, object]:
    """Add policies, ARN, and enabled controls to one OU record."""

    enriched = dict(organizational_unit)
    ou_id = enriched.get("Id")
    if not isinstance(ou_id, str):
        raise TypeError("OrganizationalUnit Id must be a string")
    enriched["Policies"] = _list_policies_for_target(ou_id, org_client)
    description = org_client.describe_organizational_unit(OrganizationalUnitId=ou_id)[
        "OrganizationalUnit"
    ]
    ou_arn = description["Arn"]
    if not isinstance(ou_arn, str):
        raise TypeError("OrganizationalUnit Arn must be a string")
    enriched["Arn"] = ou_arn
    enriched["EnabledControls"] = _list_enabled_controls_for_ou(
        control_tower_client, ou_arn
    )
    return enriched


def _enrich_account(
    account: dict[str, object], *, org_client: BaseClient
) -> dict[str, object]:
    """Add attached service-control policies to one account record."""

    enriched = dict(account)
    account_id = enriched.get("Id")
    if not isinstance(account_id, str):
        raise TypeError("Account Id must be a string")
    enriched["Policies"] = _list_policies_for_target(account_id, org_client)
    return enriched


def _list_enabled_controls_for_ou(
    control_tower_client: BaseClient, ou_arn: str
) -> list[str]:
    try:
        paginator = control_tower_client.get_paginator("list_enabled_controls")
        enabled_controls: list[dict[str, Any]] = []

        for page in paginator.paginate(targetIdentifier=ou_arn):
            enabled_controls.extend(page.get("enabledControls", []))

        return [
            control["controlIdentifier"].split("/")[-1] for control in enabled_controls
        ]

    except control_tower_client.exceptions.ResourceNotFoundException:
        __LOGGER__.warning(
            f"OU '{ou_arn}' is not registered with AWS Control Tower. Skipping."
        )
        return []

    except ClientError as error:
        __LOGGER__.error(
            f"Unexpected ClientError while listing controls for OU '{ou_arn}': "
            f"{error.response['Error']['Code']} - "
            f"{error.response['Error']['Message']}"
        )
        raise


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
    """Gather AWS Organizations structure from the management account.

    This is a read-only AWS task. It returns organizational units, accounts,
    attached service control policies, and Control Tower enabled controls. The
    task skips non-management accounts because Organizations structure is only
    collected from the management account.

    Args:
        provider: Provider name for the current execution target.
        execution_target_id: Target AWS account ID.
        execution_target_name: Friendly name for the target account.
        execution_target_type: Provider target type.
        region: Current AWS region.
        session: Boto3 session scoped to the current region.
        dry_run: Whether execution is running in dry-run mode.
        metadata: Arbitrary config metadata for the task.
        dependency_data: Runtime data selected from declared task dependencies.
        actions: Action recorder provided by the engine.

    Returns:
        A payload containing organization ID, organizational units, and
        accounts, or `{"skipped": True}` for non-management accounts.

    Raises:
        TypeError: If AWS Organizations returns malformed account or OU IDs.
        botocore.exceptions.ClientError: If an unexpected AWS API error occurs.
    """
    account_id = execution_target_id

    org_client: BaseClient = session.client("organizations")
    control_tower_client: BaseClient = session.client("controltower")

    organization = org_client.describe_organization()["Organization"]
    management_account_id = organization["MasterAccountId"]

    if account_id != management_account_id:
        __LOGGER__.info(
            f"Skipping account '{account_id}' because it is not the management account."
        )
        return {"skipped": True}

    __LOGGER__.info(
        f"Gathering organization structure in management account '{account_id}'"
    )

    root_id = org_client.list_roots()["Roots"][0]["Id"]

    __LOGGER__.debug(f"Resolved root ID '{root_id}'")

    discovered_ous = _list_all_organizational_units(root_id, org_client)
    parent_ids = [root_id] + [
        ou_id
        for organizational_unit in discovered_ous
        if isinstance((ou_id := organizational_unit.get("Id")), str)
    ]
    accounts = _list_accounts_for_parents(org_client, parent_ids)

    with ThreadPoolExecutor(
        max_workers=min(MAX_MANAGEMENT_WORKERS, max(1, len(discovered_ous)))
    ) as executor:
        organizational_units = list(
            executor.map(
                lambda item: _enrich_organizational_unit(
                    item,
                    org_client=org_client,
                    control_tower_client=control_tower_client,
                ),
                discovered_ous,
            )
        )

    with ThreadPoolExecutor(
        max_workers=min(MAX_MANAGEMENT_WORKERS, max(1, len(accounts)))
    ) as executor:
        accounts = list(
            executor.map(
                lambda item: _enrich_account(item, org_client=org_client), accounts
            )
        )

    organizational_units.sort(key=lambda item: str(item.get("Id", "")))
    accounts.sort(key=lambda item: str(item.get("Id", "")))

    actions.record(
        f"Collected {len(organizational_units)} OUs and {len(accounts)} accounts"
    )

    return {
        "organization_id": organization["Id"],
        "organizational_units": organizational_units,
        "accounts": accounts,
    }
