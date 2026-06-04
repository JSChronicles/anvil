from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from dataclasses import replace
from pathlib import Path

from anvil.descriptors import ConfigBranch, LoadedConfig, TargetDescriptor
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
    "config_file",
    "config_file_resolved",
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
    record_type: str | None = None
    status: str | None = None
    target: str | None = None
    account: str | None = None
    region: str | None = None
    task: str | None = None


def jsonl_path_for_run(*, run_dir: Path) -> Path:
    """Return the flattened JSONL path for a run directory."""
    return run_dir / JSONL_FILENAME


def build_jsonl_records_for_target(
    target_result: TargetResult, *, config_file: Path | None = None
) -> list[dict[str, object]]:
    """Build flattened account and task records for a target result."""
    target_type = _target_type(target_result.config_branch)
    records: list[dict[str, object]] = []

    for account_result in target_result.account_results:
        account_record = _base_account_record(
            target_result=target_result,
            target_type=target_type,
            account_result=account_result,
            config_file=config_file,
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


def write_jsonl_records(
    *, path: Path, target_results: list[TargetResult], config_file: Path | None = None
) -> int:
    """Write flattened result records and return the number of records written."""
    record_count = 0
    with path.open("w", encoding="utf-8") as handle:
        for target_result in target_results:
            for record in build_jsonl_records_for_target(
                target_result, config_file=config_file
            ):
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
    status_filter = _normalize_status_filter(filters.status)

    return [
        record
        for record in records
        if _matches(record, "record_type", filters.record_type)
        and _matches_status(record, status_filter)
        and _matches(record, "target", filters.target)
        and _matches_account(record, filters.account)
        and _matches(record, "region", filters.region)
        and _matches(record, "task", filters.task)
    ]


def failure_records(records: list[dict[str, object]]) -> list[dict[str, object]]:
    """Return account and task records that represent unsuccessful work."""
    return [
        record
        for record in records
        if _record_is_unsuccessful(record)
        and record.get("record_type") in {"account", "task"}
    ]


def config_file_for_failure_records(
    *, failures: list[dict[str, object]]
) -> dict[Path, list[dict[str, object]]]:
    """Group failure records by their original config file."""
    records_by_config: dict[Path, list[dict[str, object]]] = defaultdict(list)

    for record in failures:
        config_file = record.get("config_file_resolved")
        if isinstance(config_file, str) and config_file:
            records_by_config[Path(config_file)].append(record)
        else:
            raise ValueError(
                "Result records do not include config_file_resolved and cannot be rerun."
            )

    return dict(records_by_config)


def build_rerun_targets(
    *, loaded_config: LoadedConfig, failures: list[dict[str, object]]
) -> list[TargetDescriptor]:
    """Build narrowed targets from failure records and the original config."""
    failures_by_target: dict[str, list[dict[str, object]]] = defaultdict(list)
    for record in failures:
        target_name = record.get("target")
        if isinstance(target_name, str) and target_name:
            failures_by_target[target_name].append(record)

    targets: list[TargetDescriptor] = []
    for target in loaded_config.targets:
        if target.name not in failures_by_target:
            continue
        targets.extend(
            _narrow_target_for_failure_records(
                target=target, records=failures_by_target[target.name]
            )
        )

    return targets


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
    *,
    target_result: TargetResult,
    target_type: str,
    account_result: AccountResult,
    config_file: Path | None,
) -> dict[str, object]:
    record: dict[str, object] = {
        "target_type": target_type,
        target_type: target_result.target_name,
        "target": target_result.target_name,
        "generated_at": target_result.generated_at,
        "dry_run": target_result.dry_run,
        "account_id": account_result.account_id,
        "account_alias": account_result.account_alias,
    }
    if config_file is not None:
        record["config_file"] = config_file.as_posix()
        record["config_file_resolved"] = config_file.resolve().as_posix()

    return record


def _normalize_status_filter(status: str | None) -> str | set[str] | None:
    if status is None:
        return None

    normalized = status.strip().lower()
    if normalized in {"failed", "failure", "failures"}:
        return "failed"

    return {normalized}


def _record_is_unsuccessful(record: dict[str, object]) -> bool:
    status = record.get("status")
    return isinstance(status, str) and status.lower() != "success"


def _matches_status(record: dict[str, object], expected: str | set[str] | None) -> bool:
    if expected is None:
        return True

    actual = record.get("status")
    if not isinstance(actual, str):
        return False

    normalized_actual = actual.lower()
    if expected == "failed":
        return _record_is_unsuccessful(record)

    return normalized_actual in expected


def _task_specs_by_name(tasks: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    return {
        str(task["name"]): task
        for task in tasks
        if isinstance(task.get("name"), str) and task.get("name")
    }


def _expand_task_names_with_dependencies(
    *, selected_names: set[str], tasks: list[dict[str, object]]
) -> list[dict[str, object]]:
    task_specs = _task_specs_by_name(tasks)
    expanded_names: set[str] = set()

    def add_with_dependencies(task_name: str) -> None:
        if task_name in expanded_names:
            return
        task = task_specs.get(task_name)
        if task is None:
            return
        depends_on = task.get("depends_on", [])
        if not isinstance(depends_on, list):
            depends_on = []
        for dependency in depends_on:
            if isinstance(dependency, str):
                add_with_dependencies(dependency)
        expanded_names.add(task_name)

    for selected_name in selected_names:
        add_with_dependencies(selected_name)

    return [
        task
        for task in tasks
        if isinstance(task.get("name"), str) and task["name"] in expanded_names
    ]


def _narrow_target_for_failed_account(
    *, target: TargetDescriptor, records: list[dict[str, object]]
) -> TargetDescriptor:
    failed_account_ids = {
        account_id
        for account_id in (record.get("account_id") for record in records)
        if isinstance(account_id, str) and account_id
    }
    task_records = [
        record
        for record in records
        if record.get("record_type") == "task" and _record_is_unsuccessful(record)
    ]
    task_failed_account_ids = {
        account_id
        for account_id in (record.get("account_id") for record in task_records)
        if isinstance(account_id, str) and account_id
    }
    account_level_failure_exists = bool(failed_account_ids - task_failed_account_ids)

    failed_regions = {
        region
        for region in (record.get("region") for record in task_records)
        if isinstance(region, str) and region
    }
    failed_task_names = {
        task
        for task in (record.get("task") for record in task_records)
        if isinstance(task, str) and task
    }

    regions = target.regions
    if failed_regions and not account_level_failure_exists:
        regions = [region for region in target.regions if region in failed_regions]
        if not regions:
            regions = sorted(failed_regions)

    tasks = target.tasks
    if failed_task_names and not account_level_failure_exists:
        tasks = _expand_task_names_with_dependencies(
            selected_names=failed_task_names, tasks=target.tasks
        )
        if not tasks:
            tasks = target.tasks

    failed_account_id = sorted(failed_account_ids)[0]
    return replace(
        target, include=[failed_account_id], exclude=None, regions=regions, tasks=tasks
    )


def _narrow_target_for_failure_records(
    *, target: TargetDescriptor, records: list[dict[str, object]]
) -> list[TargetDescriptor]:
    records_by_account: dict[str, list[dict[str, object]]] = defaultdict(list)
    for record in records:
        account_id = record.get("account_id")
        if isinstance(account_id, str) and account_id:
            records_by_account[account_id].append(record)

    return [
        _narrow_target_for_failed_account(target=target, records=account_records)
        for _, account_records in sorted(records_by_account.items())
    ]


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
