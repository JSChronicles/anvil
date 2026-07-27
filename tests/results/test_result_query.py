from __future__ import annotations

import json

from anvil.result_query import (
    ResultFilters,
    build_jsonl_records_for_target,
    build_rerun_targets,
    config_file_for_failure_records,
    failure_records,
    filter_records,
    format_records_jsonl,
    format_records_table,
    iter_result_records,
    limit_records,
    load_result_records,
    parse_fields,
    project_records,
    query_result_records,
)
from anvil.results import EntityResult, ExecutionStatus, TargetResult, TaskResult


def _task_result(
    *, task_name: str, region: str, status: ExecutionStatus, error: str | None = None
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
        target_name="engineering",
        provider="aws",
        dry_run=True,
        entities=[
            EntityResult(
                id="111111111111",
                name="dev",
                type="account",
                provider="aws",
                metadata={},
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


def test_build_jsonl_records_flattens_entities_and_tasks():
    records = build_jsonl_records_for_target(_target_result())

    assert [record["record_type"] for record in records] == ["entity", "task", "task"]
    assert records[0]["target"] == "engineering"
    assert records[1]["entity_id"] == "111111111111"
    assert records[1]["entity_name"] == "dev"
    assert records[1]["entity_type"] == "account"
    assert records[1]["task"] == "count_vpcs"
    assert records[1]["region"] == "us-east-1"
    assert records[1]["status"] == "error"
    json.dumps(records)


def test_build_jsonl_records_includes_config_file_when_supplied():
    from pathlib import Path

    records = build_jsonl_records_for_target(
        _target_result(), config_file=Path("yaml/orgs.yaml")
    )

    assert records[0]["config_file"] == "yaml/orgs.yaml"
    assert records[1]["config_file"] == "yaml/orgs.yaml"
    assert Path(records[0]["config_file_resolved"]) == Path("yaml/orgs.yaml").resolve()
    assert Path(records[1]["config_file_resolved"]) == Path("yaml/orgs.yaml").resolve()


def test_filter_records_supports_failed_status_alias_and_common_fields():
    records = build_jsonl_records_for_target(_target_result())

    matches = filter_records(
        records,
        filters=ResultFilters(
            status="failed",
            target="engineering",
            entity="dev",
            region="us-east-1",
            task="count_vpcs",
        ),
    )

    assert len(matches) == 1
    assert matches[0]["record_type"] == "task"


def test_filter_records_failed_status_matches_any_non_success_status():
    records = [
        {"record_type": "entity", "status": "success", "entity_id": "111"},
        {"record_type": "entity", "status": "error", "entity_id": "222"},
        {"record_type": "entity", "status": "interrupted", "entity_id": "333"},
    ]

    matches = filter_records(records, filters=ResultFilters(status="failed"))

    assert [record["entity_id"] for record in matches] == ["222", "333"]


def test_filter_records_supports_record_type_filter():
    records = build_jsonl_records_for_target(_target_result())

    matches = filter_records(records, filters=ResultFilters(record_type="entity"))

    assert [record["record_type"] for record in matches] == ["entity"]


def test_failure_records_include_entity_and_task_failures():
    records = build_jsonl_records_for_target(_target_result())

    failures = failure_records(records)

    assert [record["record_type"] for record in failures] == ["entity", "task"]


def test_failure_records_include_any_non_success_status():
    records = [
        {"record_type": "entity", "status": "success"},
        {"record_type": "entity", "status": "interrupted"},
        {"record_type": "task", "status": "cancelled"},
    ]

    failures = failure_records(records)

    assert [record["status"] for record in failures] == ["interrupted", "cancelled"]


def test_config_file_for_failure_records_groups_by_config_path():
    from pathlib import Path

    records = [
        {
            "config_file": "orgs.yaml",
            "config_file_resolved": str(Path.cwd() / "orgs.yaml"),
            "status": "error",
        },
        {
            "config_file": "accounts.yaml",
            "config_file_resolved": str(Path.cwd() / "accounts.yaml"),
            "status": "interrupted",
        },
        {
            "config_file": "orgs.yaml",
            "config_file_resolved": str(Path.cwd() / "orgs.yaml"),
            "status": "error",
        },
    ]

    grouped = config_file_for_failure_records(failures=records)

    assert list(grouped) == [Path.cwd() / "orgs.yaml", Path.cwd() / "accounts.yaml"]
    assert len(grouped[Path.cwd() / "orgs.yaml"]) == 2


def test_build_rerun_targets_includes_interrupted_task_dependencies():
    from anvil.descriptors import LoadedConfig, TargetDescriptor

    loaded_config = LoadedConfig(
        targets=[
            TargetDescriptor(
                name="org-a",
                provider="aws",
                mode="organization",
                regions=["us-east-1", "us-west-2"],
                tasks=[
                    {"name": "inventory"},
                    {"name": "cleanup", "depends_on": ["inventory"]},
                ],
            )
        ]
    )

    targets = build_rerun_targets(
        loaded_config=loaded_config,
        failures=[
            {
                "record_type": "task",
                "target": "org-a",
                "entity_id": "111111111111",
                "region": "us-west-2",
                "task": "cleanup",
                "status": "interrupted",
            }
        ],
    )

    assert len(targets) == 1
    assert targets[0].include == ["111111111111"]
    assert targets[0].regions == ["us-west-2"]
    assert targets[0].tasks == [
        {"name": "inventory"},
        {"name": "cleanup", "depends_on": ["inventory"]},
    ]


def test_parse_fields_validates_known_fields():
    assert parse_fields("entity_id, entity_metadata,region") == [
        "entity_id",
        "entity_metadata",
        "region",
    ]


def test_parse_fields_rejects_unknown_fields():
    try:
        parse_fields("entity_id,nope")
    except ValueError as error:
        assert "Unknown result field: nope" in str(error)
        assert "entity_id" in str(error)
    else:
        raise AssertionError("parse_fields should reject unknown fields")


def test_limit_records_applies_after_filtering():
    records = build_jsonl_records_for_target(_target_result())

    assert limit_records(records, limit=2) == records[:2]
    assert limit_records(records, limit=None) == records


def test_project_records_keeps_requested_fields_in_order():
    records = build_jsonl_records_for_target(_target_result())

    projected = project_records(records, fields=["entity_id", "region", "task"])

    assert list(projected[0]) == ["entity_id", "region", "task"]
    assert projected[0] == {"entity_id": "111111111111", "region": None, "task": None}
    assert projected[1] == {
        "entity_id": "111111111111",
        "region": "us-east-1",
        "task": "count_vpcs",
    }


def test_format_records_table_uses_default_and_selected_fields():
    records = build_jsonl_records_for_target(_target_result())

    default_table = format_records_table(records)
    selected_table = format_records_table(records, fields=["entity_id", "task"])

    assert "type" in default_table
    assert "entity_name" in default_table
    assert "entity_id" in selected_table
    assert "count_vpcs" in selected_table
    assert "entity_name" not in selected_table


def test_format_records_jsonl_outputs_one_json_object_per_line():
    records = project_records(
        build_jsonl_records_for_target(_target_result())[:2],
        fields=["entity_id", "task"],
    )

    output = format_records_jsonl(records)
    lines = output.splitlines()

    assert len(lines) == 2
    assert json.loads(lines[0]) == {"entity_id": "111111111111", "task": None}
    assert json.loads(lines[1]) == {"entity_id": "111111111111", "task": "count_vpcs"}


def test_load_result_records_discovers_nested_results_jsonl():
    from pathlib import Path

    scratch_dir = (Path("tests") / "_tmp" / "result-query").resolve()
    results_dir = scratch_dir / "results"
    config_dir = results_dir / "orgs"
    run_dir = config_dir / "2026-05-01T120000Z"
    results_file = run_dir / "results.jsonl"

    try:
        run_dir.mkdir(parents=True)
        results_file.write_text('{"record_type":"entity","status":"success"}\n')

        records = load_result_records(results_dir=results_dir, files=None)

        assert records == [{"record_type": "entity", "status": "success"}]
    finally:
        results_file.unlink(missing_ok=True)
        if run_dir.exists():
            run_dir.rmdir()
        if config_dir.exists():
            config_dir.rmdir()
        if results_dir.exists():
            results_dir.rmdir()
        if scratch_dir.exists():
            scratch_dir.rmdir()


def test_iter_result_records_preserves_explicit_file_order(tmp_path):
    first_file = tmp_path / "first.jsonl"
    second_file = tmp_path / "second.jsonl"
    first_file.write_text('{"entity_id":"first"}\n', encoding="utf-8")
    second_file.write_text('{"entity_id":"second"}\n', encoding="utf-8")

    records = list(
        iter_result_records(results_dir=tmp_path, files=[second_file, first_file])
    )

    assert [record["entity_id"] for record in records] == ["second", "first"]


def test_load_result_records_rejects_invalid_json(tmp_path):
    results_file = tmp_path / "results.jsonl"
    results_file.write_text('{"record_type":"entity"}\nnot-json\n', encoding="utf-8")

    try:
        load_result_records(results_dir=tmp_path, files=[results_file])
    except ValueError as error:
        assert f"Invalid JSONL in {results_file} on line 2" in str(error)
    else:
        raise AssertionError("invalid JSONL should fail")


def test_load_result_records_rejects_non_object_records(tmp_path):
    results_file = tmp_path / "results.jsonl"
    results_file.write_text('["not", "object"]\n', encoding="utf-8")

    try:
        load_result_records(results_dir=tmp_path, files=[results_file])
    except ValueError as error:
        assert f"Invalid JSONL in {results_file} on line 1" in str(error)
        assert "expected object" in str(error)
    else:
        raise AssertionError("non-object JSONL should fail")


def test_query_result_records_applies_limit_after_filters_and_stops_reading(tmp_path):
    results_file = tmp_path / "results.jsonl"
    results_file.write_text(
        "\n".join(
            [
                '{"record_type":"task","status":"success","entity_id":"skip"}',
                '{"record_type":"task","status":"error","entity_id":"match-1"}',
                '{"record_type":"entity","status":"error","entity_id":"skip-type"}',
                '{"record_type":"task","status":"interrupted","entity_id":"match-2"}',
                "not-json",
            ]
        ),
        encoding="utf-8",
    )

    records = query_result_records(
        results_dir=tmp_path,
        files=[results_file],
        filters=ResultFilters(record_type="task", status="failed"),
        limit=2,
    )

    assert [record["entity_id"] for record in records] == ["match-1", "match-2"]
