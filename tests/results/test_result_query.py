from __future__ import annotations

import json

from anvil.descriptors import ConfigBranch
from anvil.result_query import (
    ResultFilters,
    build_jsonl_records_for_target,
    failure_records,
    filter_records,
    format_records_jsonl,
    format_records_table,
    limit_records,
    parse_fields,
    project_records,
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


def test_parse_fields_validates_known_fields():
    assert parse_fields("account_id, region,task") == [
        "account_id",
        "region",
        "task",
    ]


def test_parse_fields_rejects_unknown_fields():
    try:
        parse_fields("account_id,nope")
    except ValueError as error:
        assert "Unknown result field: nope" in str(error)
        assert "account_id" in str(error)
    else:
        raise AssertionError("parse_fields should reject unknown fields")


def test_limit_records_applies_after_filtering():
    records = build_jsonl_records_for_target(_target_result())

    assert limit_records(records, limit=2) == records[:2]
    assert limit_records(records, limit=None) == records


def test_project_records_keeps_requested_fields_in_order():
    records = build_jsonl_records_for_target(_target_result())

    projected = project_records(records, fields=["account_id", "region", "task"])

    assert list(projected[0]) == ["account_id", "region", "task"]
    assert projected[0] == {
        "account_id": "111111111111",
        "region": None,
        "task": None,
    }
    assert projected[1] == {
        "account_id": "111111111111",
        "region": "us-east-1",
        "task": "count_vpcs",
    }


def test_format_records_table_uses_default_and_selected_fields():
    records = build_jsonl_records_for_target(_target_result())

    default_table = format_records_table(records)
    selected_table = format_records_table(records, fields=["account_id", "task"])

    assert "type" in default_table
    assert "alias" in default_table
    assert "account_id" in selected_table
    assert "count_vpcs" in selected_table
    assert "alias" not in selected_table


def test_format_records_jsonl_outputs_one_json_object_per_line():
    records = project_records(
        build_jsonl_records_for_target(_target_result())[:2],
        fields=["account_id", "task"],
    )

    output = format_records_jsonl(records)
    lines = output.splitlines()

    assert len(lines) == 2
    assert json.loads(lines[0]) == {"account_id": "111111111111", "task": None}
    assert json.loads(lines[1]) == {
        "account_id": "111111111111",
        "task": "count_vpcs",
    }
