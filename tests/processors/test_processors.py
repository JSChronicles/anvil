from __future__ import annotations

import json
from pathlib import Path

import pytest

from anvil.processor_loader import (
    ProcessorDescriptor,
    ProcessorRunContext,
    ProcessorSpec,
    load_completed_run_context,
    run_processors,
)
from anvil.processor_validation import ProcessorValidationError, validate_processors
from anvil.processors import html_report
from anvil.processors import sarif_report


def _context(tmp_path: Path) -> ProcessorRunContext:
    return ProcessorRunContext(
        run_dir=tmp_path,
        summary_path=tmp_path / "summary.json",
        summary={"state": "completed_success"},
        target_result_paths={},
    )


def test_processor_catalog_scans_entry_points_once(monkeypatch):
    from anvil import processor_loader

    calls = 0

    def fake_entry_points(*, group):
        nonlocal calls
        calls += 1
        return []

    monkeypatch.setattr(processor_loader, "entry_points", fake_entry_points)
    processor_loader._clear_processor_caches()

    processor_loader.list_processors()
    processor_loader.list_processors()

    assert calls == 1


def test_clear_processor_caches_refreshes_entry_points(monkeypatch):
    from anvil import processor_loader

    calls = 0

    def fake_entry_points(*, group):
        nonlocal calls
        calls += 1
        return []

    monkeypatch.setattr(processor_loader, "entry_points", fake_entry_points)
    processor_loader._clear_processor_caches()

    processor_loader.list_processors()
    processor_loader._clear_processor_caches()
    processor_loader.list_processors()

    assert calls == 2


def test_validate_processors_accepts_valid_processor():
    def run(*, context, output, metadata):
        """Run a valid processor."""

        return None

    validate_processors(
        [ProcessorDescriptor(name="summary", load=lambda: run, source="stock")]
    )


def test_validate_processors_rejects_duplicate_names():
    def run(*, context, output, metadata):
        """Run a duplicate processor."""

        return None

    with pytest.raises(ProcessorValidationError, match="duplicate processor name"):
        validate_processors(
            [
                ProcessorDescriptor(name="summary", load=lambda: run, source="stock"),
                ProcessorDescriptor(name="summary", load=lambda: run, source="plugin"),
            ]
        )


def test_load_processor_rejects_duplicate_catalog_candidates(monkeypatch):
    from anvil import processor_loader
    from anvil._components import (
        ComponentCatalog,
        ComponentDescriptor,
        ComponentOrigin,
        ComponentSource,
    )

    descriptors = [
        ComponentDescriptor(
            name="shared",
            source=ComponentSource(
                origin=ComponentOrigin.STOCK, package="stock", label="stock"
            ),
            load=lambda: lambda **kwargs: None,
        ),
        ComponentDescriptor(
            name="shared",
            source=ComponentSource(
                origin=ComponentOrigin.PLUGIN, package="plugin", label="plugin: example"
            ),
            load=lambda: lambda **kwargs: None,
        ),
    ]
    processor_loader._clear_processor_caches()
    monkeypatch.setattr(
        processor_loader,
        "_processor_catalog",
        lambda: ComponentCatalog.build(descriptors),
    )

    with pytest.raises(processor_loader.ProcessorConfigError, match="ambiguous"):
        processor_loader.load_processor_callable("shared")


def test_validate_processors_rejects_missing_contract_parameter():
    def run(*, context, output):
        """Run an invalid processor."""

        return None

    with pytest.raises(ProcessorValidationError, match="metadata"):
        validate_processors(
            [ProcessorDescriptor(name="summary", load=lambda: run, source="stock")]
        )


def test_validate_processors_rejects_additional_required_parameter():
    def run(*, context, output, metadata, extra):
        """Run an invalid processor with an unsupplied parameter."""

        return None

    with pytest.raises(ProcessorValidationError, match="extra"):
        validate_processors(
            [ProcessorDescriptor(name="summary", load=lambda: run, source="stock")]
        )


def test_validate_processors_requires_keyword_only_contract_parameters():
    def run(context, *, output, metadata):
        """Run an invalid processor with a positional-or-keyword parameter."""

        return None

    with pytest.raises(ProcessorValidationError, match="keyword-only"):
        validate_processors(
            [ProcessorDescriptor(name="summary", load=lambda: run, source="stock")]
        )


def test_validate_processors_rejects_missing_detail_docstring():
    def run(*, context, output, metadata):
        return None

    with pytest.raises(ProcessorValidationError, match="detail documentation"):
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


def test_run_processors_isolates_processor_metadata(monkeypatch, tmp_path):
    def mutate(*, context, output, metadata):
        metadata["changed"] = True

    monkeypatch.setattr(
        "anvil.processor_loader.load_processor_callable", lambda processor_name: mutate
    )
    spec = ProcessorSpec("mutate", metadata={"original": True})

    run_processors(specs=[spec], context=_context(tmp_path))

    assert spec.metadata == {"original": True}


def test_run_processors_isolates_top_level_context_data(monkeypatch, tmp_path):
    observed_states: list[str] = []

    def first(*, context, output, metadata):
        context.summary["state"] = "mutated"

    def second(*, context, output, metadata):
        observed_states.append(str(context.summary["state"]))

    processors = {"first": first, "second": second}
    monkeypatch.setattr(
        "anvil.processor_loader.load_processor_callable",
        lambda processor_name: processors[processor_name],
    )
    context = _context(tmp_path)

    run_processors(
        specs=[ProcessorSpec("first"), ProcessorSpec("second")], context=context
    )

    assert observed_states == ["completed_success"]
    assert context.summary == {"state": "completed_success"}


def test_processor_context_derives_selected_result_and_path(tmp_path):
    target_path = tmp_path / "targets" / "production.json"
    context = ProcessorRunContext(
        run_dir=tmp_path,
        summary_path=tmp_path / "summary.json",
        summary={},
        target_result_paths={"production": target_path},
        target_name="production",
        target_metadata={"team": "security"},
        target_results=({"target": "production", "entities": []},),
    )

    assert context.target_result == {"target": "production", "entities": []}
    assert context.target_result_path == target_path


def test_processor_context_rejects_inconsistent_selected_target(tmp_path):
    with pytest.raises(ValueError, match="exactly one matching"):
        ProcessorRunContext(
            run_dir=tmp_path,
            summary_path=tmp_path / "summary.json",
            summary={},
            target_result_paths={},
            target_name="production",
            target_results=({"target": "sandbox"},),
        )


def test_load_completed_run_context_reads_current_results_directory(tmp_path):
    run_dir = tmp_path / "results" / "smoke" / "2026-06-02T120000Z"
    target_dir = run_dir / "targets"
    target_dir.mkdir(parents=True)

    summary_path = run_dir / "summary.json"
    target_path = target_dir / "production.json"

    summary_path.write_text(
        json.dumps({"state": "completed_success"}), encoding="utf-8"
    )
    target_path.write_text(
        json.dumps({"target": "production", "entities": []}), encoding="utf-8"
    )

    context = load_completed_run_context(results_dir=run_dir)

    assert context.summary == {"state": "completed_success"}
    assert context.target_result_paths == {"production": target_path}
    assert context.target_results == ({"target": "production", "entities": []},)


def test_load_completed_run_context_allows_missing_summary(tmp_path):
    run_dir = tmp_path / "results" / "smoke" / "2026-06-02T120000Z"
    target_dir = run_dir / "targets"
    target_dir.mkdir(parents=True)
    target_path = target_dir / "production.json"
    target_path.write_text(
        json.dumps({"target": "production", "entities": []}), encoding="utf-8"
    )

    context = load_completed_run_context(results_dir=run_dir)

    assert context.summary == {}
    assert context.summary_path == run_dir / "summary.json"
    assert context.target_result_paths == {"production": target_path}


def test_html_report_load_records_scopes_to_context_target_name(tmp_path):
    context = ProcessorRunContext(
        run_dir=tmp_path,
        summary_path=tmp_path / "summary.json",
        summary={"state": "completed_success"},
        target_result_paths={},
        target_name="production",
        target_results=[
            {
                "target": "production",
                "entities": [
                    {
                        "id": "111111111111",
                        "name": "dev",
                        "type": "account",
                        "status": "success",
                        "tasks": [],
                    }
                ],
            },
            {
                "target": "sandbox",
                "entities": [
                    {
                        "id": "222222222222",
                        "name": "prod",
                        "type": "account",
                        "status": "success",
                        "tasks": [],
                    }
                ],
            },
        ],
    )

    records = html_report._load_records(context=context)

    assert [record["entity_id"] for record in records] == ["111111111111"]


def test_html_report_load_records_keeps_whole_run_context(tmp_path):
    context = ProcessorRunContext(
        run_dir=tmp_path,
        summary_path=tmp_path / "summary.json",
        summary={"state": "completed_success"},
        target_result_paths={},
        target_results=[
            {
                "target": "production",
                "entities": [
                    {
                        "id": "111111111111",
                        "name": "dev",
                        "type": "account",
                        "status": "success",
                        "tasks": [],
                    }
                ],
            },
            {
                "target": "sandbox",
                "entities": [
                    {
                        "id": "222222222222",
                        "name": "prod",
                        "type": "account",
                        "status": "success",
                        "tasks": [],
                    }
                ],
            },
        ],
    )

    records = html_report._load_records(context=context)

    assert [record["entity_id"] for record in records] == [
        "111111111111",
        "222222222222",
    ]


def test_html_report_includes_configured_tasks_and_counts_skips(tmp_path):
    context = ProcessorRunContext(
        run_dir=tmp_path,
        summary_path=tmp_path / "summary.json",
        summary={"state": "completed_success"},
        target_result_paths={},
        target_results=[
            {
                "target": "production",
                "tasks": [
                    {
                        "task_id": "inventory_before",
                        "task_name": "inventory",
                        "region": "us-east-1",
                        "status": "skipped",
                    }
                ],
                "entities": [],
            }
        ],
    )

    records = html_report._load_records(context=context)
    cards = html_report._summary_cards(records)

    assert records[0]["entity_type"] == "configured_target"
    assert records[0]["task_id"] == "inventory_before"
    assert records[0]["task_name"] == "inventory"
    assert next(card for card in cards if card["label"] == "Skipped")["value"] == 1
    assert next(card for card in cards if card["label"] == "Failed tasks")["value"] == 0


def test_sarif_report_includes_configured_task_identity(tmp_path):
    context = ProcessorRunContext(
        run_dir=tmp_path,
        summary_path=tmp_path / "summary.json",
        summary={"state": "completed_success"},
        target_result_paths={},
        target_results=[
            {
                "target": "production",
                "tasks": [
                    {
                        "task_id": "detect_public",
                        "task_name": "detect_resources",
                        "region": "us-east-1",
                        "result": {
                            "sarif_findings": [
                                {
                                    "rule": {"id": "ANVIL001"},
                                    "message": "Public resource",
                                    "locations": [{"uri": "aws://resource"}],
                                    "properties": {
                                        "target": "spoofed-target",
                                        "entity_type": "spoofed-type",
                                        "task_id": "spoofed-id",
                                        "task_name": "spoofed-name",
                                        "resource_name": "public-resource",
                                    },
                                }
                            ]
                        },
                    }
                ],
                "entities": [],
            }
        ],
    )

    results, _ = sarif_report._collect_sarif_results(context=context)

    properties = results[0]["properties"]
    assert properties["entity_type"] == "configured_target"
    assert properties["task_id"] == "detect_public"
    assert properties["task_name"] == "detect_resources"
    assert properties["target"] == "production"
    assert properties["resource_name"] == "public-resource"
