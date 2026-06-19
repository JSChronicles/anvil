from __future__ import annotations

import json
from pathlib import Path

import pytest

from anvil.descriptors import ConfigBranch
from anvil.processor_loader import (
    ProcessorDescriptor,
    ProcessorRunContext,
    ProcessorSpec,
    load_historical_run_context,
    run_processors,
)
from anvil.processor_validation import ProcessorValidationError, validate_processors
from anvil.processors import html_report


def _context(tmp_path: Path) -> ProcessorRunContext:
    return ProcessorRunContext(
        config_branch=ConfigBranch.ORGANIZATIONS,
        run_dir=tmp_path,
        summary_path=tmp_path / "summary.json",
        summary={"state": "completed_success"},
        target_result_paths={},
    )


def test_validate_processors_accepts_valid_processor():
    def run(*, context, output, metadata):
        return None

    validate_processors(
        [ProcessorDescriptor(name="summary", load=lambda: run, source="stock")]
    )


def test_validate_processors_rejects_duplicate_names():
    def run(*, context, output, metadata):
        return None

    with pytest.raises(ProcessorValidationError, match="duplicate processor name"):
        validate_processors(
            [
                ProcessorDescriptor(name="summary", load=lambda: run, source="stock"),
                ProcessorDescriptor(name="summary", load=lambda: run, source="plugin"),
            ]
        )


def test_validate_processors_rejects_missing_contract_parameter():
    def run(*, context, output):
        return None

    with pytest.raises(ProcessorValidationError, match="metadata"):
        validate_processors(
            [ProcessorDescriptor(name="summary", load=lambda: run, source="stock")]
        )


def test_run_processors_executes_in_declaration_order(monkeypatch, tmp_path):
    seen: list[tuple[str, str | None, dict[str, object]]] = []

    def first(*, context, output, metadata):
        seen.append(("first", output, metadata))

    def second(*, context, output, metadata):
        seen.append(("second", output, metadata))

    processors = {"first": first, "second": second}
    monkeypatch.setattr(
        "anvil.processor_loader.load_processor_callable",
        lambda processor_name: processors[processor_name],
    )

    run_processors(
        specs=[
            ProcessorSpec("first", output="one.md", metadata={"include": True}),
            ProcessorSpec("second", output="two.md", metadata={}),
        ],
        context=_context(tmp_path),
    )

    assert seen == [("first", "one.md", {"include": True}), ("second", "two.md", {})]


def test_load_historical_run_context_reads_complete_results_directory(tmp_path):
    run_dir = tmp_path / "results" / "orgs" / "2026-06-02T120000Z"
    target_dir = run_dir / "organizations"
    target_dir.mkdir(parents=True)

    summary_path = run_dir / "summary.json"
    target_path = target_dir / "production.json"

    summary_path.write_text(
        json.dumps({"state": "completed_success"}), encoding="utf-8"
    )
    target_path.write_text(
        json.dumps({"organization": "production", "account_results": []}),
        encoding="utf-8",
    )

    context = load_historical_run_context(results_dir=run_dir)

    assert context.config_branch is ConfigBranch.ORGANIZATIONS
    assert context.summary == {"state": "completed_success"}
    assert context.target_result_paths == {"production": target_path}
    assert context.target_results == [
        {"organization": "production", "account_results": []}
    ]


def test_html_report_writes_generic_filterable_report_from_target_json(tmp_path):
    context = ProcessorRunContext(
        config_branch=ConfigBranch.ORGANIZATIONS,
        run_dir=tmp_path,
        summary_path=tmp_path / "summary.json",
        summary={"state": "completed_with_failures"},
        target_result_paths={},
        target_results=[
            {
                "organization": "production",
                "generated_at": "2026-06-01T00:00:00+00:00",
                "dry_run": False,
                "account_results": [
                    {
                        "account_id": "111111111111",
                        "account_alias": "dev",
                        "status": "error",
                        "started_at": "2026-06-01T00:00:00+00:00",
                        "ended_at": "2026-06-01T00:00:02+00:00",
                        "duration_seconds": 2.0,
                        "error": None,
                        "tasks": [
                            {
                                "task": "inventory",
                                "region": "us-east-1",
                                "status": "error",
                                "started_at": "2026-06-01T00:00:00+00:00",
                                "ended_at": "2026-06-01T00:00:01+00:00",
                                "duration_seconds": 1.25,
                                "result": {
                                    "arbitrary": {"nested": ["anything", 1]},
                                    "unsafe": "</script><b>",
                                },
                                "error": "boom",
                            }
                        ],
                    },
                    {
                        "account_id": "222222222222",
                        "account_alias": "prod",
                        "status": "success",
                        "started_at": "2026-06-01T00:00:00+00:00",
                        "ended_at": "2026-06-01T00:00:02+00:00",
                        "duration_seconds": 2.0,
                        "error": None,
                        "tasks": [],
                    },
                ],
            }
        ],
    )
    output = tmp_path / "report.html"

    result = html_report.run(
        context=context,
        output=str(output),
        metadata={"title": "Custom Status"},
    )

    rendered = output.read_text(encoding="utf-8")
    assert result == {"output": str(output), "record_count": 3}
    assert "Custom Status" in rendered
    assert "statusFilter" in rendered
    assert "taskFilter" in rendered
    assert "Failed tasks" in rendered
    assert "arbitrary" in rendered
    assert "</script><b>" not in rendered
    assert "\\u003c/script\\u003e\\u003cb\\u003e" in rendered


def test_html_report_uses_historical_target_json_for_default_output(tmp_path):
    context = ProcessorRunContext(
        config_branch=ConfigBranch.ACCOUNTS,
        run_dir=tmp_path,
        summary_path=tmp_path / "summary.json",
        summary={"state": "completed_success"},
        target_result_paths={},
        target_results=[
            {
                "account_group": "sandbox",
                "generated_at": "2026-06-01T00:00:00+00:00",
                "dry_run": False,
                "account_results": [
                    {
                        "account_id": "111111111111",
                        "account_alias": "dev",
                        "status": "success",
                        "started_at": "2026-06-01T00:00:00+00:00",
                        "ended_at": "2026-06-01T00:00:01+00:00",
                        "duration_seconds": 1.0,
                        "error": None,
                        "tasks": [
                            {
                                "task": "noop",
                                "region": "us-east-1",
                                "status": "success",
                                "started_at": "2026-06-01T00:00:00+00:00",
                                "ended_at": "2026-06-01T00:00:01+00:00",
                                "duration_seconds": 1.0,
                                "result": {"message": "ok"},
                                "error": None,
                            }
                        ],
                    }
                ],
            }
        ],
    )

    result = html_report.run(context=context, output=None, metadata={})

    output = tmp_path / "reports" / "html-report.html"
    rendered = output.read_text(encoding="utf-8")
    assert result == {"output": str(output), "record_count": 2}
    assert "sandbox" in rendered
    assert "noop" in rendered
