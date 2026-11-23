from __future__ import annotations

import html
import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

from anvil.results import TargetResult

if TYPE_CHECKING:
    from anvil.processor_loader import ProcessorRunContext


DEFAULT_TITLE = "Anvil Results Report"
DEFAULT_OUTPUT = "html-report.html"


def run(
    *, context: ProcessorRunContext, output: str | None, metadata: dict[str, object]
) -> dict[str, object]:
    """Write a self-contained HTML report for Anvil result records.

    The processor flattens completed Anvil target results into entity and task
    records, then writes an interactive HTML report with summary cards, filters,
    and expandable error/result details.

    Metadata:
        title: Optional report title. Defaults to `Anvil Results Report`.

    Args:
        context: Completed run context provided by the Anvil processor runner.
        output: Optional output path. Defaults to `reports/html-report.html`
            under the run directory.
        metadata: Processor metadata containing optional report settings.

    Returns:
        A payload containing the written output path and rendered record count.
    """
    title = _metadata_string(metadata=metadata, key="title", default=DEFAULT_TITLE)
    records = _load_records(context=context)
    output_path = Path(output) if output is not None else _default_output_path(context)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        _build_html(title=title, records=records, summary=context.summary),
        encoding="utf-8",
    )

    return {"output": str(output_path), "record_count": len(records)}


def _metadata_string(*, metadata: dict[str, object], key: str, default: str) -> str:
    value = metadata.get(key, default)
    if isinstance(value, str) and value.strip():
        return value.strip()

    return default


def _default_output_path(context: ProcessorRunContext) -> Path:
    return context.run_dir / "reports" / DEFAULT_OUTPUT


def _load_records(*, context: ProcessorRunContext) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    if context.target_result is not None:
        records.extend(_records_from_target_result(target_result=context.target_result))
    else:
        for target_result in context.target_results:
            if not _matches_context_target(
                context=context, target_result=target_result
            ):
                continue
            records.extend(_records_from_target_result(target_result=target_result))

    return records


def _matches_context_target(
    *, context: ProcessorRunContext, target_result: TargetResult | dict[str, object]
) -> bool:
    if context.target_name is None:
        return True

    if isinstance(target_result, TargetResult):
        return target_result.target_name == context.target_name

    target_name = _string_value(target_result.get("target"))
    return target_name == context.target_name


def _records_from_target_result(
    *, target_result: TargetResult | dict[str, object]
) -> list[dict[str, object]]:
    if isinstance(target_result, TargetResult):
        return _records_from_target_dict(target_result=target_result.to_dict())

    return _records_from_target_dict(target_result=target_result)


def _records_from_target_dict(
    *, target_result: dict[str, object]
) -> list[dict[str, object]]:
    target_name = _string_value(target_result.get("target"))
    entities = target_result.get("entities", [])
    if not isinstance(entities, list):
        return []

    records: list[dict[str, object]] = []
    configured_entity_record = {
        "target_type": "target",
        "target": target_name,
        "generated_at": target_result.get("generated_at"),
        "dry_run": target_result.get("dry_run"),
        "entity_id": None,
        "entity_name": None,
        "entity_type": "configured_target",
    }
    records.extend(
        _task_records(
            tasks=target_result.get("tasks", []), entity_record=configured_entity_record
        )
    )
    for entity_result in entities:
        if not isinstance(entity_result, dict):
            continue
        entity_result = cast(dict[str, object], entity_result)

        entity_record = {
            "target_type": "target",
            "target": target_name,
            "generated_at": target_result.get("generated_at"),
            "dry_run": target_result.get("dry_run"),
            "entity_id": entity_result.get("id"),
            "entity_name": entity_result.get("name"),
            "entity_type": entity_result.get("type"),
        }
        records.append(
            {
                **entity_record,
                "record_type": "entity",
                **_timed_status_record(entity_result),
                "error": entity_result.get("error"),
            }
        )

        records.extend(
            _task_records(
                tasks=entity_result.get("tasks", []), entity_record=entity_record
            )
        )

    return records


def _task_records(
    *, tasks: object, entity_record: dict[str, object]
) -> list[dict[str, object]]:
    if not isinstance(tasks, list):
        return []

    records: list[dict[str, object]] = []
    for task_result in tasks:
        if not isinstance(task_result, dict):
            continue
        task_result = cast(dict[str, object], task_result)
        records.append(
            {
                **entity_record,
                "record_type": "task",
                "task_id": task_result.get("task_id"),
                "task_name": task_result.get("task_name"),
                "region": task_result.get("region"),
                **_timed_status_record(task_result),
                "result": task_result.get("result"),
                "error": task_result.get("error"),
                "skip_reason": task_result.get("skip_reason"),
            }
        )
    return records


def _timed_status_record(record: dict[str, object]) -> dict[str, object]:
    return {
        "status": record.get("status"),
        "started_at": record.get("started_at"),
        "ended_at": record.get("ended_at"),
        "duration_seconds": record.get("duration_seconds"),
    }


def _string_value(value: object) -> str:
    if isinstance(value, str):
        return value

    return ""


def _build_html(
    *, title: str, records: list[dict[str, object]], summary: dict[str, object]
) -> str:
    payload = {"records": records, "summary": summary, "cards": _summary_cards(records)}
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
  --border: #d8dde5; --accent: #255e9c; --accent-bg: #eaf2fb; --code-bg: #f3f5f8;
  --success: #13795b; --success-bg: #e8f5ef;
  --error: #b42318; --error-bg: #fdeceb;
  --interrupted: #915c00; --interrupted-bg: #fff4df;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #12161c; --panel: #1b212a; --text: #e6e9ee; --muted: #98a2b3;
    --border: #2c3541; --accent: #6ea8e6; --accent-bg: #1c2b3d; --code-bg: #141a21;
    --success: #4cc9a0; --success-bg: #16322a;
    --error: #ff8a80; --error-bg: #3a1a18;
    --interrupted: #f0b44c; --interrupted-bg: #37290f;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 14px/1.45 Arial, Helvetica, sans-serif; }
header { background: #202a37; color: #fff; padding: 22px 28px; }
h1 { margin: 0 0 6px; font-size: 24px; font-weight: 700; }
.subtitle { color: #d3dae5; margin: 0; }
main { padding: 22px 28px 36px; max-width: 1600px; margin: auto; }
.cards { display: grid; gap: 14px; margin-bottom: 16px;
  grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); }
.card { background: var(--panel); border: 1px solid var(--border);
  border-left: 5px solid var(--accent); border-radius: 6px; padding: 14px 16px;
  text-align: left; font: inherit; color: inherit; cursor: pointer; }
.card:hover { border-color: var(--accent); }
.card:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.card.selected { outline: 2px solid var(--accent); }
.card-label { font-size: 12px; text-transform: uppercase; color: var(--muted); }
.card-value { font-size: 34px; font-weight: 700; line-height: 1.1; }
.card-mark { display: inline-block; margin-top: 6px; padding: 1px 8px; border-radius: 999px;
  font-size: 11px; font-weight: 700; color: var(--accent); background: var(--accent-bg);
  border: 1px solid var(--border); }
.card.success { border-left-color: var(--success);
  background: linear-gradient(135deg, var(--panel), var(--success-bg)); }
.card.success .card-value, .card.success .card-mark { color: var(--success); }
.card.error { border-left-color: var(--error);
  background: linear-gradient(135deg, var(--panel), var(--error-bg)); }
.card.error .card-value, .card.error .card-mark { color: var(--error); }
.card.interrupted { border-left-color: var(--interrupted);
  background: linear-gradient(135deg, var(--panel), var(--interrupted-bg)); }
.card.interrupted .card-value, .card.interrupted .card-mark { color: var(--interrupted); }
.filters { display: grid; gap: 12px; margin-bottom: 12px; background: var(--panel);
  border: 1px solid var(--border); border-radius: 6px; padding: 14px;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); }
label { display: grid; gap: 5px; color: var(--muted); font-size: 12px; }
select, input, button.clear { width: 100%; min-height: 36px; border: 1px solid var(--border);
  border-radius: 4px; padding: 6px 8px; background: var(--panel); color: var(--text); font: inherit; }
button.clear { cursor: pointer; align-self: end; }
.count { color: var(--muted); margin: 8px 2px; font-size: 13px; }
.table-wrap { background: var(--panel); border: 1px solid var(--border);
  border-radius: 6px; overflow: auto; max-height: 72vh; }
table { width: 100%; border-collapse: collapse; min-width: 1040px; }
th, td { border-bottom: 1px solid var(--border); padding: 9px 10px;
  text-align: left; vertical-align: top; }
th { position: sticky; top: 0; z-index: 1; background: var(--panel); color: var(--muted);
  font-size: 12px; text-transform: uppercase; cursor: pointer; user-select: none; white-space: nowrap; }
th[aria-sort="ascending"]::after { content: " \\25B2"; }
th[aria-sort="descending"]::after { content: " \\25BC"; }
tr:last-child td { border-bottom: 0; }
tbody tr:hover { background: var(--bg); }
.pill { display: inline-flex; align-items: center; min-height: 24px; border-radius: 999px;
  padding: 2px 9px; font-size: 12px; font-weight: 700; border: 1px solid var(--border);
  white-space: nowrap; color: var(--muted); background: var(--bg); }
.pill.success { color: var(--success); background: var(--success-bg); border-color: var(--success); }
.pill.error { color: var(--error); background: var(--error-bg); border-color: var(--error); }
.pill.interrupted { color: var(--interrupted); background: var(--interrupted-bg); border-color: var(--interrupted); }
details { max-width: 420px; }
summary { color: var(--accent); cursor: pointer; }
pre { max-height: 260px; overflow: auto; white-space: pre-wrap; word-break: break-word;
  background: var(--code-bg); border: 1px solid var(--border); border-radius: 4px; padding: 8px; }
.empty { padding: 24px; color: var(--muted); }
</style>
</head>
<body>
<header>
  <h1>__TITLE__</h1>
  <p class="subtitle" id="subtitle"></p>
</header>
<main>
  <section class="cards" id="cards"></section>
  <section class="filters" id="filters">
    <label>Search<input id="searchFilter" type="search" placeholder="Filter visible fields"></label>
    <label>Status<select id="statusFilter"></select></label>
    <label>Type<select id="typeFilter"></select></label>
    <label>Target<select id="targetFilter"></select></label>
    <label>Entity<select id="entityFilter"></select></label>
    <label>Region<select id="regionFilter"></select></label>
    <label>Task ID<select id="taskIdFilter"></select></label>
    <label>Task name<select id="taskNameFilter"></select></label>
    <button class="clear" id="clearFilters" type="button">Clear filters</button>
  </section>
  <div class="count" id="count"></div>
  <section class="table-wrap">
    <table>
      <thead>
        <tr id="head"></tr>
      </thead>
      <tbody id="recordsBody"></tbody>
    </table>
    <div class="empty" id="emptyState" hidden>No matching records.</div>
  </section>
</main>
<script id="report-data" type="application/json">__DATA__</script>
<script>
const data = JSON.parse(document.getElementById("report-data").textContent);
const records = data.records;
const controls = {
  status: document.getElementById("statusFilter"),
  record_type: document.getElementById("typeFilter"),
  target: document.getElementById("targetFilter"),
  entity: document.getElementById("entityFilter"),
  region: document.getElementById("regionFilter"),
  task_id: document.getElementById("taskIdFilter"),
  task_name: document.getElementById("taskNameFilter"),
  search: document.getElementById("searchFilter")
};
const fields = ["status", "record_type", "target", "entity", "region", "task_id", "task_name"];
const columns = [
  ["status", "Status"], ["record_type", "Type"], ["target", "Target"], ["entity", "Entity"],
  ["region", "Region"], ["task", "Task"], ["duration_seconds", "Duration"],
  ["error", "Error", false], ["result", "Result", false]
];
let activeCard = -1;
let sortKey = "";
let sortDir = 1;

function value(record, field) {
  if (field === "entity") return record.entity_name || record.entity_id || "";
  if (field === "task") return [record.task_id, record.task_name].filter(Boolean).join(" / ");
  return record[field] || "";
}

function populateSelect(select, values, label) {
  select.innerHTML = "";
  const all = document.createElement("option");
  all.value = "";
  all.textContent = `All ${label}`;
  select.appendChild(all);
  values.forEach((item) => {
    const option = document.createElement("option");
    option.value = item;
    option.textContent = item;
    select.appendChild(option);
  });
}

function sortedValues(field) {
  return [...new Set(records.map((record) => value(record, field)).filter(Boolean))]
    .sort((left, right) => left.localeCompare(right));
}

function cardMatches(record, card) {
  const filter = card.filter || {};
  return Object.entries(filter).every(([field, expected]) => {
    const actual = value(record, field);
    return Array.isArray(expected) ? expected.includes(actual) : actual === expected;
  });
}

function renderCards(cards) {
  const root = document.getElementById("cards");
  root.innerHTML = "";
  cards.forEach((card, index) => {
    const element = document.createElement("button");
    element.type = "button";
    element.className = `card ${card.tone || ""}`.trim();
    element.dataset.index = index;
    element.title = "Click to filter; click again to clear";
    element.innerHTML = '<div class="card-label"></div><div class="card-value"></div><span class="card-mark"></span>';
    element.querySelector(".card-label").textContent = card.label;
    element.querySelector(".card-value").textContent = card.value;
    element.querySelector(".card-mark").textContent = card.mark;
    element.addEventListener("click", () => {
      const isAll = !card.filter || Object.keys(card.filter).length === 0;
      activeCard = isAll || activeCard === index ? -1 : index;
      renderTable();
    });
    root.appendChild(element);
  });
}

function recordMatches(record) {
  if (activeCard >= 0 && !cardMatches(record, data.cards[activeCard])) return false;
  for (const field of fields) {
    const expected = controls[field].value;
    if (expected && value(record, field) !== expected) return false;
  }
  const query = controls.search.value.trim().toLowerCase();
  if (!query) return true;
  return [record.status, record.record_type, record.target, record.entity_id, record.entity_name,
    record.entity_type, record.region, record.task_id, record.task_name, record.error]
    .some((item) => String(item || "").toLowerCase().includes(query));
}

function statusClass(status) {
  return ["success", "error", "interrupted"].includes(status) ? status : "unknown";
}

function detailCell(label, content) {
  if (content === null || content === undefined || content === "") return null;
  const details = document.createElement("details");
  const summary = document.createElement("summary");
  summary.textContent = label;
  const pre = document.createElement("pre");
  pre.textContent = typeof content === "string" ? content : JSON.stringify(content, null, 2);
  details.append(summary, pre);
  return details;
}

function compare(left, right) {
  const a = sortKey === "duration_seconds" ? Number(left[sortKey] ?? -1) : String(value(left, sortKey));
  const b = sortKey === "duration_seconds" ? Number(right[sortKey] ?? -1) : String(value(right, sortKey));
  return (a > b ? 1 : a < b ? -1 : 0) * sortDir;
}

function renderHead() {
  const head = document.getElementById("head");
  head.innerHTML = "";
  columns.forEach(([key, label, sortable]) => {
    const th = document.createElement("th");
    th.textContent = label;
    if (sortable !== false) {
      th.addEventListener("click", () => {
        sortDir = sortKey === key ? -sortDir : 1;
        sortKey = key;
        renderTable();
      });
      if (sortKey === key) th.setAttribute("aria-sort", sortDir > 0 ? "ascending" : "descending");
    }
    head.appendChild(th);
  });
}

function renderTable() {
  const body = document.getElementById("recordsBody");
  body.innerHTML = "";
  const matches = records.filter(recordMatches);
  if (sortKey) matches.sort(compare);
  document.getElementById("emptyState").hidden = matches.length > 0;
  document.querySelectorAll("#cards .card").forEach((card) =>
    card.classList.toggle("selected", Number(card.dataset.index) === activeCard));
  renderHead();

  matches.forEach((record) => {
    const row = document.createElement("tr");
    for (let i = 0; i < 9; i += 1) row.appendChild(document.createElement("td"));
    const cells = row.children;
    const pill = document.createElement("span");
    pill.className = `pill ${statusClass(record.status)}`;
    pill.textContent = record.status || "unknown";
    cells[0].appendChild(pill);
    cells[1].textContent = record.record_type || "";
    cells[2].textContent = record.target || "";
    cells[3].textContent = [record.entity_name, record.entity_id].filter(Boolean).join(" ");
    cells[4].textContent = record.region || "";
    cells[5].textContent = value(record, "task");
    cells[6].textContent = record.duration_seconds === undefined || record.duration_seconds === null
      ? "" : `${Number(record.duration_seconds).toFixed(1)}s`;
    const error = detailCell("error", record.error);
    const result = detailCell("result", record.result);
    if (error) cells[7].appendChild(error);
    if (result) cells[8].appendChild(result);
    body.appendChild(row);
  });
  const label = activeCard >= 0 ? ` · card filter: ${data.cards[activeCard].label}` : "";
  document.getElementById("count").textContent = `${matches.length} of ${records.length} records shown${label}`;
  document.getElementById("subtitle").textContent =
    `${records.length} records` + (data.summary.state ? ` · run ${data.summary.state}` : "");
}

populateSelect(controls.status, sortedValues("status"), "statuses");
populateSelect(controls.record_type, sortedValues("record_type"), "types");
populateSelect(controls.target, sortedValues("target"), "targets");
populateSelect(controls.entity, sortedValues("entity"), "entities");
populateSelect(controls.region, sortedValues("region"), "regions");
populateSelect(controls.task_id, sortedValues("task_id"), "task IDs");
populateSelect(controls.task_name, sortedValues("task_name"), "task names");
renderCards(data.cards);
Object.values(controls).forEach((control) => control.addEventListener("input", renderTable));
document.getElementById("clearFilters").addEventListener("click", () => {
  Object.values(controls).forEach((control) => { control.value = ""; });
  activeCard = -1;
  renderTable();
});
renderTable();
</script>
</body>
</html>
"""


def _summary_cards(records: list[dict[str, object]]) -> list[dict[str, object]]:
    success_count = _count_status(records=records, status="success")
    error_count = _count_status(records=records, status="error")
    interrupted_count = _count_status(records=records, status="interrupted")
    unsuccessful_count = sum(1 for record in records if _is_unsuccessful(record))
    skipped_count = _count_status(records=records, status="skipped")
    failed_entities = sum(
        1
        for record in records
        if record.get("record_type") == "entity" and _is_unsuccessful(record)
    )
    failed_tasks = sum(
        1
        for record in records
        if record.get("record_type") == "task" and _is_unsuccessful(record)
    )

    # Each card's optional "filter" maps a record field to the value(s) it
    # selects; the report applies it when the card is clicked.
    unsuccessful = ["error", "interrupted"]
    return [
        {"label": "Records", "value": len(records), "mark": "ALL", "filter": {}},
        {
            "label": "Succeeded",
            "value": success_count,
            "tone": "success",
            "mark": "OK",
            "filter": {"status": "success"},
        },
        {
            "label": "Unsuccessful",
            "value": unsuccessful_count,
            "tone": "error",
            "mark": "ATTN",
            "filter": {"status": unsuccessful},
        },
        {
            "label": "Errors",
            "value": error_count,
            "tone": "error",
            "mark": "ERR",
            "filter": {"status": "error"},
        },
        {
            "label": "Interrupted",
            "value": interrupted_count,
            "tone": "interrupted",
            "mark": "INT",
            "filter": {"status": "interrupted"},
        },
        {
            "label": "Skipped",
            "value": skipped_count,
            "mark": "SKIP",
            "filter": {"status": "skipped"},
        },
        {
            "label": "Failed entities",
            "value": failed_entities,
            "tone": "error",
            "mark": "ENT",
            "filter": {"record_type": "entity", "status": unsuccessful},
        },
        {
            "label": "Failed tasks",
            "value": failed_tasks,
            "tone": "error",
            "mark": "TASK",
            "filter": {"record_type": "task", "status": unsuccessful},
        },
    ]


def _count_status(*, records: list[dict[str, object]], status: str) -> int:
    return sum(1 for record in records if record.get("status") == status)


def _is_unsuccessful(record: dict[str, object]) -> bool:
    status = record.get("status")
    return isinstance(status, str) and status.lower() in {"error", "interrupted"}


def _json_for_script(value: object) -> str:
    return (
        json.dumps(value, separators=(",", ":"))
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )
