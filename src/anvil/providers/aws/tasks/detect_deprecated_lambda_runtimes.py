"""
Detect Lambda functions running on configured deprecated runtimes.
"""

from __future__ import annotations

import logging

from anvil.actions import ActionRecorder

__LOGGER__ = logging.getLogger(__name__)

RULE_ID = "aws.lambda.deprecated_runtime"


def _validate_runtimes(metadata: dict[str, object]) -> list[str]:
    runtimes = metadata.get("runtimes")
    if not isinstance(runtimes, list) or len(runtimes) == 0:
        raise RuntimeError(
            "detect_deprecated_lambda_runtimes requires metadata.runtimes to be a "
            "non-empty list of deprecated AWS runtime strings "
            "(e.g. ['python3.8', 'nodejs14.x'])"
        )

    validated_runtimes: list[str] = []
    for item in runtimes:
        if not isinstance(item, str) or not item.strip():
            raise RuntimeError(
                "detect_deprecated_lambda_runtimes requires every entry in "
                f"metadata.runtimes to be a non-empty string; got {item!r}"
            )
        validated_runtimes.append(item.strip())

    return validated_runtimes


def _runtime_rule() -> dict[str, object]:
    return {
        "id": RULE_ID,
        "name": "Lambda function uses a deprecated runtime",
        "short_description": "A Lambda function is configured with a deprecated runtime.",
        "full_description": (
            "Deprecated Lambda runtimes may stop receiving runtime updates and can "
            "increase operational or security risk."
        ),
        "help_markdown": (
            "Upgrade the Lambda function to a supported runtime and validate the "
            "deployment before retiring the deprecated runtime."
        ),
        "level": "warning",
        "security_severity": "6.0",
        "precision": "high",
        "tags": ["security", "aws", "lambda"],
    }


def _location_uri(*, account_id: str, region_name: str, function_name: str) -> str:
    return f"anvil/aws/{account_id}/{region_name}/lambda/functions/{function_name}.json"


def _finding_for_function(
    *,
    account_id: str,
    region_name: str,
    function_name: str,
    function_arn: str,
    runtime: str,
) -> dict[str, object]:
    return {
        "rule": _runtime_rule(),
        "message": (
            f"Lambda function {function_name} uses deprecated runtime {runtime}."
        ),
        "locations": [
            {
                "uri": _location_uri(
                    account_id=account_id,
                    region_name=region_name,
                    function_name=function_name,
                ),
                "message": f"Lambda function {function_name}",
                "properties": {
                    "aws_account_id": account_id,
                    "aws_region": region_name,
                    "aws_arn": function_arn,
                    "aws_service": "lambda",
                    "aws_resource_type": "function",
                    "function_name": function_name,
                },
            }
        ],
        "fingerprint": (
            f"{RULE_ID}:{account_id}:{region_name}:{function_arn}:{runtime}"
        ),
        "properties": {
            "function_name": function_name,
            "function_arn": function_arn,
            "runtime": runtime,
        },
    }


def _detect_deprecated_runtimes(
    lambda_client, *, account_id: str, region_name: str, deprecated_runtimes: set[str]
) -> tuple[list[dict[str, object]], int]:
    findings: list[dict[str, object]] = []
    checked_count = 0
    paginator = lambda_client.get_paginator("list_functions")

    for page in paginator.paginate():
        for function in page.get("Functions", []):
            checked_count += 1
            runtime = function.get("Runtime")
            if not isinstance(runtime, str) or runtime not in deprecated_runtimes:
                continue

            function_name = function.get("FunctionName")
            function_arn = function.get("FunctionArn")
            if not isinstance(function_name, str) or not isinstance(function_arn, str):
                continue

            findings.append(
                _finding_for_function(
                    account_id=account_id,
                    region_name=region_name,
                    function_name=function_name,
                    function_arn=function_arn,
                    runtime=runtime,
                )
            )

    return findings, checked_count


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
    """Detect Lambda functions using configured deprecated runtimes.

    This is a read-only AWS detection task. It scans Lambda functions in the
    current session region and returns SARIF-compatible findings for functions
    whose runtime matches the configured deprecated runtime list.

    Metadata:
        runtimes: Required non-empty list of deprecated AWS Lambda runtime
            strings to match, such as `python3.8` or `nodejs14.x`.

    Args:
        provider: Provider name for the current execution target.
        execution_target_id: Target AWS account ID.
        execution_target_name: Friendly name for the target account.
        execution_target_type: Provider target type.
        region: Current AWS region.
        session: Boto3 session scoped to the current region.
        dry_run: Whether execution is running in dry-run mode.
        metadata: Task metadata containing deprecated runtime filters.
        dependency_data: Runtime data selected from declared task dependencies.
        actions: Action recorder provided by the engine.

    Returns:
        A payload containing checked function count, finding count, and
        SARIF-compatible findings under `sarif_findings`.

    Raises:
        RuntimeError: If metadata.runtimes is missing or invalid.
    """
    account_id = execution_target_id
    account_alias = execution_target_name
    region_name = region
    deprecated_runtimes = set(_validate_runtimes(metadata))
    lambda_client = session.client("lambda")

    __LOGGER__.info(
        f"Detecting Lambda functions using deprecated runtimes "
        f"{sorted(deprecated_runtimes)} in account {account_alias} ({account_id}), "
        f"region={region_name}"
    )

    sarif_findings, checked_count = _detect_deprecated_runtimes(
        lambda_client,
        account_id=account_id,
        region_name=region_name,
        deprecated_runtimes=deprecated_runtimes,
    )

    actions.record(
        f"Detected {len(sarif_findings)} Lambda deprecated runtime finding(s) "
        f"out of {checked_count} function(s) checked in account {account_id} "
        f"region {region_name}"
    )

    return {
        "checked_count": checked_count,
        "finding_count": len(sarif_findings),
        "sarif_findings": sarif_findings,
    }
