from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from anvil.descriptors import ConfigBranch
from anvil.results import AccountResult, TargetResult, TaskResult


JSONL_FILENAME = "results.jsonl"
DEFAULT_TABLE_FIELDS = [
    "record_type",
    "status",
    "target",
    "account_id",
    "account_alias",
    "region",
    "task",
    "error",
]
FIELD_HEADERS = {"record_type": "type", "account_alias": "alias"}
AVAILABLE_FIELDS = [
    "account_alias",
    "account_group",
    "account_id",
    "dry_run",
    "duration_seconds",
    "ended_at",
    "error",
    "generated_at",
    "organization",
    "record_type",
    "region",
    "result",
    "started_at",
    "status",
    "target",
    "target_type",
    "task",
]


@dataclass(frozen=True, slots=True)
class ResultFilters:
    status: str | None = None
    organization: str | None = None
    account: str | None = None
    region: str | None = None
    task: str | None = None


def jsonl_path_for_run(*, run_dir: Path) -> Path:
    """Return the flattened JSONL path for a run directory."""
    return run_dir / JSONL_FILENAME


def build_jsonl_records_for_target(
    target_result: TargetResult,
) -> list[dict[str, object]]:
    """Build flattened account and task records for a target result."""
    target_type = _target_type(target_result.config_branch)
    records: list[dict[str, object]] = []

    for account_result in target_result.account_results:
        account_record = _base_account_record(
            target_result=target_result,
            target_type=target_type,
            account_result=account_result,
        )
        records.append(
            {
                **account_record,
                "record_type": "account",
                **_timed_status_record(account_result),
                "error": account_result.error,
            }
        )

        for task_result in account_result.tasks:
            records.append(
                {
                    **account_record,
                    "record_type": "task",
                    "task": task_result.task_name,
                    "region": task_result.region,
                    **_timed_status_record(task_result),
                    "result": task_result.result,
                    "error": task_result.error,
                }
            )

    return records


def write_jsonl_records(*, path: Path, target_results: list[TargetResult]) -> int:
    """Write flattened result records and return the number of records written."""
    record_count = 0
    with path.open("w", encoding="utf-8") as handle:
        for target_result in target_results:
            for record in build_jsonl_records_for_target(target_result):
                handle.write(json.dumps(record, separators=(",", ":")))
                handle.write("\n")
                record_count += 1

    return record_count


def load_result_records(
    *, results_dir: Path, files: list[Path] | None
) -> list[dict[str, object]]:
    """Load result records from explicit files or the default results directory."""
    paths = files if files else sorted(results_dir.glob(f"**/{JSONL_FILENAME}"))
    records: list[dict[str, object]] = []

    for path in paths:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                stripped = line.strip()
                if not stripped:
                    continue

                try:
                    payload = json.loads(stripped)
                except json.JSONDecodeError as error:
                    raise ValueError(
                        f"Invalid JSONL in {path} on line {line_number}: {error.msg}"
                    ) from error

                if not isinstance(payload, dict):
                    message = (
                        f"Invalid JSONL in {path} on line {line_number}: "
                        "expected object"
                    )
                    raise ValueError(message)
                records.append(payload)

    return records


def filter_records(
    records: list[dict[str, object]], *, filters: ResultFilters
) -> list[dict[str, object]]:
    """Return records matching all supplied filters."""
    normalized_status = _normalize_status(filters.status)

    return [
        record
        for record in records
        if _matches(record, "status", normalized_status)
        and _matches(record, "target", filters.organization)
        and _matches_account(record, filters.account)
        and _matches(record, "region", filters.region)
        and _matches(record, "task", filters.task)
    ]


def failure_records(records: list[dict[str, object]]) -> list[dict[str, object]]:
    """Return account and task records that represent unsuccessful work."""
    return [
        record
        for record in records
        if record.get("status") in {"error", "interrupted"}
        and record.get("record_type") in {"account", "task"}
    ]


def parse_fields(fields: str | None) -> list[str] | None:
    """Parse and validate a comma-separated field projection."""
    if fields is None:
        return None

    parsed_fields = [field.strip() for field in fields.split(",")]
    parsed_fields = [field for field in parsed_fields if field]

    if not parsed_fields:
        raise ValueError("--fields must include at least one field name")

    unknown_fields = [field for field in parsed_fields if field not in AVAILABLE_FIELDS]
    if unknown_fields:
        available = ", ".join(AVAILABLE_FIELDS)
        unknown = ", ".join(unknown_fields)
        message = f"Unknown result field: {unknown}. Available fields: {available}"
        raise ValueError(message)

    return parsed_fields


def limit_records(
    records: list[dict[str, object]], *, limit: int | None
) -> list[dict[str, object]]:
    """Limit records after filtering and before output formatting."""
    if limit is None:
        return records

    return records[:limit]


def project_records(
    records: list[dict[str, object]], *, fields: list[str] | None
) -> list[dict[str, object]]:
    """Project records to selected fields."""
    if fields is None:
        return records

    return [{field: record.get(field) for field in fields} for record in records]


def format_records_jsonl(records: list[dict[str, object]]) -> str:
    """Format result records as newline-delimited JSON."""
    return "\n".join(json.dumps(record, separators=(",", ":")) for record in records)


def format_records_table(
    records: list[dict[str, object]], *, fields: list[str] | None = None
) -> str:
    """Format result records as a compact table."""
    table_fields = fields or DEFAULT_TABLE_FIELDS
    rows = [
        [_format_cell(record.get(field)) for field in table_fields]
        for record in records
    ]
    return _format_table(
        headers=[FIELD_HEADERS.get(field, field) for field in table_fields], rows=rows
    )


def _target_type(config_branch: ConfigBranch) -> str:
    if config_branch is ConfigBranch.ACCOUNTS:
        return "account_group"

    return "organization"


def _timed_status_record(result: AccountResult | TaskResult) -> dict[str, object]:
    return {
        "status": result.status.value,
        "started_at": result.started_at,
        "ended_at": result.ended_at,
        "duration_seconds": result.duration_seconds,
    }


def _base_account_record(
    *, target_result: TargetResult, target_type: str, account_result: AccountResult
) -> dict[str, object]:
    return {
        "target_type": target_type,
        target_type: target_result.target_name,
        "target": target_result.target_name,
        "generated_at": target_result.generated_at,
        "dry_run": target_result.dry_run,
        "account_id": account_result.account_id,
        "account_alias": account_result.account_alias,
    }


def _normalize_status(status: str | None) -> str | None:
    if status is None:
        return None

    normalized = status.strip().lower()
    aliases = {"failed": "error", "failure": "error", "failures": "error"}
    return aliases.get(normalized, normalized)


def _matches(record: dict[str, object], key: str, expected: str | None) -> bool:
    if expected is None:
        return True

    actual = record.get(key)
    return isinstance(actual, str) and actual.lower() == expected.lower()


def _matches_account(record: dict[str, object], expected: str | None) -> bool:
    if expected is None:
        return True

    expected_lower = expected.lower()
    for key in ("account_id", "account_alias"):
        actual = record.get(key)
        if isinstance(actual, str) and actual.lower() == expected_lower:
            return True

    return False


def _format_table(*, headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "No matching results."

    widths = [
        max(len(header), *(len(row[index]) for row in rows))
        for index, header in enumerate(headers)
    ]
    header_line = "  ".join(
        header.ljust(widths[index]) for index, header in enumerate(headers)
    )
    divider = "  ".join("-" * width for width in widths)
    row_lines = [
        "  ".join(value.ljust(widths[index]) for index, value in enumerate(row))
        for row in rows
    ]
    return "\n".join([header_line, divider, *row_lines])


def _format_cell(value: object) -> str:
    if value is None:
        return ""

    if isinstance(value, str):
        return value

    if isinstance(value, bool | int | float):
        return str(value)

    return json.dumps(value, separators=(",", ":"))
