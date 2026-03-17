"""
CLI entrypoint for multi-organization AWS account processing.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import yaml
from anvil.descriptors import OrgDescriptor
from anvil.results import EngineState
from anvil.runner import run_auth_checks, run_multiple_orgs
from anvil.task_loader import (
    ResolvedTask,
    _load_task_callable,
    discover_tasks,
    list_tasks,
)
from anvil.task_validation import validate_tasks
from anvil.validators import validate_org_config_schema

__LOGGER__ = logging.getLogger(__name__)


def _load_orgs_from_file(path: Path) -> list[OrgDescriptor]:
    """
    Load and validate organization descriptors from a YAML file.
    """
    __LOGGER__.debug(f"Loading organization config from {path}")

    if not path.exists():
        raise FileNotFoundError(f"Org config file not found: {path}")

    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    validate_org_config_schema(config=raw)

    org_entries = raw.get("organizations")
    if not isinstance(org_entries, list):
        raise ValueError("Invalid org config: 'organizations' must be a list")

    orgs: list[OrgDescriptor] = []

    for index, entry in enumerate(org_entries, start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"Organization entry #{index} must be a mapping")

        orgs.append(OrgDescriptor(**entry))

    if not orgs:
        raise ValueError("No organizations defined in config file")

    return orgs


# ============================================================================
# Argument helpers
# ============================================================================


def _add_common_org_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--org-file",
        required=True,
        type=Path,
        help="Path to YAML file defining organizations",
    )

    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--include", nargs="+", help="Account IDs to exclusively include"
    )
    group.add_argument("--exclude", nargs="+", help="Account IDs to exclude")


# ============================================================================
# Command handlers
# ============================================================================


def _cmd_run(args) -> int:
    orgs = _load_orgs_from_file(args.org_file)

    engine_result = run_multiple_orgs(
        orgs=orgs,
        cli_dry_run=args.dry_run,
        cli_include=args.include,
        cli_exclude=args.exclude,
    )

    results_dir = Path.cwd() / "results"
    results_dir.mkdir(exist_ok=True)

    summary = engine_result.build_summary()

    for organization_result in engine_result.organization_results:
        safe_name = organization_result.org_name.replace("/", "_").replace(" ", "_")

        org_file = results_dir / f"{safe_name}.json"

        with org_file.open("w", encoding="utf-8") as handle:
            json.dump(organization_result.to_dict(), handle, indent=2)

    summary_path = Path.cwd() / "multi-org-summary.json"

    with summary_path.open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2)

    __LOGGER__.info(
        f"Wrote summary to {summary_path} and "
        f"{len(engine_result.organization_results)} org result files"
    )

    return 0 if engine_result.state is EngineState.COMPLETED_SUCCESS else 1


def _cmd_auth_check(args) -> int:
    """
    Auth-only execution for configured organizations.
    """
    orgs = _load_orgs_from_file(args.org_file)

    engine_result = run_auth_checks(orgs=orgs)

    auth_payload = {
        "generated_at": engine_result.generated_at,
        "auth": [auth_result.to_dict() for auth_result in engine_result.auth_results],
    }

    if not args.quiet:
        if args.json:
            print(json.dumps(auth_payload, indent=2))
        else:
            print(auth_payload)

    return 0 if engine_result.state is EngineState.COMPLETED_SUCCESS else 1


def _cmd_list_tasks() -> int:
    tasks = list_tasks()

    print("Available tasks:")
    for task in tasks:
        print(f"  - {task}")

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
    orgs = _load_orgs_from_file(args.org_file)

    # Import lazily so CLI stays lightweight
    from anvil.graph import render_graph

    render_graph(orgs=orgs, output_json=args.json)

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Multi-org AWS account processing runner"
    )

    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging verbosity",
    )

    subparsers = parser.add_subparsers(dest="command", required=False)

    # ------------------------------------------------------------------
    # auth check
    # ------------------------------------------------------------------

    auth_parser = subparsers.add_parser("auth", help="Authentication commands")

    auth_subparsers = auth_parser.add_subparsers(dest="auth_command", required=True)

    auth_check_parser = auth_subparsers.add_parser(
        "check", help="Validate AWS authentication for organizations"
    )

    _add_common_org_args(auth_check_parser)

    auth_check_parser.add_argument(
        "--json", action="store_true", help="Output results as JSON"
    )

    auth_check_parser.add_argument(
        "--quiet", action="store_true", help="Suppress output (exit code only)"
    )

    auth_check_parser.set_defaults(func=_cmd_auth_check)

    # ------------------------------------------------------------------
    # run
    # ------------------------------------------------------------------

    run_parser = subparsers.add_parser("run", help="Execute tasks across organizations")

    _add_common_org_args(run_parser)

    run_parser.add_argument(
        "--dry-run", action="store_true", help="Run without making changes"
    )

    run_parser.set_defaults(func=_cmd_run)

    # ------------------------------------------------------------------
    # tasks
    # ------------------------------------------------------------------

    tasks_parser = subparsers.add_parser("tasks", help="Task-related commands")

    tasks_subparsers = tasks_parser.add_subparsers(dest="tasks_command", required=True)

    tasks_list_parser = tasks_subparsers.add_parser("list", help="List available tasks")

    tasks_list_parser.set_defaults(func=lambda _: _cmd_list_tasks())

    tasks_validate_parser = tasks_subparsers.add_parser(
        "validate", help="Validate available tasks without executing them"
    )

    tasks_validate_parser.set_defaults(func=lambda _: _cmd_tasks_validate())

    # ------------------------------------------------------------------
    # grah
    # ------------------------------------------------------------------

    graph_parser = subparsers.add_parser(
        "graph", help="Show execution dependency graph"
    )

    _add_common_org_args(graph_parser)

    graph_parser.add_argument(
        "--json", action="store_true", help="Output graph as JSON"
    )

    graph_parser.set_defaults(func=_cmd_graph)

    # ------------------------------------------------------------------
    # Parse + dispatch
    # ------------------------------------------------------------------

    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format=("%(levelname)-8s [%(filename)s:%(funcName)s:%(lineno)d] %(message)s"),
    )

    if not args.command:
        parser.error("the following arguments are required: command")

    try:
        exit_code = args.func(args)
        raise SystemExit(exit_code)
    except Exception as error:
        __LOGGER__.error(f"Execution failed: {error}")
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
