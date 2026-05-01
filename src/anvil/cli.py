"""
CLI entrypoint for Anvil config-driven AWS account processing.
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
from pathlib import Path
from anvil.graph import render_graph
import yaml
from anvil.benchmark import BenchmarkRecorder
from anvil.descriptors import ConfigBranch, LoadedConfig
from anvil.result_query import (
    ResultFilters,
    failure_records,
    filter_records,
    format_records_jsonl,
    format_records_table,
    jsonl_path_for_run,
    limit_records,
    load_result_records,
    parse_fields,
    project_records,
    write_jsonl_records,
)
from anvil.results import EngineResult, EngineState
from anvil.runner import run_auth_checks, run_multiple_targets
from anvil.task_loader import (
    ResolvedTask,
    TaskDescriptor,
    _load_task_callable,
    discover_tasks,
    list_tasks,
)
from anvil.task_validation import validate_tasks
from anvil.validators import load_config_descriptors, validate_config_schema

__LOGGER__ = logging.getLogger(__name__)


def _load_targets_from_config_file(path: Path) -> LoadedConfig:
    """
    Load and validate target descriptors from a YAML config file.
    """
    __LOGGER__.debug(f"Loading config from {path}")

    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    validate_config_schema(config=raw)
    return load_config_descriptors(config=raw)


def _validate_cli_overrides(*, loaded_config: LoadedConfig, args) -> None:
    """
    Validate branch-specific CLI override semantics.
    """
    if loaded_config.branch.value == "accounts" and args.exclude is not None:
        raise ValueError(
            "CLI --exclude is not supported for account-group config files"
        )


def _add_common_config_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config-file",
        required=True,
        nargs="+",
        type=Path,
        help=(
            "Path(s) to YAML config file(s) defining organizations or explicit "
            "account groups"
        ),
    )

    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--include",
        nargs="+",
        help="Narrow the configured target set to specific account IDs",
    )
    group.add_argument(
        "--exclude",
        nargs="+",
        help="Organization-config only: exclude discovered account IDs",
    )


def _build_run_id() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H%M%SZ")


def _create_results_run_dir(*, config_file: Path) -> Path:
    run_dir = Path.cwd() / "results" / config_file.stem / _build_run_id()
    run_dir.mkdir(parents=True)
    return run_dir


def _target_results_dir_name(config_branch: ConfigBranch) -> str:
    if config_branch is ConfigBranch.ACCOUNTS:
        return "account-groups"

    return "organizations"


def _safe_result_filename(name: str) -> str:
    safe_name = "".join(
        character if character.isalnum() or character in {".", "-", "_"} else "_"
        for character in name
    )
    return safe_name.strip("._") or "target"


def _target_result_file_path(*, target_results_dir: Path, target_name: str) -> Path:
    safe_name = _safe_result_filename(target_name)
    result_file = target_results_dir / f"{safe_name}.json"
    suffix = 1

    while result_file.exists():
        result_file = target_results_dir / f"{safe_name}-{suffix}.json"
        suffix += 1

    return result_file


def _write_run_results(*, config_file: Path, engine_result) -> None:
    run_dir = _create_results_run_dir(config_file=config_file)
    target_results_dir = run_dir / _target_results_dir_name(engine_result.config_branch)
    target_results_dir.mkdir()

    recorder = BenchmarkRecorder(enabled=engine_result.benchmark is not None)
    target_files: list[dict[str, object]] = []

    for target_result in engine_result.target_results:
        result_file = _target_result_file_path(
            target_results_dir=target_results_dir,
            target_name=target_result.target_name,
        )

        with recorder.phase("serialization_seconds"):
            target_json = json.dumps(target_result.to_dict(), indent=2)
        target_serialize_seconds = recorder.pop("serialization_seconds")

        with recorder.phase("write_seconds"):
            with result_file.open("w", encoding="utf-8") as handle:
                handle.write(target_json)
        target_write_seconds = recorder.pop("write_seconds")
        if recorder.enabled:
            target_files.append(
                {
                    "target": target_result.target_name,
                    "serialization_seconds": target_serialize_seconds,
                    "write_seconds": target_write_seconds,
                    "bytes": len(target_json.encode("utf-8")),
                }
            )

    if engine_result.benchmark is not None:
        engine_result.benchmark["result_write"] = {"target_files": target_files}

    jsonl_path = jsonl_path_for_run(run_dir=run_dir)
    jsonl_record_count = write_jsonl_records(
        path=jsonl_path, target_results=engine_result.target_results
    )

    summary = engine_result.build_summary()
    summary_path = run_dir / "summary.json"

    summary_json = json.dumps(summary, indent=2)
    with summary_path.open("w", encoding="utf-8") as handle:
        handle.write(summary_json)

    __LOGGER__.info(
        f"Wrote run results to {run_dir}: summary={summary_path}, "
        f"target_files={len(engine_result.target_results)}, "
        f"jsonl_records={jsonl_record_count}"
    )


def _run_single_config_file(*, config_file: Path, args) -> int:
    loaded_config: LoadedConfig = _load_targets_from_config_file(config_file)
    _validate_cli_overrides(loaded_config=loaded_config, args=args)

    engine_result: EngineResult = run_multiple_targets(
        targets=loaded_config.targets,
        max_parallel_targets=loaded_config.max_parallel_targets,
        cli_dry_run=args.dry_run,
        cli_include=args.include,
        cli_exclude=args.exclude,
        benchmark_enabled=getattr(args, "benchmark", False),
    )
    _write_run_results(config_file=config_file, engine_result=engine_result)

    return 0 if engine_result.state is EngineState.COMPLETED_SUCCESS else 1


def _cmd_run(args) -> int:
    exit_code = 0

    for config_file in args.config_file:
        run_exit_code = _run_single_config_file(config_file=config_file, args=args)
        if run_exit_code != 0:
            exit_code = 1

    return exit_code


def _cmd_auth_check(args) -> int:
    overall_exit_code = 0

    for config_file in args.config_file:
        loaded_config: LoadedConfig = _load_targets_from_config_file(config_file)
        _validate_cli_overrides(loaded_config=loaded_config, args=args)

        engine_result: EngineResult = run_auth_checks(targets=loaded_config.targets)

        auth_payload: dict[str, str | list[dict[str, object]]] = {
            "generated_at": engine_result.generated_at,
            "auth": [
                auth_result.to_dict(config_branch=loaded_config.branch)
                for auth_result in engine_result.auth_results
            ],
        }

        if not args.quiet:
            print(json.dumps(auth_payload, indent=2))

        if engine_result.state is not EngineState.COMPLETED_SUCCESS:
            overall_exit_code = 1

    return overall_exit_code


def _cmd_list_tasks() -> int:
    tasks: list[TaskDescriptor] = list_tasks()

    print("Available tasks:")

    current_source: str | None = None
    for task in tasks:
        if task.source != current_source:
            if current_source is not None:
                print()

            print(f"{task.source}:")
            current_source = task.source

        print(f"  - {task.name}")

    return 0


def _cmd_tasks_validate() -> int:
    try:
        descriptors: list[TaskDescriptor] = discover_tasks()

        resolved = []
        for descriptor in descriptors:
            run = _load_task_callable(descriptor.name)
            resolved.append(
                ResolvedTask(
                    name=descriptor.name, run=run, depends_on=[], optional=False
                )
            )

        validate_tasks(resolved)

    except Exception as exc:
        print(f"[ERROR] task validation failed: {exc}")
        return 1

    print("[OK] all tasks are valid")
    return 0


def _cmd_graph(args) -> int:

    for config_file in args.config_file:
        loaded_config = _load_targets_from_config_file(config_file)
        _validate_cli_overrides(loaded_config=loaded_config, args=args)
        render_graph(targets=loaded_config.targets, output_json=args.json)

    return 0


def _load_filtered_result_records(
    args, *, record_type: str | None = None
) -> list[dict[str, object]]:
    records = load_result_records(
        results_dir=Path.cwd() / "results", files=args.results_file
    )
    if record_type is not None:
        records = [
            record for record in records if record.get("record_type") == record_type
        ]

    return filter_records(
        records,
        filters=ResultFilters(
            status=args.status,
            organization=args.organization,
            account=args.account,
            region=args.region,
            task=args.task,
        ),
    )


def _print_query_payload(
    payload: list[dict[str, object]],
    *,
    fields: list[str] | None,
    output_json: bool,
    output_jsonl: bool,
) -> None:
    projected_payload = project_records(payload, fields=fields)

    if output_json:
        print(json.dumps(projected_payload, indent=2))
        return

    if output_jsonl:
        jsonl_payload = format_records_jsonl(projected_payload)
        if jsonl_payload:
            print(jsonl_payload)
        return

    print(format_records_table(payload, fields=fields))


def _emit_result_records(args, records: list[dict[str, object]]) -> None:
    fields = parse_fields(args.fields)
    records = limit_records(records, limit=args.limit)
    _print_query_payload(
        records, fields=fields, output_json=args.json, output_jsonl=args.jsonl
    )


def _cmd_results_failures(args) -> int:
    records = _load_filtered_result_records(args)
    failures = failure_records(records)
    _emit_result_records(args, failures)
    return 0


def _cmd_results_accounts(args) -> int:
    records = _load_filtered_result_records(args, record_type="account")
    _emit_result_records(args, records)
    return 0


def _cmd_results_tasks(args) -> int:
    records = _load_filtered_result_records(args, record_type="task")
    _emit_result_records(args, records)
    return 0


def _cmd_results_regions(args) -> int:
    records = _load_filtered_result_records(args, record_type="task")
    _emit_result_records(args, records)
    return 0


def _positive_int(value: str) -> int:
    try:
        parsed_value = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error

    if parsed_value < 1:
        raise argparse.ArgumentTypeError("must be greater than or equal to 1")

    return parsed_value


def _add_results_query_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--results-file",
        type=Path,
        dest="results_file",
        help=(
            "Result JSONL file(s) to query. Defaults to every results.jsonl file "
            "under ./results."
        ),
        nargs="+",
    )
    parser.add_argument(
        "--status",
        help="Filter by status: success, error, interrupted, or failed",
    )
    parser.add_argument("--organization", help="Filter by organization or target name")
    parser.add_argument("--account", help="Filter by account ID or account alias")
    parser.add_argument("--region", help="Filter by AWS region")
    parser.add_argument("--task", help="Filter by task name")
    parser.add_argument(
        "--fields",
        help="Comma-separated result fields to include in output",
    )
    parser.add_argument(
        "--limit",
        type=_positive_int,
        help="Maximum number of records to print after filtering",
    )
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument("--json", action="store_true", help="Output JSON")
    output_group.add_argument("--jsonl", action="store_true", help="Output JSONL")


def _add_log_level_arg(parser: argparse.ArgumentParser) -> None:
    """
    Add a standard log-level option to a parser.
    """
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging verbosity",
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Anvil config-driven AWS account processing runner"
    )

    subparsers = parser.add_subparsers(dest="command", required=False)

    auth_parser = subparsers.add_parser("auth", help="Authentication commands")
    auth_subparsers = auth_parser.add_subparsers(dest="auth_command", required=True)

    auth_check_parser = auth_subparsers.add_parser(
        "check",
        help="Validate AWS authentication for configured organizations or account groups",
    )
    _add_common_config_args(auth_check_parser)
    _add_log_level_arg(auth_check_parser)
    auth_check_parser.add_argument(
        "--quiet", action="store_true", help="Suppress output (exit code only)"
    )
    auth_check_parser.set_defaults(func=_cmd_auth_check)

    run_parser = subparsers.add_parser(
        "run", help="Execute tasks from an organization or account-group config"
    )
    _add_common_config_args(run_parser)
    _add_log_level_arg(run_parser)
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=None,
        help="Run without making changes",
    )
    run_parser.add_argument(
        "--benchmark",
        action="store_true",
        help=(
            "Include diagnostic phase timings in result JSON. "
            "This can significantly increase output size."
        ),
    )
    run_parser.set_defaults(func=_cmd_run)

    tasks_parser = subparsers.add_parser("tasks", help="Task-related commands")
    _add_log_level_arg(tasks_parser)
    tasks_subparsers = tasks_parser.add_subparsers(dest="tasks_command", required=True)

    tasks_list_parser = tasks_subparsers.add_parser("list", help="List available tasks")
    tasks_list_parser.set_defaults(func=lambda _: _cmd_list_tasks())

    tasks_validate_parser = tasks_subparsers.add_parser(
        "validate", help="Validate available tasks without executing them"
    )
    tasks_validate_parser.set_defaults(func=lambda _: _cmd_tasks_validate())
    _add_log_level_arg(tasks_validate_parser)

    graph_parser = subparsers.add_parser(
        "graph",
        help="Show task dependency graph for configured organizations or account groups",
    )
    _add_common_config_args(graph_parser)
    _add_log_level_arg(graph_parser)
    graph_parser.add_argument(
        "--json", action="store_true", help="Output graph as JSON"
    )
    graph_parser.set_defaults(func=_cmd_graph)

    results_parser = subparsers.add_parser(
        "results", help="Query flattened run results"
    )
    _add_log_level_arg(results_parser)
    results_subparsers = results_parser.add_subparsers(
        dest="results_command", required=True
    )

    results_failures_parser = results_subparsers.add_parser(
        "failures", help="Show unsuccessful account and task records"
    )
    _add_results_query_args(results_failures_parser)
    results_failures_parser.set_defaults(func=_cmd_results_failures)

    results_accounts_parser = results_subparsers.add_parser(
        "accounts", help="Show account result records"
    )
    _add_results_query_args(results_accounts_parser)
    results_accounts_parser.set_defaults(func=_cmd_results_accounts)

    results_tasks_parser = results_subparsers.add_parser(
        "tasks", help="Show task result records"
    )
    _add_results_query_args(results_tasks_parser)
    results_tasks_parser.set_defaults(func=_cmd_results_tasks)

    results_regions_parser = results_subparsers.add_parser(
        "regions", help="Show task result records filtered by region"
    )
    _add_results_query_args(results_regions_parser)
    results_regions_parser.set_defaults(func=_cmd_results_regions)

    args = parser.parse_args()

    if not args.command:
        parser.error("the following arguments are required: command")

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format=("%(levelname)-8s [%(filename)s:%(funcName)s:%(lineno)d] %(message)s"),
    )
    # Suppress repeated botocore SSO cache reads at INFO while keeping Anvil INFO logs.
    logging.getLogger("botocore.tokens").setLevel(logging.WARNING)

    try:
        exit_code = args.func(args)
        raise SystemExit(exit_code)
    except Exception as error:
        __LOGGER__.error(f"Execution failed: {error}")
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
