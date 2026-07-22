from __future__ import annotations

import html
import json
from pathlib import Path
from typing import TYPE_CHECKING

from anvil.descriptors import ConfigBranch
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
        records.extend(
            _records_from_target_result(
                target_result=context.target_result, config_branch=context.config_branch
            )
        )
    else:
        for target_result in context.target_results:
            if not _matches_context_target(
                context=context, target_result=target_result
            ):
                continue
            records.extend(
                _records_from_target_result(
                    target_result=target_result, config_branch=context.config_branch
                )
            )

    return records


def _matches_context_target(
    *, context: ProcessorRunContext, target_result: TargetResult | dict[str, object]
) -> bool:
    if context.target_name is None:
        return True

    if isinstance(target_result, TargetResult):
        return target_result.target_name == context.target_name

    target_type = _target_type(context.config_branch)
    target_name = _string_value(target_result.get(target_type)) or _string_value(
        target_result.get("target")
    )
    return target_name == context.target_name


def _records_from_target_result(
    *, target_result: TargetResult | dict[str, object], config_branch: ConfigBranch
) -> list[dict[str, object]]:
    if isinstance(target_result, TargetResult):
        return _records_from_target_dict(
            target_result=target_result.to_dict(), config_branch=config_branch
        )

    return _records_from_target_dict(
        target_result=target_result, config_branch=config_branch
    )


def _records_from_target_dict(
    *, target_result: dict[str, object], config_branch: ConfigBranch
) -> list[dict[str, object]]:
    target_type = _target_type(config_branch)
    target_name = _string_value(target_result.get(target_type)) or _string_value(
        target_result.get("target")
    )
    entities = target_result.get("entities", [])
    if not isinstance(entities, list):
        return []

    records: list[dict[str, object]] = []
    for entity_result in entities:
        if not isinstance(entity_result, dict):
            continue

        entity_record = {
            "target_type": target_type,
            target_type: target_name,
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

        tasks = entity_result.get("tasks", [])
        if not isinstance(tasks, list):
            continue

        for task_result in tasks:
            if not isinstance(task_result, dict):
                continue
            records.append(
                {
                    **entity_record,
                    "record_type": "task",
                    "task": task_result.get("task"),
                    "region": task_result.get("region"),
                    **_timed_status_record(task_result),
                    "result": task_result.get("result"),
                    "error": task_result.get("error"),
                }
            )

    return records


def _target_type(config_branch: ConfigBranch) -> str:
    if config_branch is not ConfigBranch.TARGETS:
        raise ValueError(f"Unsupported config branch: {config_branch}")
    return "target"


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
    escaped_title = html.escape(title)
    data_json = _json_for_script(payload)

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escaped_title}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --text: #1d232b;
      --muted: #667085;
      --border: #d8dde5;
      --success: #13795b;
      --success-bg: #e8f5ef;
      --success-border: #b6dfcc;
      --error: #b42318;
      --error-bg: #fdeceb;
      --error-border: #f3b9b5;
      --interrupted: #915c00;
      --interrupted-bg: #fff4df;
      --interrupted-border: #f3ce8d;
      --accent: #255e9c;
      --accent-bg: #eaf2fb;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: Arial, Helvetica, sans-serif;
      font-size: 14px;
      line-height: 1.45;
    }}
    header {{
      background: #202a37;
      color: #ffffff;
      padding: 24px 28px;
    }}
    h1 {{
      margin: 0 0 8px;
      font-size: 26px;
      font-weight: 700;
      letter-spacing: 0;
    }}
    .subtitle {{ color: #d3dae5; margin: 0; }}
    main {{ padding: 24px 28px 36px; }}
    .cards {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
      gap: 14px;
      margin-bottom: 22px;
    }}
    .card {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-left: 5px solid var(--accent);
      border-radius: 6px;
      box-shadow: 0 10px 24px rgba(32, 42, 55, 0.08);
      padding: 16px;
      min-height: 112px;
      position: relative;
      display: grid;
      grid-template-columns: 1fr auto;
      grid-template-areas:
        "value mark"
        "label mark";
      gap: 6px 12px;
      align-items: center;
    }}
    .card.success {{ border-left-color: var(--success); background: linear-gradient(135deg, #ffffff 0%, var(--success-bg) 100%); }}
    .card.error {{ border-left-color: var(--error); background: linear-gradient(135deg, #ffffff 0%, var(--error-bg) 100%); }}
    .card.interrupted {{ border-left-color: var(--interrupted); background: linear-gradient(135deg, #ffffff 0%, var(--interrupted-bg) 100%); }}
    .card-label {{
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0;
      grid-area: label;
      align-self: start;
    }}
    .card-value {{
      font-size: 36px;
      font-weight: 700;
      line-height: 1;
      grid-area: value;
      align-self: end;
    }}
    .card.success .card-value {{ color: var(--success); }}
    .card.error .card-value {{ color: var(--error); }}
    .card.interrupted .card-value {{ color: var(--interrupted); }}
    .card-mark {{
      grid-area: mark;
      align-self: start;
      justify-self: end;
      min-width: 42px;
      min-height: 28px;
      border-radius: 999px;
      border: 1px solid var(--border);
      display: inline-flex;
      align-items: center;
      justify-content: center;
      padding: 3px 9px;
      color: var(--accent);
      background: var(--accent-bg);
      font-size: 12px;
      font-weight: 700;
    }}
    .card.success .card-mark {{
      border-color: var(--success-border);
      color: var(--success);
      background: var(--success-bg);
    }}
    .card.error .card-mark {{
      border-color: var(--error-border);
      color: var(--error);
      background: var(--error-bg);
    }}
    .card.interrupted .card-mark {{
      border-color: var(--interrupted-border);
      color: var(--interrupted);
      background: var(--interrupted-bg);
    }}
    .filters {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 12px;
      margin-bottom: 18px;
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 14px;
    }}
    label {{ display: grid; gap: 5px; color: var(--muted); font-size: 12px; }}
    select, input {{
      width: 100%;
      min-height: 36px;
      border: 1px solid var(--border);
      border-radius: 4px;
      padding: 6px 8px;
      background: #ffffff;
      color: var(--text);
      font: inherit;
    }}
    .table-wrap {{
      overflow-x: auto;
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 6px;
    }}
    table {{ width: 100%; border-collapse: collapse; min-width: 1040px; }}
    th, td {{
      border-bottom: 1px solid var(--border);
      padding: 9px 10px;
      text-align: left;
      vertical-align: top;
    }}
    th {{
      background: #eef1f5;
      color: #344054;
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0;
      position: sticky;
      top: 0;
      z-index: 1;
    }}
    tr:last-child td {{ border-bottom: 0; }}
    .pill {{
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      border-radius: 999px;
      padding: 2px 8px;
      font-size: 12px;
      font-weight: 700;
      border: 1px solid transparent;
      white-space: nowrap;
    }}
    .pill.success {{
      background: var(--success-bg);
      border-color: var(--success-border);
      color: var(--success);
    }}
    .pill.error {{
      background: var(--error-bg);
      border-color: var(--error-border);
      color: var(--error);
    }}
    .pill.interrupted {{
      background: var(--interrupted-bg);
      border-color: var(--interrupted-border);
      color: var(--interrupted);
    }}
    .pill.unknown {{
      background: #eef1f5;
      border-color: var(--border);
      color: var(--muted);
    }}
    details {{ max-width: 420px; }}
    summary {{ color: var(--accent); cursor: pointer; }}
    pre {{
      max-height: 260px;
      overflow: auto;
      white-space: pre-wrap;
      word-break: break-word;
      background: #f3f5f8;
      border: 1px solid var(--border);
      border-radius: 4px;
      padding: 8px;
    }}
    .empty {{ padding: 24px; color: var(--muted); }}
  </style>
</head>
<body>
  <header>
    <h1>{escaped_title}</h1>
    <p class="subtitle" id="subtitle"></p>
  </header>
  <main>
    <section class="cards" id="cards"></section>
    <section class="filters" id="filters">
      <label>Status<select id="statusFilter"></select></label>
      <label>Type<select id="typeFilter"></select></label>
      <label>Target<select id="targetFilter"></select></label>
      <label>Entity<select id="entityFilter"></select></label>
      <label>Region<select id="regionFilter"></select></label>
      <label>Task<select id="taskFilter"></select></label>
      <label>Search<input id="searchFilter" type="search" placeholder="Filter visible fields"></label>
    </section>
    <section class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Status</th>
            <th>Type</th>
            <th>Target</th>
            <th>Entity</th>
            <th>Region</th>
            <th>Task</th>
            <th>Duration</th>
            <th>Error</th>
            <th>Result</th>
          </tr>
        </thead>
        <tbody id="recordsBody"></tbody>
      </table>
      <div class="empty" id="emptyState" hidden>No matching records.</div>
    </section>
  </main>
  <script id="report-data" type="application/json">{data_json}</script>
  <script>
    const data = JSON.parse(document.getElementById("report-data").textContent);
    const records = data.records;
    const controls = {{
      status: document.getElementById("statusFilter"),
      record_type: document.getElementById("typeFilter"),
      target: document.getElementById("targetFilter"),
      entity: document.getElementById("entityFilter"),
      region: document.getElementById("regionFilter"),
      task: document.getElementById("taskFilter"),
      search: document.getElementById("searchFilter")
    }};
    const fields = ["status", "record_type", "target", "entity", "region", "task"];

    function value(record, field) {{
      if (field === "entity") {{
        return record.entity_name || record.entity_id || "";
      }}
      return record[field] || "";
    }}

    function populateSelect(select, values, label) {{
      select.innerHTML = "";
      const all = document.createElement("option");
      all.value = "";
      all.textContent = `All ${{label}}`;
      select.appendChild(all);
      values.forEach((item) => {{
        const option = document.createElement("option");
        option.value = item;
        option.textContent = item;
        select.appendChild(option);
      }});
    }}

    function sortedValues(field) {{
      return [...new Set(records.map((record) => value(record, field)).filter(Boolean))]
        .sort((left, right) => left.localeCompare(right));
    }}

    function renderCards(cards) {{
      const cardsRoot = document.getElementById("cards");
      cardsRoot.innerHTML = "";
      cards.forEach((card) => {{
        const element = document.createElement("article");
        element.className = `card ${{card.tone || ""}}`.trim();
        element.innerHTML = `<div class="card-value"></div><div class="card-label"></div><div class="card-mark"></div>`;
        element.querySelector(".card-label").textContent = card.label;
        element.querySelector(".card-value").textContent = card.value;
        element.querySelector(".card-mark").textContent = card.mark;
        cardsRoot.appendChild(element);
      }});
    }}

    function recordMatches(record) {{
      for (const field of fields) {{
        const expected = controls[field].value;
        if (expected && value(record, field) !== expected) {{
          return false;
        }}
      }}
      const query = controls.search.value.trim().toLowerCase();
      if (!query) {{
        return true;
      }}
      return [
        record.status,
        record.record_type,
        record.target,
        record.entity_id,
        record.entity_name,
        record.entity_type,
        record.region,
        record.task,
        record.error
      ].some((item) => String(item || "").toLowerCase().includes(query));
    }}

    function statusClass(status) {{
      if (status === "success" || status === "error" || status === "interrupted") {{
        return status;
      }}
      return "unknown";
    }}

    function detailCell(label, value) {{
      if (value === null || value === undefined || value === "") {{
        return "";
      }}
      const json = typeof value === "string" ? value : JSON.stringify(value, null, 2);
      return `<details><summary>${{label}}</summary><pre></pre></details>`;
    }}

    function setDetailPre(cell, value) {{
      const pre = cell.querySelector("pre");
      if (pre) {{
        pre.textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2);
      }}
    }}

    function renderTable() {{
      const body = document.getElementById("recordsBody");
      const empty = document.getElementById("emptyState");
      body.innerHTML = "";
      const matches = records.filter(recordMatches);
      empty.hidden = matches.length > 0;

      matches.forEach((record) => {{
        const row = document.createElement("tr");
        row.innerHTML = `
          <td><span class="pill ${{statusClass(record.status)}}"></span></td>
          <td></td>
          <td></td>
          <td></td>
          <td></td>
          <td></td>
          <td></td>
          <td>${{detailCell("error", record.error)}}</td>
          <td>${{detailCell("result", record.result)}}</td>
        `;
        const cells = row.querySelectorAll("td");
        cells[0].querySelector(".pill").textContent = record.status || "unknown";
        cells[1].textContent = record.record_type || "";
        cells[2].textContent = record.target || "";
        cells[3].textContent = [record.entity_name, record.entity_id].filter(Boolean).join(" ");
        cells[4].textContent = record.region || "";
        cells[5].textContent = record.task || "";
        cells[6].textContent = record.duration_seconds === undefined || record.duration_seconds === null
          ? ""
          : `${{record.duration_seconds}}s`;
        setDetailPre(cells[7], record.error);
        setDetailPre(cells[8], record.result);
        body.appendChild(row);
      }});
      document.getElementById("subtitle").textContent =
        `${{matches.length}} of ${{records.length}} records shown`;
    }}

    populateSelect(controls.status, sortedValues("status"), "statuses");
    populateSelect(controls.record_type, sortedValues("record_type"), "types");
    populateSelect(controls.target, sortedValues("target"), "targets");
    populateSelect(controls.entity, sortedValues("entity"), "entities");
    populateSelect(controls.region, sortedValues("region"), "regions");
    populateSelect(controls.task, sortedValues("task"), "tasks");
    renderCards(data.cards);
    Object.values(controls).forEach((control) => control.addEventListener("input", renderTable));
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

    return [
        {"label": "Records", "value": len(records), "mark": "ALL"},
        {"label": "Succeeded", "value": success_count, "tone": "success", "mark": "OK"},
        {
            "label": "Unsuccessful",
            "value": unsuccessful_count,
            "tone": "error",
            "mark": "ATTN",
        },
        {"label": "Errors", "value": error_count, "tone": "error", "mark": "ERR"},
        {
            "label": "Interrupted",
            "value": interrupted_count,
            "tone": "interrupted",
            "mark": "INT",
        },
        {
            "label": "Failed entities",
            "value": failed_entities,
            "tone": "error",
            "mark": "ENT",
        },
        {
            "label": "Failed tasks",
            "value": failed_tasks,
            "tone": "error",
            "mark": "TASK",
        },
    ]


def _count_status(*, records: list[dict[str, object]], status: str) -> int:
    return sum(1 for record in records if record.get("status") == status)


def _is_unsuccessful(record: dict[str, object]) -> bool:
    status = record.get("status")
    return isinstance(status, str) and status.lower() != "success"


def _json_for_script(value: object) -> str:
    return (
        json.dumps(value, separators=(",", ":"))
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )
