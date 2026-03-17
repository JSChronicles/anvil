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
    __LOGGER__.debug("Creating AWS sts client")
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
    dry_run: bool,
    example_piece: str | None,
) -> dict[str, object]:
    """
    Replace this function with the per-account work for your script.

    Args:
        account_session: Session for the target account.
        account: Account dictionary from get_all_accounts().
        dry_run: Whether to simulate actions only.
        example_piece: Example task-specific argument passed into account_task.

    Returns:
        Result dictionary for the processed account.

    Notes:
        - Dry-run messages should start with "(dry-run)".
        - Neutral informational messages such as "not found" or "already compliant"
          do not need the dry-run prefix.
    """
    account_id = str(account["AWSAccountID"])
    account_name = str(account["AWSAccountName"])
    #
    # Replace this section with the actual logic you want to run.
    #
    # Example:
    # iam_client = account_session.client("iam", config=BOTO_CONFIG)
    # s3_client = account_session.client("s3", config=BOTO_CONFIG)
    #

    if dry_run:
        __LOGGER__.info(
            f"(dry_run) Performed <action_here> for {account_id} ({account_name})"
        )
    else:
        # Replace this section with the actual logic you want to run.
        #
        # Example:
        # iam_client = account_session.client("iam", config=BOTO_CONFIG)
        # s3_client = account_session.client("s3", config=BOTO_CONFIG)
        __LOGGER__.info(f"Performed <action_here> for {account_id} ({account_name})")

    # Use `Message` only when it adds value, for example: f"Deleted {deleted_count} unused access keys"
    return {
        "Status": "success",
        "Changed": not dry_run,
        "Message": "Example result message",
    }


def process_account(
    session: boto3.Session,
    account: dict[str, object],
    dry_run: bool,
    role_name: str,
    example_piece: str | None,
) -> dict[str, object]:
    """
    Process one AWS account concurrently.

    This wrapper handles session setup, assume-role behavior, error handling,
    and standard result formatting.
    """
    account_id = str(account["AWSAccountID"])
    account_name = str(account["AWSAccountName"])
    is_management = bool(account["IsManagement"])

    __LOGGER__.info(f"Processing AWS account: {account_id} ({account_name})")

    try:
        if is_management:
            __LOGGER__.debug(f"Using management account session for {account_id}")
            account_session = session
        else:
            account_session = assume_role(
                session=session, account_id=account_id, role_name=role_name
            )

        result = account_task(
            account_session=account_session,
            account=account,
            dry_run=dry_run,
            example_piece=example_piece,
        )

        extra_result_fields = {
            key: value
            for key, value in result.items()
            if key
            not in {
                "AWSAccountID",
                "AWSAccountName",
                "IsManagement",
                "Status",
                "Changed",
                "Message",
            }
        }

        return {
            "AWSAccountID": account_id,
            "AWSAccountName": account_name,
            "IsManagement": is_management,
            "Status": result.get("Status", "success"),
            "Changed": result.get("Changed", not dry_run),
            "Message": result.get("Message", ""),
            **extra_result_fields,
        }

    except ClientError as error:
        __LOGGER__.error(
            f"ClientError while processing account {account_id} ({account_name}): {error}"
        )
        message = str(error)
    except Exception as error:
        __LOGGER__.exception(
            f"Unexpected error while processing account {account_id} ({account_name})"
        )
        message = str(error)

    return {
        "AWSAccountID": account_id,
        "AWSAccountName": account_name,
        "IsManagement": is_management,
        "Status": "error",
        "Changed": False,
        "Message": message,
    }


def orchestrate(
    example_piece: str | None,
    include: list[str] | None,
    exclude: list[str] | None,
    output: str | None,
    profile: str | None,
    region: str,
    dry_run: bool,
    role_name: str,
    max_workers: int,
) -> list[dict[str, object]]:
    """
    Orchestrate the full multi-account run.

    This function creates the root session, retrieves active organization
    accounts, applies include/exclude filtering, runs per-account work in
    parallel, and returns the collected results.

    Args:
        example_piece: Example task-specific argument passed into account_task.
        include: Account IDs to include.
        exclude: Account IDs to exclude.
        output: Optional output JSON path.
        profile: Optional AWS profile name.
        region: AWS region to use for boto3 clients/session.
        dry_run: Whether to simulate changes only.
        role_name: Role name for non-management accounts.
        max_workers: Thread pool size.

    Returns:
        List of per-account result dictionaries.
    """
    __LOGGER__.debug("Creating boto3 session")
    session = boto3.Session(profile_name=profile, region_name=region)
    accounts = get_all_accounts(session)

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
        target_ids = org_account_ids - excluded_ids

    target_accounts = [
        account for account in accounts if str(account["AWSAccountID"]) in target_ids
    ]

    __LOGGER__.info(f"Processing {len(target_accounts)} account(s)")

    results: list[dict[str, object]] = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(
                process_account,
                session,
                target_account,
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
        description="Template for running work across AWS Organization accounts."
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
        "--region",
        default="us-east-1",
        help="AWS region to use for boto3 clients/session (default: us-east-1)",
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
        region=args.region,
        dry_run=args.dry_run,
        role_name=args.role_name,
        max_workers=args.max_workers,
    )
