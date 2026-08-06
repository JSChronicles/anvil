from __future__ import annotations

import logging
from typing import Any

import boto3
from botocore.client import BaseClient
from botocore.exceptions import ClientError

from anvil.actions import ActionRecorder

__LOGGER__ = logging.getLogger(__name__)


def _list_organizational_units(
    parent_id: str, org_client: BaseClient
) -> list[dict[str, object]]:
    organizational_units: list[dict[str, object]] = []

    paginator = org_client.get_paginator("list_organizational_units_for_parent")

    for page in paginator.paginate(ParentId=parent_id):
        for organizational_unit in page.get("OrganizationalUnits", []):
            organizational_units.append(
                {"Name": organizational_unit["Name"], "Id": organizational_unit["Id"]}
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


def _list_accounts(org_client: BaseClient) -> list[dict[str, object]]:
    accounts: list[dict[str, object]] = []

    paginator = org_client.get_paginator("list_accounts")

    for page in paginator.paginate():
        for account in page.get("Accounts", []):
            account_copy = dict(account)

            joined_timestamp = account_copy.get("JoinedTimestamp")
            if joined_timestamp is not None:
                account_copy["JoinedTimestamp"] = joined_timestamp.isoformat()

            accounts.append(account_copy)

    return accounts


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

    organizational_units = _list_organizational_units(root_id, org_client)
    accounts = _list_accounts(org_client)

    for organizational_unit in organizational_units:
        ou_id_value = organizational_unit.get("Id")
        if not isinstance(ou_id_value, str):
            raise TypeError("OrganizationalUnit Id must be a string")

        __LOGGER__.debug(f"Gathering SCPs for OU '{organizational_unit.get('Name')}'")

        organizational_unit["Policies"] = _list_policies_for_target(
            ou_id_value, org_client
        )

        describe_response = org_client.describe_organizational_unit(
            OrganizationalUnitId=ou_id_value
        )

        ou_arn = describe_response["OrganizationalUnit"]["Arn"]

        __LOGGER__.debug(
            f"Gathering Control Tower enabled controls for OU "
            f"'{organizational_unit.get('Name')}'"
        )

        organizational_unit["EnabledControls"] = _list_enabled_controls_for_ou(
            control_tower_client, ou_arn
        )

    for account in accounts:
        account_id_value = account.get("Id")
        if not isinstance(account_id_value, str):
            raise TypeError("Account Id must be a string")

        __LOGGER__.debug(f"Gathering SCPs for account '{account.get('Name')}'")

        account["Policies"] = _list_policies_for_target(account_id_value, org_client)

    actions.record(
        f"Collected {len(organizational_units)} OUs and {len(accounts)} accounts"
    )

    return {
        "organization_id": organization["Id"],
        "organizational_units": organizational_units,
        "accounts": accounts,
    }
