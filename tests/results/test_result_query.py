from __future__ import annotations

import json

from anvil.descriptors import ConfigBranch
from anvil.result_query import (
    ResultFilters,
    build_jsonl_records_for_target,
    failure_records,
    filter_records,
)
from anvil.results import AccountResult, ExecutionStatus, TargetResult, TaskResult


def _task_result(
    *,
    task_name: str,
    region: str,
    status: ExecutionStatus,
    error: str | None = None,
) -> TaskResult:
    return TaskResult(
        task_name=task_name,
        region=region,
        status=status,
        started_at="2026-04-30T00:00:00+00:00",
        ended_at="2026-04-30T00:00:01+00:00",
        duration_seconds=1.0,
        result={"ok": status.is_success},
        error=error,
    )


def _target_result() -> TargetResult:
    return TargetResult.create(
        config_branch=ConfigBranch.ORGANIZATIONS,
        target_name="engineering",
        dry_run=True,
        account_results=[
            AccountResult(
                account_id="111111111111",
                account_alias="dev",
                status=ExecutionStatus.ERROR,
                started_at="2026-04-30T00:00:00+00:00",
                ended_at="2026-04-30T00:00:02+00:00",
                duration_seconds=2.0,
                tasks=[
                    _task_result(
                        task_name="count_vpcs",
                        region="us-east-1",
                        status=ExecutionStatus.ERROR,
                        error="boom",
                    ),
                    _task_result(
                        task_name="count_vpcs",
                        region="us-west-2",
                        status=ExecutionStatus.SUCCESS,
                    ),
                ],
                error=None,
            )
        ],
    )


def test_build_jsonl_records_flattens_accounts_and_tasks():
    records = build_jsonl_records_for_target(_target_result())

    assert [record["record_type"] for record in records] == ["account", "task", "task"]
    assert records[0]["organization"] == "engineering"
    assert records[1]["account_id"] == "111111111111"
    assert records[1]["task"] == "count_vpcs"
    assert records[1]["region"] == "us-east-1"
    assert records[1]["status"] == "error"
    json.dumps(records)


def test_filter_records_supports_failed_status_alias_and_common_fields():
    records = build_jsonl_records_for_target(_target_result())

    matches = filter_records(
        records,
        filters=ResultFilters(
            status="failed",
            organization="engineering",
            account="dev",
            region="us-east-1",
            task="count_vpcs",
        ),
    )

    assert len(matches) == 1
    assert matches[0]["record_type"] == "task"


def test_failure_records_include_account_and_task_failures():
    records = build_jsonl_records_for_target(_target_result())

    failures = failure_records(records)

    assert [record["record_type"] for record in failures] == ["account", "task"]
