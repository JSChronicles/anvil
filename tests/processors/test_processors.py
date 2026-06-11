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
