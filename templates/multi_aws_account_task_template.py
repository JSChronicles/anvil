from __future__ import annotations

import argparse
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

__LOGGER__ = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.INFO, format="%(levelname)-8s [%(filename)s:%(lineno)d] %(message)s"
)

BOTO_CONFIG = Config(max_pool_connections=30)


def assume_role(
    session: boto3.Session,
    account_id: str,
    role_name: str = "OrganizationAccountAccessRole",
    role_session_name: str = "OrgAcctAccessRole",
) -> boto3.Session:
    """Assume role into the given AWS account and return a new boto3 Session."""
    __LOGGER__.debug("Creating AWS STS client")
    sts_client = session.client("sts", config=BOTO_CONFIG)
    role_arn = f"arn:aws:iam::{account_id}:role/{role_name}"

    __LOGGER__.debug(f"Assuming role {role_arn}")

    try:
        response = sts_client.assume_role(
            RoleArn=role_arn, RoleSessionName=role_session_name
        )
    except ClientError as error:
        __LOGGER__.error(f"Failed to assume role into account {account_id}: {error}")
        raise

    credentials = response["Credentials"]

    return boto3.Session(
        aws_access_key_id=credentials["AccessKeyId"],
        aws_secret_access_key=credentials["SecretAccessKey"],
        aws_session_token=credentials["SessionToken"],
        region_name=session.region_name,
    )


def get_all_accounts(session: boto3.Session) -> list[dict[str, object]]:
    """
    Retrieve active accounts in the AWS Organization and flag the management account.

    Args:
        session: Root boto3 session.

    Returns:
        List of active account dictionaries.
    """
    __LOGGER__.debug("Creating AWS Organizations client")
    org_client = session.client("organizations", config=BOTO_CONFIG)

    __LOGGER__.debug("Fetching organization information")
    org_info = org_client.describe_organization()["Organization"]

    __LOGGER__.info("Fetching active accounts in the organization")
    accounts: list[dict[str, object]] = []

    try:
        paginator = org_client.get_paginator("list_accounts")
        for page in paginator.paginate():
            for account in page["Accounts"]:
                if account.get("State") != "ACTIVE":
                    __LOGGER__.debug(
                        f"Skipping non-active account {account['Id']} "
                        f"({account['Name']}): state={account.get('State')}"
                    )
                    continue

                accounts.append(
                    {
                        "AWSOrganizationID": org_info["Id"],
                        "AWSAccountID": account["Id"],
                        "AWSAccountName": account["Name"],
                        "IsManagement": account["Id"] == org_info["MasterAccountId"],
                        "State": account.get("State"),
                    }
                )
    except ClientError as error:
        __LOGGER__.error(f"Failed to retrieve accounts from AWS Organizations: {error}")
        raise

    __LOGGER__.info(
        f"Found {len(accounts)} active accounts "
        f"(management={org_info['MasterAccountId']})"
    )
    return accounts


def account_task(
    account_session: boto3.Session,
    account: dict[str, object],
    region: str,
    dry_run: bool,
    example_piece: str | None,
) -> dict[str, object]:
    """
    Replace this function with the per-account, per-region work for your script.

    Args:
        account_session: Session for the target account and region.
        account: Account dictionary from get_all_accounts().
        region: Current AWS region being processed.
        dry_run: Whether to simulate actions only.
        example_piece: Example task-specific argument passed into account_task.

    Returns:
        Result dictionary for the processed account-region combination.

    Notes:
        - Dry-run messages should start with "(dry-run)".
        - Neutral informational messages such as "not found" or "already compliant"
          do not need the dry-run prefix.
        - Region is explicit here for clarity, but account_session is also already
          region-scoped.
    """
    account_id = str(account["AWSAccountID"])
    account_name = str(account["AWSAccountName"])

    # Replace this section with the actual logic you want to run.
    #
    # Example:
    # iam_client = account_session.client("iam", config=BOTO_CONFIG)
    # ec2_client = account_session.client("ec2", config=BOTO_CONFIG)
    #
    # The session passed in is already scoped to the current region.

    if dry_run:
        __LOGGER__.info(
            f"(dry-run) Performed <action_here> for "
            f"{account_id} ({account_name}) in {region}"
        )
    else:
        __LOGGER__.info(
            f"Performed <action_here> for {account_id} ({account_name}) in {region}"
        )

    return {
        "Status": "success",
        "Changed": not dry_run,
        "Message": f"Example result message for {region}",
    }


def process_account(
    base_session: boto3.Session,
    account: dict[str, object],
    regions: list[str],
    dry_run: bool,
    role_name: str,
    example_piece: str | None,
) -> dict[str, object]:
    """
    Process one AWS account concurrently across all configured regions.

    This wrapper handles session setup, assume-role behavior, per-region execution,
    error handling, and standard result formatting.
    """
    account_id = str(account["AWSAccountID"])
    account_name = str(account["AWSAccountName"])
    is_management = bool(account["IsManagement"])

    __LOGGER__.info(f"Processing AWS account: {account_id} ({account_name})")

    region_results: list[dict[str, object]] = []
    overall_status = "success"
    overall_changed = False

    for region in regions:
        __LOGGER__.debug(
            f"Preparing account session for {account_id} ({account_name}) in {region}"
        )

        try:
            region_base_session = boto3.Session(
                profile_name=base_session.profile_name, region_name=region
            )

            if is_management:
                __LOGGER__.debug(
                    f"Using management account session for {account_id} in {region}"
                )
                account_session = region_base_session
            else:
                account_session = assume_role(
                    session=region_base_session,
                    account_id=account_id,
                    role_name=role_name,
                )

            result = account_task(
                account_session=account_session,
                account=account,
                region=region,
                dry_run=dry_run,
                example_piece=example_piece,
            )

            region_changed = bool(result.get("Changed", not dry_run))
            region_status = str(result.get("Status", "success"))

            extra_result_fields = {
                key: value
                for key, value in result.items()
                if key not in {"Region", "Status", "Changed", "Message"}
            }

            region_result = {
                "Region": region,
                "Status": region_status,
                "Changed": region_changed,
                "Message": result.get("Message", ""),
                **extra_result_fields,
            }
            region_results.append(region_result)

            if region_status != "success":
                overall_status = "error"

            if region_changed:
                overall_changed = True

        except ClientError as error:
            __LOGGER__.error(
                f"ClientError while processing account "
                f"{account_id} ({account_name}) in {region}: {error}"
            )
            region_results.append(
                {
                    "Region": region,
                    "Status": "error",
                    "Changed": False,
                    "Message": str(error),
                }
            )
            overall_status = "error"

        except Exception as error:
            __LOGGER__.exception(
                f"Unexpected error while processing account "
                f"{account_id} ({account_name}) in {region}"
            )
            region_results.append(
                {
                    "Region": region,
                    "Status": "error",
                    "Changed": False,
                    "Message": str(error),
                }
            )
            overall_status = "error"

    return {
        "AWSAccountID": account_id,
        "AWSAccountName": account_name,
        "IsManagement": is_management,
        "Status": overall_status,
        "Changed": overall_changed,
        "Regions": region_results,
    }


def orchestrate(
    example_piece: str | None,
    include: list[str] | None,
    exclude: list[str] | None,
    output: str | None,
    profile: str | None,
    regions: list[str],
    dry_run: bool,
    role_name: str,
    max_workers: int,
) -> list[dict[str, object]]:
    """
    Orchestrate the full multi-account, multi-region run.

    This function creates the root session, retrieves active organization
    accounts, applies include/exclude filtering, runs per-account work in
    parallel, and returns the collected results.

    Args:
        example_piece: Example task-specific argument passed into account_task.
        include: Account IDs to include.
        exclude: Account IDs to exclude.
        output: Optional output JSON path.
        profile: Optional AWS profile name.
        regions: AWS regions to process.
        dry_run: Whether to simulate changes only.
        role_name: Role name for non-management accounts.
        max_workers: Thread pool size.

    Returns:
        List of per-account result dictionaries.
    """
    if not regions:
        raise ValueError("regions must contain at least one region")

    normalized_regions = [region.strip() for region in regions]
    if any(not region for region in normalized_regions):
        raise ValueError("regions must not contain empty values")

    if len(set(normalized_regions)) != len(normalized_regions):
        raise ValueError("regions must not contain duplicates")

    bootstrap_region = normalized_regions[0]

    __LOGGER__.debug(
        f"Creating root boto3 session using bootstrap region {bootstrap_region}"
    )
    base_session = boto3.Session(profile_name=profile, region_name=bootstrap_region)

    accounts = get_all_accounts(base_session)
    org_account_ids = {str(account["AWSAccountID"]) for account in accounts}

    __LOGGER__.debug("Determining which account IDs to process")
    if include:
        requested_ids = set(include)
        missing = requested_ids - org_account_ids
        if missing:
            __LOGGER__.warning(
                "These account IDs were requested but were not found as ACTIVE "
                f"organization accounts: {sorted(missing)}"
            )
        target_ids = requested_ids & org_account_ids
    else:
        excluded_ids = set(exclude or [])
        missing_excluded_ids = excluded_ids - org_account_ids
        if missing_excluded_ids:
            __LOGGER__.warning(
                "These account IDs were excluded but were not found as ACTIVE "
                f"organization accounts: {sorted(missing_excluded_ids)}"
            )
        target_ids = org_account_ids - excluded_ids

    target_accounts = [
        account for account in accounts if str(account["AWSAccountID"]) in target_ids
    ]

    __LOGGER__.info(
        f"Processing {len(target_accounts)} account(s) across "
        f"{len(normalized_regions)} region(s): {normalized_regions}"
    )

    results: list[dict[str, object]] = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(
                process_account,
                base_session,
                target_account,
                normalized_regions,
                dry_run,
                role_name,
                example_piece,
            ): target_account
            for target_account in target_accounts
        }

        for future in as_completed(future_map):
            result = future.result()
            results.append(result)

    results.sort(key=lambda item: str(item["AWSAccountID"]))

    if output:
        with open(output, "w", encoding="utf-8") as output_file:
            json.dump(results, output_file, indent=2)
        __LOGGER__.info(f"Results written to {output}")
    else:
        print(json.dumps(results, indent=2))

    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=(
            "Template for running work across AWS Organization accounts and regions."
        )
    )
    parser.add_argument(
        "--example-piece",
        required=False,
        help="Example task-specific argument passed into account_task",
    )

    account_group = parser.add_mutually_exclusive_group()
    account_group.add_argument(
        "--include", nargs="+", help="Account IDs to only include"
    )
    account_group.add_argument("--exclude", nargs="+", help="Account IDs to exclude")

    parser.add_argument(
        "--dry-run", action="store_true", help="Simulate actions without making changes"
    )
    parser.add_argument(
        "--role-name",
        default="OrganizationAccountAccessRole",
        help="Role name to assume in non-management accounts",
    )
    parser.add_argument(
        "--regions",
        nargs="+",
        default=["us-east-1"],
        help="AWS regions to process (default: us-east-1)",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=10,
        help="Maximum number of accounts to process in parallel",
    )
    parser.add_argument("-o", "--output", required=False, help="Output JSON file path")
    parser.add_argument(
        "-p", "--profile", type=str, required=False, help="AWS profile name"
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Set the logging level (default: INFO)",
    )

    args = parser.parse_args()

    __LOGGER__.setLevel(getattr(logging, args.log_level))

    orchestrate(
        example_piece=args.example_piece,
        include=args.include,
        exclude=args.exclude,
        output=args.output,
        profile=args.profile,
        regions=args.regions,
        dry_run=args.dry_run,
        role_name=args.role_name,
        max_workers=args.max_workers,
    )


# This template provides:
# - AWS Organizations account discovery
# - active-account filtering
#   - `--include` / `--exclude` account selection
# - parallel per-account execution
#   - multiple regions per account
# - assume-role handling for member accounts
# - dry-run support
# - JSON result output

# Replace the innards of the `account_task()` function with your own per-account logic.
# Replace the `--example-piece` argparse and `example_piece` in other areas or edit as desired
