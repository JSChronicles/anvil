from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from anvil.processor_loader import ProcessorRunContext


DEFAULT_TITLE = "AWS Inactive Accounts Report"
DEFAULT_OUTPUT = "inactive-account-status-report.html"
REPORT_RECORD_TYPE = "inactive_account_report"

NEUTRAL_SCORE = 50
SCORE_KEYS = ("cost_score", "activity_score", "iam_usage_score", "resource_score")
STATUS_ORDER = ("ACTIVE", "POSSIBLY_INACTIVE", "LIKELY_INACTIVE")
STATUS_DESCRIPTIONS = {
    "ACTIVE": "Enough cost, resource, and usage signal to be considered in use.",
    "POSSIBLY_INACTIVE": (
        "Some signals suggest low use. Review purpose and confirm with owners "
        "before taking action."
    ),
    "LIKELY_INACTIVE": (
        "Strong inactivity signals. Candidate for review and possible quarantine."
    ),
}


def run(
    *, context: ProcessorRunContext, output: str | None, metadata: dict[str, object]
) -> dict[str, object]:
    """Write a self-contained HTML report of AWS inactive-account findings.

    The processor reads the `inactive_account_report` task result from each
    completed target and renders status summary cards, a score legend, and a
    filterable, sortable account table with expandable per-account details.

    Metadata:
        title: Optional report title. Defaults to `AWS Inactive Accounts Report`.

    Args:
        context: Completed run context provided by the Anvil processor runner.
        output: Optional output path. Defaults to
            `reports/inactive-account-status-report.html` under the run directory.
        metadata: Processor metadata containing optional report settings.

    Returns:
        A payload containing the written output path, rendered account count, and
        per-status counts.

    Raises:
        RuntimeError: If no `inactive_account_report` task result is found.
    """
    title = _metadata_string(metadata=metadata, key="title", default=DEFAULT_TITLE)
    rows, generated_at = _load_rows(context=context)
    output_path = Path(output) if output is not None else _default_output_path(context)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        _build_html(title=title, rows=rows, generated_at=generated_at), encoding="utf-8"
    )

    return {
        "output": str(output_path),
        "account_count": len(rows),
        "status_counts": _status_counts(rows),
    }


def _metadata_string(*, metadata: dict[str, object], key: str, default: str) -> str:
    value = metadata.get(key, default)
    if isinstance(value, str) and value.strip():
        return value.strip()

    return default


def _default_output_path(context: ProcessorRunContext) -> Path:
    return context.run_dir / "reports" / DEFAULT_OUTPUT


def _load_rows(*, context: ProcessorRunContext) -> tuple[list[dict[str, object]], str]:
    rows: list[dict[str, object]] = []
    generated_at = ""
    found_report = False

    for target_result in context.target_results:
        target_name = _string(target_result.get("target"))
        if context.target_name is not None and target_name != context.target_name:
            continue

        for report in _report_results(target_result=target_result):
            found_report = True
            generated_at = generated_at or _string(target_result.get("generated_at"))
            accounts = report.get("accounts", [])
            if not isinstance(accounts, list):
                continue
            for account in accounts:
                if isinstance(account, dict):
                    rows.append(_row(target=target_name, account=account))

    if not found_report:
        raise RuntimeError(
            "inactive_account_html_report requires an "
            f"'{REPORT_RECORD_TYPE}' task result in the target results"
        )

    return rows, generated_at


def _report_results(*, target_result: dict[str, object]) -> list[dict[str, object]]:
    tasks = target_result.get("tasks", [])
    if not isinstance(tasks, list):
        return []

    reports: list[dict[str, object]] = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        result = task.get("result")
        if isinstance(result, dict) and result.get("record_type") == REPORT_RECORD_TYPE:
            reports.append(result)

    return reports


def _row(*, target: str, account: dict[str, object]) -> dict[str, object]:
    scores = _mapping(account.get("scores"))
    signals = _mapping(account.get("signals"))
    cost = _mapping(signals.get("cost"))
    resources = _mapping(signals.get("resources"))
    ownership = _mapping(account.get("ownership_context"))
    resource_ownership = _mapping(ownership.get("resource_ownership"))
    account_tags = _mapping(ownership.get("account_tags"))
    account_owner_tags = _mapping(ownership.get("account_ownership_tags"))
    owner_counts = _mapping(resource_ownership.get("owner_counts"))
    contact = _mapping(ownership.get("operations_contact"))
    warnings = account.get("warnings", [])

    return {
        "target": target,
        "id": _string(account.get("account_id")),
        "name": _string(account.get("account_name")),
        "status": _string(account.get("status")),
        "score": _number(account.get("final_score")),
        "scores": {key: _number(scores.get(key)) for key in SCORE_KEYS},
        "avg_cost": _number(cost.get("avg_monthly_cost_3m")),
        "trend": _string(cost.get("cost_trend")),
        "resources": _number(resources.get("resource_count")),
        "env": _string(account_tags.get("Environment")),
        "operations_contact": _contact_label(contact),
        "account_owner": _string(account_owner_tags.get("Owner")),
        "resource_owners": sorted(str(owner) for owner in owner_counts),
        "recommendation": _string(account.get("recommendation")),
        "warnings": (
            [str(warning) for warning in warnings] if isinstance(warnings, list) else []
        ),
    }


def _contact_label(contact: dict[str, object]) -> str:
    """Format an Operations alternate contact as 'Name <email>'."""
    name = _string(contact.get("name"))
    email = _string(contact.get("email"))
    return " ".join(part for part in (name, f"<{email}>" if email else "") if part)


def _mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""


def _number(value: object) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return value


def _status_counts(rows: list[dict[str, object]]) -> dict[str, int]:
    counts = {status: 0 for status in STATUS_ORDER}
    for row in rows:
        status = str(row["status"])
        counts[status] = counts.get(status, 0) + 1

    return counts


def _score_bands(rows: list[dict[str, object]]) -> dict[str, str]:
    """Return the observed final-score range for each status in this run."""
    scores_by_status: dict[str, list[float]] = {}
    for row in rows:
        scores_by_status.setdefault(str(row["status"]), []).append(
            _number(row["score"])
        )

    return {
        status: f"{min(scores)}-{max(scores)}"
        for status, scores in scores_by_status.items()
    }


def _neutral_signal_note(rows: list[dict[str, object]]) -> str:
    """Explain activity/IAM scores when no account has real telemetry for them."""
    if not rows:
        return ""

    neutral = all(
        isinstance(row["scores"], dict)
        and row["scores"].get("activity_score") == NEUTRAL_SCORE
        and row["scores"].get("iam_usage_score") == NEUTRAL_SCORE
        for row in rows
    )
    if not neutral:
        return ""

    return (
        "Activity and IAM usage scores are neutral "
        f"({NEUTRAL_SCORE}) for every account, so no CloudTrail-based signal "
        "contributed. Statuses reflect cost and resource signals only."
    )


def _json_for_script(payload: object) -> str:
    return (
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )


def _build_html(*, title: str, rows: list[dict[str, object]], generated_at: str) -> str:
    bands = _score_bands(rows)
    payload = {
        "rows": rows,
        "generated_at": generated_at,
        "rendered_at": datetime.now(timezone.utc).isoformat(),
        "statuses": [
            {
                "status": status,
                "description": STATUS_DESCRIPTIONS[status],
                "band": bands.get(status, ""),
            }
            for status in STATUS_ORDER
        ],
        "note": _neutral_signal_note(rows),
        "multi_target": len({row["target"] for row in rows}) > 1,
    }

    return _HTML_TEMPLATE.replace("__TITLE__", html.escape(title)).replace(
        "__DATA__", _json_for_script(payload)
    )


_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root {
  color-scheme: light dark;
  --bg: #f6f7f9; --panel: #ffffff; --text: #1d232b; --muted: #667085;
  --border: #d8dde5; --accent: #255e9c;
  --active: #13795b; --active-bg: #e8f5ef;
  --possibly: #915c00; --possibly-bg: #fff4df;
  --likely: #b42318; --likely-bg: #fdeceb;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #12161c; --panel: #1b212a; --text: #e6e9ee; --muted: #98a2b3;
    --border: #2c3541; --accent: #6ea8e6;
    --active: #4cc9a0; --active-bg: #16322a;
    --possibly: #f0b44c; --possibly-bg: #37290f;
    --likely: #ff8a80; --likely-bg: #3a1a18;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 14px/1.45 Arial, Helvetica, sans-serif; }
header { background: #202a37; color: #fff; padding: 22px 28px; }
h1 { margin: 0 0 6px; font-size: 24px; }
header p { margin: 0; color: #d3dae5; }
main { padding: 22px 28px 36px; max-width: 1500px; margin: auto; }
.cards { display: grid; gap: 14px; margin-bottom: 14px;
  grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); }
.card { background: var(--panel); border: 1px solid var(--border);
  border-left: 5px solid var(--accent); border-radius: 6px; padding: 14px 16px;
  cursor: pointer; text-align: left; font: inherit; color: inherit; }
.card .label { font-size: 12px; text-transform: uppercase; color: var(--muted); }
.card .value { font-size: 34px; font-weight: 700; line-height: 1.1; }
.card .desc { font-size: 12px; color: var(--muted); margin-top: 6px; }
.card .band { font-size: 12px; margin-top: 4px; }
.card.ACTIVE { border-left-color: var(--active);
  background: linear-gradient(135deg, var(--panel), var(--active-bg)); }
.card.ACTIVE .value { color: var(--active); }
.card.POSSIBLY_INACTIVE { border-left-color: var(--possibly);
  background: linear-gradient(135deg, var(--panel), var(--possibly-bg)); }
.card.POSSIBLY_INACTIVE .value { color: var(--possibly); }
.card.LIKELY_INACTIVE { border-left-color: var(--likely);
  background: linear-gradient(135deg, var(--panel), var(--likely-bg)); }
.card.LIKELY_INACTIVE .value { color: var(--likely); }
.card.selected { outline: 2px solid var(--accent); }
.note { background: var(--panel); border: 1px solid var(--border); border-radius: 6px;
  padding: 10px 14px; margin-bottom: 14px; color: var(--muted); font-size: 13px; }
.note[hidden] { display: none; }
.note b { color: var(--text); }
.bar { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 10px; }
input, select { background: var(--panel); color: var(--text);
  border: 1px solid var(--border); border-radius: 6px; padding: 8px 10px; font: inherit; }
input { flex: 1; min-width: 220px; }
.count { color: var(--muted); margin: 8px 2px; font-size: 13px; }
.table-wrap { background: var(--panel); border: 1px solid var(--border);
  border-radius: 6px; overflow: auto; max-height: 70vh; }
table { border-collapse: collapse; width: 100%; }
th, td { padding: 8px 10px; border-bottom: 1px solid var(--border);
  text-align: left; white-space: nowrap; }
th { position: sticky; top: 0; background: var(--panel); cursor: pointer;
  font-size: 12px; text-transform: uppercase; color: var(--muted); user-select: none; }
.num { text-align: right; }
tr.row { cursor: pointer; }
tr.row:hover { background: var(--bg); }
.pill { display: inline-block; padding: 2px 9px; border-radius: 999px;
  font-size: 12px; font-weight: 700; border: 1px solid; }
.pill.ACTIVE { color: var(--active); background: var(--active-bg); border-color: var(--active); }
.pill.POSSIBLY_INACTIVE { color: var(--possibly); background: var(--possibly-bg); border-color: var(--possibly); }
.pill.LIKELY_INACTIVE { color: var(--likely); background: var(--likely-bg); border-color: var(--likely); }
.detail td { white-space: normal; background: var(--bg); }
.detail dl { display: grid; grid-template-columns: max-content 1fr; gap: 4px 14px; margin: 0; }
.detail dt { color: var(--muted); }
.detail dd { margin: 0; }
</style>
</head>
<body>
<header><h1>__TITLE__</h1><p id="subtitle"></p></header>
<main>
<div class="cards" id="cards"></div>
<div class="note" id="note" hidden></div>
<div class="bar">
  <input id="search" type="search" placeholder="Search name, account ID, owner, environment">
  <select id="status-filter" aria-label="Status"></select>
  <select id="env-filter" aria-label="Environment"></select>
</div>
<div class="count" id="count"></div>
<div class="table-wrap"><table><thead><tr id="head"></tr></thead><tbody id="body"></tbody></table></div>
</main>
<script id="report-data" type="application/json">__DATA__</script>
<script>
const DATA = JSON.parse(document.getElementById("report-data").textContent);
const ROWS = DATA.rows;
const STATUS_INFO = Object.fromEntries(DATA.statuses.map(entry => [entry.status, entry]));
const COLUMNS = [
  ["name", "Account"], ["id", "Account ID"], ["status", "Status"],
  ["score", "Score", true], ["avg_cost", "Avg $/mo (3m)", true], ["trend", "Cost trend"],
  ["resources", "Resources", true], ["env", "Env"], ["owner_text", "Operations contact"]
];
if (DATA.multi_target) COLUMNS.unshift(["target", "Target"]);
// Ownership comes from the account, never from individual resource tags.
ROWS.forEach(row => { row.owner_text = row.operations_contact || row.account_owner; });

const el = id => document.getElementById(id);
const esc = text => String(text).replace(/[&<>"']/g,
  character => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[character]));
const label = text => text.replace(/_/g, " ");
const money = value => "$" + Number(value).toLocaleString(undefined, {maximumFractionDigits: 0});

let sortKey = "score", sortDir = -1, statusFilter = "", envFilter = "";

const counts = {};
DATA.statuses.forEach(entry => { counts[entry.status] = 0; });
ROWS.forEach(row => { counts[row.status] = (counts[row.status] || 0) + 1; });

const generated = DATA.generated_at ? new Date(DATA.generated_at).toLocaleString() : "";
el("subtitle").textContent = ROWS.length + " accounts" + (generated ? " · generated " + generated : "");

el("cards").innerHTML =
  `<button class="card" data-status=""><div class="label">All accounts</div>
   <div class="value">${ROWS.length}</div><div class="desc">Total scanned</div></button>` +
  Object.keys(counts).map(status => {
    const info = STATUS_INFO[status] || {description: "", band: ""};
    return `<button class="card ${esc(status)}" data-status="${esc(status)}">
      <div class="label">${esc(label(status))}</div><div class="value">${counts[status]}</div>
      <div class="desc">${esc(info.description)}</div>
      ${info.band ? `<div class="band">Score ${esc(info.band)}</div>` : ""}</button>`;
  }).join("");

const noteParts = [];
if (DATA.note) noteParts.push("<b>Data caveat:</b> " + esc(DATA.note));
noteParts.push("<b>Scoring:</b> higher score = more likely inactive. Score ranges on each card are observed in this run.");
el("note").innerHTML = noteParts.join("<br>");
el("note").hidden = false;

el("status-filter").innerHTML = '<option value="">All statuses</option>' +
  Object.keys(counts).map(status => `<option value="${esc(status)}">${esc(label(status))}</option>`).join("");
const envs = [...new Set(ROWS.map(row => row.env || "(none)"))].sort();
el("env-filter").innerHTML = '<option value="">All environments</option>' +
  envs.map(env => `<option value="${esc(env)}">${esc(env)}</option>`).join("");
el("head").innerHTML = COLUMNS.map(
  column => `<th class="${column[2] ? "num" : ""}" data-key="${column[0]}">${column[1]}</th>`).join("");

function cell(row, key) {
  switch (key) {
    case "status": return `<span class="pill ${esc(row.status)}">${esc(label(row.status))}</span>`;
    case "avg_cost": return money(row.avg_cost);
    case "trend": return esc(label(row.trend));
    default: return esc(row[key]);
  }
}

function render() {
  const query = el("search").value.toLowerCase();
  const rows = ROWS.filter(row =>
    (!statusFilter || row.status === statusFilter) &&
    (!envFilter || (row.env || "(none)") === envFilter) &&
    (!query || [row.name, row.id, row.owner_text, row.account_owner, row.env].join(" ").toLowerCase().includes(query)));
  rows.sort((left, right) => {
    const leftValue = left[sortKey], rightValue = right[sortKey];
    return (leftValue > rightValue ? 1 : leftValue < rightValue ? -1 : 0) * sortDir;
  });
  el("count").textContent = rows.length + " of " + ROWS.length + " accounts";
  el("body").innerHTML = rows.map(row =>
    `<tr class="row" data-id="${esc(row.target + "|" + row.id)}">` +
    COLUMNS.map(column => `<td class="${column[2] ? "num" : ""}">${cell(row, column[0])}</td>`).join("") + "</tr>").join("");
  document.querySelectorAll("#cards .card").forEach(
    card => card.classList.toggle("selected", card.dataset.status === statusFilter));
}

el("cards").addEventListener("click", event => {
  const card = event.target.closest(".card");
  if (!card) return;
  statusFilter = card.dataset.status;
  el("status-filter").value = statusFilter;
  render();
});
el("status-filter").addEventListener("change", event => { statusFilter = event.target.value; render(); });
el("env-filter").addEventListener("change", event => { envFilter = event.target.value; render(); });
el("search").addEventListener("input", render);
el("head").addEventListener("click", event => {
  const key = event.target.dataset.key;
  if (!key) return;
  sortDir = sortKey === key ? -sortDir : -1;
  sortKey = key;
  render();
});
el("body").addEventListener("click", event => {
  const tableRow = event.target.closest("tr.row");
  if (!tableRow) return;
  const next = tableRow.nextElementSibling;
  if (next && next.classList.contains("detail")) { next.remove(); return; }
  const row = ROWS.find(candidate => candidate.target + "|" + candidate.id === tableRow.dataset.id);
  const info = STATUS_INFO[row.status] || {description: ""};
  const scores = Object.entries(row.scores).map(([name, score]) => label(name) + " " + score).join(" · ");
  const detail = document.createElement("tr");
  detail.className = "detail";
  detail.innerHTML = `<td colspan="${COLUMNS.length}"><dl>
    <dt>Meaning</dt><dd>${esc(info.description)}</dd>
    <dt>Scores</dt><dd>${esc(scores)} → final ${row.score}</dd>
    <dt>Operations contact</dt><dd>${esc(row.operations_contact || "—")}</dd>
    <dt>Account Owner tag</dt><dd>${esc(row.account_owner || "—")}</dd>
    <dt>Resource owner tags</dt><dd>${esc(row.resource_owners.join(", ") || "—")} (context only; does not identify the account owner)</dd>
    <dt>Recommendation</dt><dd>${esc(row.recommendation)}</dd>
    <dt>Warnings</dt><dd>${esc(row.warnings.join("; ") || "—")}</dd></dl></td>`;
  tableRow.after(detail);
});
render();
</script>
</body>
</html>
"""
