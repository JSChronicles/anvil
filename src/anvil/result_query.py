from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from dataclasses import replace
from pathlib import Path

from anvil.descriptors import ConfigBranch, LoadedConfig, TargetDescriptor
from anvil.results import EntityResult, TargetResult, TaskResult


JSONL_FILENAME = "results.jsonl"
DEFAULT_TABLE_FIELDS = [
    "record_type",
    "status",
    "target",
    "entity_id",
    "entity_name",
    "entity_metadata",
    "entity_type",
    "region",
    "task",
    "error",
]
FIELD_HEADERS = {"record_type": "type"}
AVAILABLE_FIELDS = [
    "actions",
    "config_file",
    "config_file_resolved",
    "dry_run",
    "duration_seconds",
    "ended_at",
    "entity_id",
    "entity_name",
    "entity_type",
    "error",
    "generated_at",
    "record_type",
    "provider",
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
    entity: str | None = None
    region: str | None = None
    task: str | None = None


def jsonl_path_for_run(*, run_dir: Path) -> Path:
    """Return the flattened JSONL path for a run directory."""
    return run_dir / JSONL_FILENAME


def build_jsonl_records_for_target(
    target_result: TargetResult, *, config_file: Path | None = None
) -> list[dict[str, object]]:
    """Build flattened entity and task records for a target result."""
    target_type = _target_type(target_result.config_branch)
    records: list[dict[str, object]] = []

    for entity_result in target_result.entities:
        entity_record = _base_entity_record(
            target_result=target_result,
            target_type=target_type,
            entity_result=entity_result,
            config_file=config_file,
        )
        records.append(
            {
                **entity_record,
                "record_type": "entity",
                **_timed_status_record(entity_result),
                "error": entity_result.error,
            }
        )

        for task_result in entity_result.tasks:
            records.append(
                {
                    **entity_record,
                    "record_type": "task",
                    "task": task_result.task_name,
                    "region": task_result.region,
                    **_timed_status_record(task_result),
                    "result": task_result.result,
                    "error": task_result.error,
                    "actions": list(task_result.actions),
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
    return list(iter_result_records(results_dir=results_dir, files=files))


def iter_result_records(
    *, results_dir: Path, files: list[Path] | None
) -> Iterator[dict[str, object]]:
    """Yield result records from explicit files or the default results directory."""
    paths = files if files else sorted(results_dir.glob(f"**/{JSONL_FILENAME}"))

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
                yield payload


def filter_records(
    records: list[dict[str, object]], *, filters: ResultFilters
) -> list[dict[str, object]]:
    """Return records matching all supplied filters."""
    return list(iter_filtered_records(records, filters=filters))


def iter_filtered_records(
    records: Iterable[dict[str, object]], *, filters: ResultFilters
) -> Iterator[dict[str, object]]:
    """Yield records matching all supplied filters."""
    status_filter = _normalize_status_filter(filters.status)

    for record in records:
        if (
            _matches(record, "record_type", filters.record_type)
            and _matches_status(record, status_filter)
            and _matches(record, "target", filters.target)
            and _matches_entity(record, filters.entity)
            and _matches(record, "region", filters.region)
            and _matches(record, "task", filters.task)
        ):
            yield record


def query_result_records(
    *,
    results_dir: Path,
    files: list[Path] | None,
    filters: ResultFilters,
    limit: int | None = None,
) -> list[dict[str, object]]:
    """Return filtered result records, stopping after limit matching records."""
    if limit is not None and limit <= 0:
        return []

    records: list[dict[str, object]] = []
    for record in iter_filtered_records(
        iter_result_records(results_dir=results_dir, files=files), filters=filters
    ):
        records.append(record)
        if limit is not None and len(records) >= limit:
            break

    return records


def failure_records(records: list[dict[str, object]]) -> list[dict[str, object]]:
    """Return entity and task records that represent unsuccessful work."""
    return [
        record
        for record in records
        if _record_is_unsuccessful(record)
        and record.get("record_type") in {"entity", "task"}
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
    if config_branch is not ConfigBranch.TARGETS:
        raise ValueError(f"Unsupported config branch: {config_branch}")
    return "target"


def _timed_status_record(result: EntityResult | TaskResult) -> dict[str, object]:
    return {
        "status": result.status.value,
        "started_at": result.started_at,
        "ended_at": result.ended_at,
        "duration_seconds": result.duration_seconds,
    }


def _base_entity_record(
    *,
    target_result: TargetResult,
    target_type: str,
    entity_result: EntityResult,
    config_file: Path | None,
) -> dict[str, object]:
    record: dict[str, object] = {
        "target_type": target_type,
        target_type: target_result.target_name,
        "target": target_result.target_name,
        "generated_at": target_result.generated_at,
        "dry_run": target_result.dry_run,
        "entity_id": entity_result.id,
        "entity_name": entity_result.name,
        "entity_type": entity_result.type,
        "provider": entity_result.provider,
        "entity_metadata": dict(entity_result.metadata),
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


def _narrow_target_for_failed_entity(
    *, target: TargetDescriptor, records: list[dict[str, object]]
) -> TargetDescriptor:
    failed_entity_ids = {
        entity_id
        for entity_id in (record.get("entity_id") for record in records)
        if isinstance(entity_id, str) and entity_id
    }
    task_records = [
        record
        for record in records
        if record.get("record_type") == "task" and _record_is_unsuccessful(record)
    ]
    task_failed_entity_ids = {
        entity_id
        for entity_id in (record.get("entity_id") for record in task_records)
        if isinstance(entity_id, str) and entity_id
    }
    entity_level_failure_exists = bool(failed_entity_ids - task_failed_entity_ids)

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
    if failed_regions and not entity_level_failure_exists:
        regions = [
            region for region in target.regions or [] if region in failed_regions
        ]
        if not regions:
            regions = sorted(failed_regions)

    tasks = target.tasks
    if failed_task_names and not entity_level_failure_exists:
        tasks = _expand_task_names_with_dependencies(
            selected_names=failed_task_names, tasks=target.tasks
        )
        if not tasks:
            tasks = target.tasks

    failed_entity_id = sorted(failed_entity_ids)[0]
    return replace(
        target, include=[failed_entity_id], exclude=None, regions=regions, tasks=tasks
    )


def _narrow_target_for_failure_records(
    *, target: TargetDescriptor, records: list[dict[str, object]]
) -> list[TargetDescriptor]:
    records_by_entity: dict[str, list[dict[str, object]]] = defaultdict(list)
    for record in records:
        entity_id = record.get("entity_id")
        if isinstance(entity_id, str) and entity_id:
            records_by_entity[entity_id].append(record)

    return [
        _narrow_target_for_failed_entity(target=target, records=entity_records)
        for _, entity_records in sorted(records_by_entity.items())
    ]


def _matches(record: dict[str, object], key: str, expected: str | None) -> bool:
    if expected is None:
        return True

    actual = record.get(key)
    return isinstance(actual, str) and actual.lower() == expected.lower()


def _matches_entity(record: dict[str, object], expected: str | None) -> bool:
    if expected is None:
        return True

    expected_lower = expected.lower()
    for key in ("entity_id", "entity_name"):
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
