"""
List Lambda functions matching specified runtime versions in the current execution region.
"""

from __future__ import annotations

import logging

from anvil.actions import ActionRecorder

__LOGGER__ = logging.getLogger(__name__)


def _validate_runtimes(metadata: dict[str, object]) -> list[str]:
    runtimes = metadata.get("runtimes")
    if not isinstance(runtimes, list) or len(runtimes) == 0:
        raise RuntimeError(
            "list_lambdas_by_runtime requires metadata.runtimes to be a non-empty list "
            "of AWS runtime strings (e.g. ['python3.8', 'nodejs14.x'])"
        )
    validated_runtimes: list[str] = []
    for item in runtimes:
        if not isinstance(item, str):
            raise RuntimeError(
                "list_lambdas_by_runtime requires every entry in metadata.runtimes "
                f"to be a string; got {type(item).__name__!r}: {item!r}"
            )
        validated_runtimes.append(item)
    return validated_runtimes


def _list_matching_functions(
    lambda_client, target_runtimes: set[str]
) -> tuple[list[dict[str, object]], int]:
    matched: list[dict[str, object]] = []
    total_scanned = 0
    paginator = lambda_client.get_paginator("list_functions")

    for page in paginator.paginate():
        for function in page.get("Functions", []):
            total_scanned += 1
            runtime = function.get("Runtime")
            if not isinstance(runtime, str):
                continue
            if runtime not in target_runtimes:
                continue
            matched.append(
                {
                    "function_name": function.get("FunctionName", ""),
                    "function_arn": function.get("FunctionArn", ""),
                    "runtime": runtime,
                    "description": function.get("Description", ""),
                    "last_modified": function.get("LastModified", ""),
                    "code_size": function.get("CodeSize", 0),
                }
            )

    return matched, total_scanned


def run(
    *,
    account_id: str,
    account_alias: str,
    session,
    dry_run: bool,
    metadata: dict[str, object],
    actions: ActionRecorder,
) -> dict[str, object]:
    """List Lambda functions using any configured runtime.

    This is a read-only AWS task. It scans Lambda functions in the current
    session region and groups matching functions by runtime.

    Metadata:
        runtimes: Required non-empty list of AWS Lambda runtime strings to
            match, such as `python3.8` or `nodejs18.x`.

    Args:
        account_id: Target AWS account ID.
        account_alias: Friendly name for the target account.
        session: Boto3 session scoped to the current region.
        dry_run: Whether execution is running in dry-run mode.
        metadata: Task metadata containing runtime filters.
        actions: Action recorder provided by the engine.

    Returns:
        A payload containing matching functions, matches grouped by runtime,
        scan totals, and the target runtime list.

    Raises:
        RuntimeError: If metadata.runtimes is missing or invalid.
    """
    region_name = session.region_name
    target_runtimes = set(_validate_runtimes(metadata))
    lambda_client = session.client("lambda")

    __LOGGER__.info(
        f"Scanning Lambda functions for runtimes {sorted(target_runtimes)} in "
        f"account {account_alias} ({account_id}), region={region_name}"
    )

    matched, total_scanned = _list_matching_functions(lambda_client, target_runtimes)

    by_runtime: dict[str, list[dict[str, object]]] = {}
    for function in matched:
        runtime = function["runtime"]
        if not isinstance(runtime, str):
            continue
        if runtime not in by_runtime:
            by_runtime[runtime] = []
        by_runtime[runtime].append(function)

    runtime_counts = {
        runtime: len(functions) for runtime, functions in by_runtime.items()
    }

    __LOGGER__.info(
        f"Found {len(matched)} matching Lambda function(s) out of {total_scanned} scanned "
        f"in account {account_alias} ({account_id}), region={region_name}: {runtime_counts}"
    )

    actions.record(
        f"Listed Lambda functions by runtime in account {account_id} region {region_name}: "
        f"{len(matched)} matched out of {total_scanned} scanned ({runtime_counts})"
    )

    return {
        "functions": matched,
        "by_runtime": by_runtime,
        "summary": {
            "total_scanned": total_scanned,
            "total_matched": len(matched),
            "matched_by_runtime": runtime_counts,
            "target_runtimes": sorted(target_runtimes),
        },
    }
