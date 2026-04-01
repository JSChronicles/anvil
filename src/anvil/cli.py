"""
CLI entrypoint for Anvil config-driven AWS account processing.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import yaml
from anvil.descriptors import LoadedConfig
from anvil.results import EngineState
from anvil.runner import run_auth_checks, run_multiple_targets
from anvil.task_loader import (
    ResolvedTask,
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
        type=Path,
        help="Path to YAML config file defining organizations or explicit account groups",
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


def _cmd_run(args) -> int:
    loaded_config = _load_targets_from_config_file(args.config_file)
    _validate_cli_overrides(loaded_config=loaded_config, args=args)

    engine_result = run_multiple_targets(
        targets=loaded_config.targets,
        cli_dry_run=args.dry_run,
        cli_include=args.include,
        cli_exclude=args.exclude,
    )

    results_dir = Path.cwd() / "results"
    results_dir.mkdir(exist_ok=True)

    summary = engine_result.build_summary()

    for target_result in engine_result.target_results:
        safe_name = target_result.target_name.replace("/", "_").replace(" ", "_")
        result_file = results_dir / f"{safe_name}.json"

        with result_file.open("w", encoding="utf-8") as handle:
            json.dump(target_result.to_dict(), handle, indent=2)

    summary_path = results_dir / "target-summary.json"

    with summary_path.open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2)

    __LOGGER__.info(
        f"Wrote summary to {summary_path} and "
        f"{len(engine_result.target_results)} target result files"
    )

    return 0 if engine_result.state is EngineState.COMPLETED_SUCCESS else 1


def _cmd_auth_check(args) -> int:
    """
    Auth-only execution for configured targets.
    """
    loaded_config = _load_targets_from_config_file(args.config_file)
    _validate_cli_overrides(loaded_config=loaded_config, args=args)

    engine_result = run_auth_checks(targets=loaded_config.targets)

    auth_payload = {
        "generated_at": engine_result.generated_at,
        "auth": [
            auth_result.to_dict(config_branch=loaded_config.branch)
            for auth_result in engine_result.auth_results
        ],
    }

    if not args.quiet:
        print(json.dumps(auth_payload, indent=2))

    return 0 if engine_result.state is EngineState.COMPLETED_SUCCESS else 1


def _cmd_list_tasks() -> int:
    tasks = list_tasks()

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
        descriptors = discover_tasks()

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
    loaded_config = _load_targets_from_config_file(args.config_file)
    _validate_cli_overrides(loaded_config=loaded_config, args=args)

    from anvil.graph import render_graph

    render_graph(targets=loaded_config.targets, output_json=args.json)

    return 0


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
        "check", help="Validate AWS authentication for configured organizations or account groups"
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
        "graph", help="Show task dependency graph for configured organizations or account groups"
    )
    _add_common_config_args(graph_parser)
    _add_log_level_arg(graph_parser)
    graph_parser.add_argument(
        "--json", action="store_true", help="Output graph as JSON"
    )
    graph_parser.set_defaults(func=_cmd_graph)

    args = parser.parse_args()

    if not args.command:
        parser.error("the following arguments are required: command")

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format=("%(levelname)-8s [%(filename)s:%(funcName)s:%(lineno)d] %(message)s"),
    )

    try:
        exit_code = args.func(args)
        raise SystemExit(exit_code)
    except Exception as error:
        __LOGGER__.error(f"Execution failed: {error}")
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
