from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from anvil.processor_loader import ProcessorRunContext
from anvil.processors import inactive_account_html_report


def _account(account_id: str, name: str, status: str, score: int) -> dict[str, object]:
    return {
        "account_id": account_id,
        "account_name": name,
        "status": status,
        "final_score": score,
        "scores": {
            "cost_score": 0,
            "activity_score": 50,
            "iam_usage_score": 50,
            "resource_score": 0,
        },
        "signals": {
            "cost": {"avg_monthly_cost_3m": 12.5, "cost_trend": "at_average"},
            "resources": {"resource_count": 3},
        },
        "ownership_context": {
            "account_ownership_tags": {"Owner": "team@example.com"},
            "account_tags": {"Environment": "dev"},
            "resource_ownership": {"owner_counts": {"jane@example.com": 1}},
        },
        "recommendation": "No action recommended.",
        "warnings": ["No signal result was received"],
    }


def _context(tmp_path: Path, accounts: list[dict[str, object]]) -> ProcessorRunContext:
    return ProcessorRunContext(
        run_dir=tmp_path,
        summary_path=tmp_path / "summary.json",
        summary={"state": "completed_success"},
        target_result_paths={},
        target_results=[
            {
                "target": "production",
                "generated_at": "2026-11-17T02:25:11+00:00",
                "tasks": [
                    {
                        "task_id": "inactive_account_report",
                        "result": {
                            "record_type": "inactive_account_report",
                            "accounts": accounts,
                        },
                    }
                ],
                "entities": [],
            }
        ],
    )


def _embedded_payload(html_text: str) -> dict[str, object]:
    match = re.search(
        r'<script id="report-data" type="application/json">(.*?)</script>',
        html_text,
        re.DOTALL,
    )
    assert match is not None
    return json.loads(match.group(1))


def test_writes_report_with_status_counts(tmp_path):
    accounts = [
        _account("111111111111", "alpha", "ACTIVE", 30),
        _account("222222222222", "beta", "POSSIBLY_INACTIVE", 50),
    ]

    result = inactive_account_html_report.run(
        context=_context(tmp_path, accounts), output=None, metadata={}
    )

    output = tmp_path / "reports" / inactive_account_html_report.DEFAULT_OUTPUT
    assert result["output"] == str(output)
    assert result["account_count"] == 2
    assert result["status_counts"] == {
        "ACTIVE": 1,
        "POSSIBLY_INACTIVE": 1,
        "LIKELY_INACTIVE": 0,
    }
    payload = _embedded_payload(output.read_text(encoding="utf-8"))
    assert [row["name"] for row in payload["rows"]] == ["alpha", "beta"]
    assert payload["note"]  # activity/IAM are neutral for every account


def test_score_bands_are_observed_from_rows():
    rows = [
        {"status": "ACTIVE", "score": 30},
        {"status": "ACTIVE", "score": 44},
        {"status": "POSSIBLY_INACTIVE", "score": 50},
    ]

    assert inactive_account_html_report._score_bands(rows) == {
        "ACTIVE": "30-44",
        "POSSIBLY_INACTIVE": "50-50",
    }


def test_escapes_title_and_script_breakout(tmp_path):
    accounts = [_account("111111111111", "</script><b>x", "ACTIVE", 30)]

    inactive_account_html_report.run(
        context=_context(tmp_path, accounts),
        output=str(tmp_path / "out.html"),
        metadata={"title": "<i>Title</i>"},
    )

    text = (tmp_path / "out.html").read_text(encoding="utf-8")
    assert "<title>&lt;i&gt;Title&lt;/i&gt;</title>" in text
    assert text.count("</script>") == 2  # data block + app script only
    assert _embedded_payload(text)["rows"][0]["name"] == "</script><b>x"


def test_requires_report_task_result(tmp_path):
    context = ProcessorRunContext(
        run_dir=tmp_path,
        summary_path=tmp_path / "summary.json",
        summary={},
        target_result_paths={},
        target_results=[{"target": "production", "tasks": [], "entities": []}],
    )

    with pytest.raises(RuntimeError, match="inactive_account_report"):
        inactive_account_html_report.run(context=context, output=None, metadata={})
