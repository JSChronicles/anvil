"""
CLI entrypoint for Anvil config-driven provider target processing.
"""

from __future__ import annotations

import argparse
import datetime
import inspect
import json
import logging
import os
import shlex
import shutil
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from importlib import metadata as importlib_metadata
from importlib import util as importlib_util
from pathlib import Path
from typing import Protocol

import yaml

from anvil._loader_utils import DiscoveryIssue
from anvil.benchmark import BenchmarkRecorder
from anvil.descriptors import ConfigBranch, LoadedConfig
from anvil.graph import render_graph
from anvil.processor_loader import (
    ProcessorDescriptor,
    ProcessorSpec,
    discover_processors,
    list_processors,
    load_completed_run_context,
    resolve_processor_output_path,
    run_configured_post_processors,
    run_processors,
)
from anvil.processor_validation import processor_validation_errors
from anvil.provider_loader import ProviderDescriptor, discover_providers, list_providers
from anvil.providers.base import validate_provider_contract
from anvil.result_query import (
    ResultFilters,
    build_rerun_targets,
    config_file_for_failure_records,
    failure_records,
    format_records_jsonl,
    format_records_table,
    jsonl_path_for_run,
    parse_fields,
    project_records,
    query_result_records,
    write_jsonl_records,
)
from anvil.results import EngineResult, EngineState
from anvil.runner import run_auth_checks, run_multiple_targets
from anvil.task_loader import ResolvedTask, TaskDescriptor, discover_tasks, list_tasks
from anvil.task_validation import task_validation_errors
from anvil.validators import load_config_descriptors, validate_config_schema

__LOGGER__ = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WrittenRunResults:
    """Paths and summary metadata written for one Anvil run."""

    run_dir: Path
    summary_path: Path
    jsonl_path: Path
    summary: dict[str, object]
    target_result_paths: dict[str, Path]
    target_file_count: int
    jsonl_record_count: int


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Result for one requested validation category."""

    label: str
    succeeded: bool
    error: str | None = None


@dataclass(frozen=True, slots=True)
class DiagnosticCheck:
    """One offline diagnostic line emitted by default validation."""

    section: str
    status: str
    label: str
    detail: str | None = None


class ListableDescriptor(Protocol):
    """Descriptor fields needed for grouped CLI listing."""

    name: str
    source: str


class DetailDescriptor(ListableDescriptor, Protocol):
    """Descriptor fields needed for CLI detail output."""

    load: Callable[[], Callable]


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


def _validate_cli_overrides(
    *, loaded_config: LoadedConfig, args: argparse.Namespace
) -> None:
    """
    Validate branch-specific CLI override semantics.
    """
    if loaded_config.branch is ConfigBranch.TARGETS and args.exclude is not None:
        explicit_targets = [
            target for target in loaded_config.targets if target.is_explicit_mode
        ]
        if explicit_targets:
            target_names = ", ".join(target.name for target in explicit_targets)
            raise ValueError(
                "CLI --exclude is not supported for explicit provider modes; "
                f"target(s): {target_names}"
            )


def _add_common_config_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config-file",
        required=True,
        nargs="+",
        type=Path,
        help=(
            "Path(s) to schema_version: 2 YAML config file(s) defining provider targets"
        ),
    )

    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--include",
        nargs="+",
        help=(
            "Narrow the configured target set to specific provider target IDs; "
            "mutually exclusive with --exclude"
        ),
    )
    group.add_argument(
        "--exclude",
        nargs="+",
        help=(
            "Discovery-config only: exclude discovered provider target IDs; "
            "mutually exclusive with --include"
        ),
    )


def _build_run_id() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H%M%SZ")


def _create_results_run_dir(*, config_file: Path) -> Path:
    run_dir = Path.cwd() / "results" / config_file.stem / _build_run_id()
    run_dir.mkdir(parents=True)
    return run_dir


def _target_results_dir_name(config_branch: ConfigBranch) -> str:
    if config_branch is not ConfigBranch.TARGETS:
        raise ValueError(f"Unsupported config branch: {config_branch}")
    return "targets"


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


def _write_run_results(
    *, config_file: Path, engine_result: EngineResult
) -> WrittenRunResults:
    run_dir = _create_results_run_dir(config_file=config_file)
    target_results_dir = run_dir / _target_results_dir_name(engine_result.config_branch)
    target_results_dir.mkdir()

    recorder = BenchmarkRecorder(enabled=engine_result.benchmark is not None)
    target_files: list[dict[str, object]] = []
    target_result_paths: dict[str, Path] = {}

    for target_result in engine_result.target_results:
        result_file = _target_result_file_path(
            target_results_dir=target_results_dir, target_name=target_result.target_name
        )
        target_result_paths[target_result.target_name] = result_file

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
        path=jsonl_path,
        target_results=engine_result.target_results,
        config_file=config_file,
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

    return WrittenRunResults(
        run_dir=run_dir,
        summary_path=summary_path,
        jsonl_path=jsonl_path,
        summary=summary,
        target_result_paths=target_result_paths,
        target_file_count=len(engine_result.target_results),
        jsonl_record_count=jsonl_record_count,
    )


def _summary_has_queryable_failures(summary: dict[str, object]) -> bool:
    for key in (
        "total_failed_entities",
        "total_interrupted_entities",
        "total_failed_tasks",
    ):
        value = summary.get(key)
        if isinstance(value, int) and value > 0:
            return True

    return False


def _display_command_path(path: Path) -> str:
    try:
        display_path = f"./{path.resolve().relative_to(Path.cwd().resolve())}"
    except ValueError:
        display_path = str(path)

    display_path = display_path.replace("\\", "/")
    return shlex.quote(display_path)


def _print_failure_followups(*, results_file: Path) -> None:
    results_path = _display_command_path(results_file)
    print()
    print("View failures:")
    print(f"  anvil results --status failed --results-file {results_path}")
    print()
    print("Rerun failed entities:")
    print(f"  anvil results --status failed --results-file {results_path} --rerun")


def _run_single_config_file(*, config_file: Path, args: argparse.Namespace) -> int:
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
    written_results = _write_run_results(
        config_file=config_file, engine_result=engine_result
    )
    run_configured_post_processors(
        config_branch=loaded_config.branch,
        targets=loaded_config.targets,
        target_results=engine_result.target_results,
        run_dir=written_results.run_dir,
        summary_path=written_results.summary_path,
        summary=written_results.summary,
        target_result_paths=written_results.target_result_paths,
    )
    if _summary_has_queryable_failures(written_results.summary):
        _print_failure_followups(results_file=written_results.jsonl_path)

    return 0 if engine_result.state is EngineState.COMPLETED_SUCCESS else 1


def _cmd_run(args: argparse.Namespace) -> int:
    exit_code = 0

    for config_file in args.config_file:
        run_exit_code = _run_single_config_file(config_file=config_file, args=args)
        if run_exit_code != 0:
            exit_code = 1

    return exit_code


def _cmd_auth_check(args: argparse.Namespace) -> int:
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


def _cmd_validate_auth(args: argparse.Namespace) -> int:
    if args.config_file is None:
        raise ValueError("--config-file is required with --auth")

    auth_args = argparse.Namespace(
        config_file=args.config_file,
        include=args.include,
        exclude=args.exclude,
        quiet=True,
    )
    return _cmd_auth_check(auth_args)


def _validate_config_files(args: argparse.Namespace) -> None:
    if args.config_file is None:
        raise ValueError("--config-file is required for config validation")

    for config_file in args.config_file:
        loaded_config = _load_targets_from_config_file(config_file)
        _validate_cli_overrides(loaded_config=loaded_config, args=args)


def _print_grouped_listing(
    *, label: str, descriptors: Sequence[ListableDescriptor]
) -> None:
    """Print descriptors grouped by their source."""
    print(f"Available {label}:")

    current_source: str | None = None
    for descriptor in descriptors:
        if descriptor.source != current_source:
            if current_source is not None:
                print()

            print(f"{descriptor.source}:")
            current_source = descriptor.source

        print(f"  - {descriptor.name}")


def _detail_text(*, descriptor: DetailDescriptor) -> str:
    try:
        run = descriptor.load()
    except Exception as exc:
        raise ValueError(f"{descriptor.name} ({descriptor.source}): {exc}") from exc

    doc = inspect.getdoc(run)
    if doc is None:
        module = inspect.getmodule(run)
        if module is not None:
            doc = inspect.getdoc(module)

    if doc is None:
        raise ValueError(
            f"No detail available for {descriptor.name} ({descriptor.source})"
        )

    return f"{descriptor.name} ({descriptor.source})\n\n{doc}"


def _select_detail_descriptor(
    *, descriptors: list[DetailDescriptor], name: str, label: str
) -> DetailDescriptor:
    matches = [descriptor for descriptor in descriptors if descriptor.name == name]
    if not matches:
        available_display = ", ".join(
            sorted({descriptor.name for descriptor in descriptors})
        )
        raise ValueError(
            f"Unknown {label}: {name}. Available {label}s: {available_display}"
        )

    if len(matches) > 1:
        source_display = ", ".join(descriptor.source for descriptor in matches)
        raise ValueError(
            f"{label.capitalize()} '{name}' is ambiguous; found in multiple "
            f"sources: {source_display}"
        )

    return matches[0]


def _print_single_detail(
    *, descriptors: list[DetailDescriptor], name: str, label: str
) -> None:
    descriptor = _select_detail_descriptor(
        descriptors=descriptors, name=name, label=label
    )
    print(_detail_text(descriptor=descriptor))


def _cmd_list(args: argparse.Namespace) -> int:
    _validate_list_args(args)

    if args.tasks is not None:
        if args.detail:
            _print_single_detail(
                descriptors=discover_tasks().tasks, name=args.tasks[0], label="task"
            )
            return 0

        _print_grouped_listing(label="tasks", descriptors=list_tasks())
        return 0

    if getattr(args, "providers", False):
        _print_grouped_listing(label="providers", descriptors=list_providers())
        return 0

    if args.detail:
        _print_single_detail(
            descriptors=discover_processors().processors,
            name=args.processors[0],
            label="processor",
        )
        return 0

    _print_grouped_listing(label="processors", descriptors=list_processors())
    return 0


def _discovery_issue_messages(issues: list[DiscoveryIssue]) -> list[str]:
    return [f"{issue.name} ({issue.source}): {issue.error}" for issue in issues]


def _raise_validation_errors(errors: list[str]) -> None:
    if errors:
        raise ValueError("\n  - " + "\n  - ".join(errors))


def _select_task_descriptors(
    *, descriptors: list[TaskDescriptor], task_names: list[str]
) -> list[TaskDescriptor]:
    available_names = {descriptor.name for descriptor in descriptors}
    unknown_names = [name for name in task_names if name not in available_names]

    if unknown_names:
        available_display = ", ".join(sorted(available_names))
        unknown_display = ", ".join(unknown_names)
        raise ValueError(
            f"Unknown task(s): {unknown_display}. Available tasks: {available_display}"
        )

    requested_names = set(task_names)
    return [
        descriptor for descriptor in descriptors if descriptor.name in requested_names
    ]


def _select_tasks(task_names: list[str]) -> list[TaskDescriptor]:
    return _select_task_descriptors(
        descriptors=discover_tasks().tasks, task_names=task_names
    )


def _validate_selected_tasks(task_names: list[str] | None) -> None:
    discovery = discover_tasks()
    errors: list[str] = []
    descriptors = discovery.tasks

    if task_names:
        try:
            descriptors = _select_task_descriptors(
                descriptors=discovery.tasks, task_names=task_names
            )
        except ValueError as exc:
            errors.append(str(exc))
            errors.extend(_discovery_issue_messages(discovery.issues))
            _raise_validation_errors(errors)
    else:
        errors.extend(_discovery_issue_messages(discovery.issues))

    resolved = []
    for descriptor in descriptors:
        try:
            run = descriptor.load()
        except Exception as exc:
            errors.append(f"{descriptor.name} ({descriptor.source}): {exc}")
            continue

        resolved.append(
            ResolvedTask(name=descriptor.name, run=run, depends_on=[], optional=False)
        )

    errors.extend(task_validation_errors(resolved))
    _raise_validation_errors(errors)


def _select_processor_descriptors(
    *, descriptors: list[ProcessorDescriptor], processor_names: list[str]
) -> list[ProcessorDescriptor]:
    requested_names = set(processor_names)
    available_names = {descriptor.name for descriptor in descriptors}
    unknown_names = [name for name in processor_names if name not in available_names]

    if unknown_names:
        available_display = ", ".join(sorted(available_names))
        unknown_display = ", ".join(unknown_names)
        raise ValueError(
            f"Unknown processor(s): {unknown_display}. "
            f"Available processors: {available_display}"
        )

    return [
        descriptor for descriptor in descriptors if descriptor.name in requested_names
    ]


def _select_processors(processor_names: list[str]) -> list[ProcessorDescriptor]:
    return _select_processor_descriptors(
        descriptors=discover_processors().processors, processor_names=processor_names
    )


def _validate_selected_processors(processor_names: list[str] | None) -> None:
    discovery = discover_processors()
    errors: list[str] = []
    processors = discovery.processors

    if processor_names:
        try:
            processors = _select_processor_descriptors(
                descriptors=discovery.processors, processor_names=processor_names
            )
        except ValueError as exc:
            errors.append(str(exc))
            errors.extend(_discovery_issue_messages(discovery.issues))
            _raise_validation_errors(errors)
    else:
        errors.extend(_discovery_issue_messages(discovery.issues))

    errors.extend(processor_validation_errors(processors))
    _raise_validation_errors(errors)


def _select_provider_descriptors(
    *, descriptors: list[ProviderDescriptor], provider_names: list[str]
) -> list[ProviderDescriptor]:
    descriptor_by_name = {descriptor.name: descriptor for descriptor in descriptors}
    unknown_names = [name for name in provider_names if name not in descriptor_by_name]

    if unknown_names:
        available_names = ", ".join(sorted(descriptor_by_name))
        unknown_display = ", ".join(unknown_names)
        raise ValueError(
            f"Unknown provider(s): {unknown_display}. "
            f"Available providers: {available_names}"
        )

    return [descriptor_by_name[name] for name in provider_names]


def _validate_selected_providers(provider_names: list[str] | None) -> None:
    discovery = discover_providers()
    errors: list[str] = []
    providers = discovery.providers

    if provider_names:
        try:
            providers = _select_provider_descriptors(
                descriptors=discovery.providers, provider_names=provider_names
            )
        except ValueError as exc:
            errors.append(str(exc))
            errors.extend(_discovery_issue_messages(discovery.issues))
            _raise_validation_errors(errors)
    else:
        errors.extend(_discovery_issue_messages(discovery.issues))

    for descriptor in providers:
        try:
            provider = descriptor.load()
            validate_provider_contract(provider)
        except Exception as exc:
            errors.append(f"{descriptor.name} ({descriptor.source}): {exc}")

    _raise_validation_errors(errors)


def _validation_result(label: str, callback) -> ValidationResult:
    try:
        result = callback()
    except Exception as exc:
        return ValidationResult(label=label, succeeded=False, error=str(exc))

    if isinstance(result, int) and result != 0:
        return ValidationResult(label=label, succeeded=False)

    return ValidationResult(label=label, succeeded=True)


def _print_validation_summary(results: list[ValidationResult]) -> None:
    for result in results:
        status = "[OK]" if result.succeeded else "[ERROR]"
        print(f"{status:<8} {result.label}")
        if result.error is not None:
            for error_line in result.error.splitlines():
                if error_line:
                    print(f"         {error_line}")


def _cmd_validate(args: argparse.Namespace) -> int:
    results: list[ValidationResult] = []

    if args.config_file is not None and not args.auth:
        results.append(
            _validation_result("Config", lambda: _validate_config_files(args))
        )

    if args.tasks is not None:
        results.append(
            _validation_result("Tasks", lambda: _validate_selected_tasks(args.tasks))
        )

    if args.processors is not None:
        results.append(
            _validation_result(
                "Processors", lambda: _validate_selected_processors(args.processors)
            )
        )

    if getattr(args, "providers", None) is not None:
        results.append(
            _validation_result(
                "Providers", lambda: _validate_selected_providers(args.providers)
            )
        )

    if args.auth:
        results.append(
            _validation_result("Authentication", lambda: _cmd_validate_auth(args))
        )

    if not results:
        checks = _diagnostic_checks(args)
        if not args.quiet:
            _print_diagnostic_checks(checks)
        return 1 if any(check.status == "ERROR" for check in checks) else 0

    if not args.quiet:
        _print_validation_summary(results)
    return 0 if all(result.succeeded for result in results) else 1


def _package_version(distribution_name: str) -> str:
    try:
        return importlib_metadata.version(distribution_name)
    except importlib_metadata.PackageNotFoundError:
        return "unknown"


def _module_available(module_name: str) -> bool:
    try:
        return importlib_util.find_spec(module_name) is not None
    except ModuleNotFoundError:
        return False


def _diagnostic_dependency_checks() -> list[DiagnosticCheck]:
    dependencies = [
        ("aws", "boto3", "boto3"),
        ("azure", "azure.identity", "azure-identity"),
        ("gcp", "google.auth", "google-auth"),
        ("github", "github", "PyGithub"),
    ]

    checks: list[DiagnosticCheck] = []
    for provider_name, module_name, package_name in dependencies:
        if _module_available(module_name):
            checks.append(
                DiagnosticCheck(
                    section="Optional Dependencies",
                    status="OK",
                    label=provider_name,
                    detail=f"{package_name} importable",
                )
            )
        else:
            checks.append(
                DiagnosticCheck(
                    section="Optional Dependencies",
                    status="WARN",
                    label=provider_name,
                    detail=f"{package_name} not importable",
                )
            )

    return checks


def _diagnostic_discovery_checks() -> list[DiagnosticCheck]:
    checks: list[DiagnosticCheck] = []

    provider_discovery = discover_providers()
    provider_names = ", ".join(
        descriptor.name for descriptor in provider_discovery.providers
    )
    checks.append(
        DiagnosticCheck(
            section="Discovery",
            status="OK",
            label="providers",
            detail=provider_names or "none",
        )
    )
    for issue in provider_discovery.issues:
        checks.append(
            DiagnosticCheck(
                section="Discovery",
                status="WARN",
                label=f"provider {issue.name}",
                detail=f"{issue.source}: {issue.error}",
            )
        )

    task_discovery = discover_tasks()
    checks.append(
        DiagnosticCheck(
            section="Discovery",
            status="OK" if not task_discovery.issues else "WARN",
            label="tasks",
            detail=f"{len(task_discovery.tasks)} discovered",
        )
    )
    for issue in task_discovery.issues:
        checks.append(
            DiagnosticCheck(
                section="Discovery",
                status="WARN",
                label=f"task {issue.name}",
                detail=f"{issue.source}: {issue.error}",
            )
        )

    processor_discovery = discover_processors()
    checks.append(
        DiagnosticCheck(
            section="Discovery",
            status="OK" if not processor_discovery.issues else "WARN",
            label="processors",
            detail=f"{len(processor_discovery.processors)} discovered",
        )
    )
    for issue in processor_discovery.issues:
        checks.append(
            DiagnosticCheck(
                section="Discovery",
                status="WARN",
                label=f"processor {issue.name}",
                detail=f"{issue.source}: {issue.error}",
            )
        )

    return checks


def _env_present(name: str) -> bool:
    return bool(os.environ.get(name))


def _diagnostic_auth_source_checks() -> list[DiagnosticCheck]:
    home = Path.home()
    aws_config_path = home / ".aws" / "config"
    checks = [
        DiagnosticCheck(
            section="Auth Sources",
            status="OK" if _env_present("AWS_PROFILE") else "WARN",
            label="AWS_PROFILE",
            detail="set" if _env_present("AWS_PROFILE") else "not set",
        ),
        DiagnosticCheck(
            section="Auth Sources",
            status="OK" if aws_config_path.exists() else "WARN",
            label="AWS config",
            detail=str(aws_config_path) if aws_config_path.exists() else "not found",
        ),
        DiagnosticCheck(
            section="Auth Sources",
            status="OK" if _env_present("GOOGLE_APPLICATION_CREDENTIALS") else "WARN",
            label="GOOGLE_APPLICATION_CREDENTIALS",
            detail=(
                "set" if _env_present("GOOGLE_APPLICATION_CREDENTIALS") else "not set"
            ),
        ),
        DiagnosticCheck(
            section="Auth Sources",
            status="OK" if _env_present("GITHUB_TOKEN") else "WARN",
            label="GITHUB_TOKEN",
            detail="set" if _env_present("GITHUB_TOKEN") else "not set",
        ),
        DiagnosticCheck(
            section="Auth Sources",
            status="OK" if _env_present("ANVIL_GITHUB_CONFIG") else "WARN",
            label="ANVIL_GITHUB_CONFIG",
            detail="set" if _env_present("ANVIL_GITHUB_CONFIG") else "not set",
        ),
    ]

    for executable_name in ("aws", "az", "gcloud", "gh"):
        executable_path = shutil.which(executable_name)
        checks.append(
            DiagnosticCheck(
                section="Auth Sources",
                status="OK" if executable_path else "WARN",
                label=f"{executable_name} executable",
                detail=executable_path or "not found on PATH",
            )
        )

    return checks


def _diagnostic_path_checks() -> list[DiagnosticCheck]:
    cwd = Path.cwd()
    results_dir = cwd / "results"
    return [
        DiagnosticCheck(
            section="Environment",
            status="OK",
            label="Python",
            detail=sys.version.split()[0],
        ),
        DiagnosticCheck(
            section="Environment",
            status="OK",
            label="Anvil",
            detail=_package_version("anvil"),
        ),
        DiagnosticCheck(
            section="Environment",
            status="OK",
            label="Working directory",
            detail=str(cwd),
        ),
        DiagnosticCheck(
            section="Environment",
            status="OK" if results_dir.exists() else "WARN",
            label="Results directory",
            detail=str(results_dir) if results_dir.exists() else "not created yet",
        ),
    ]


def _diagnostic_config_checks(args: argparse.Namespace) -> list[DiagnosticCheck]:
    checks: list[DiagnosticCheck] = []
    if args.config_file is None:
        return checks

    for config_file in args.config_file:
        try:
            loaded_config = _load_targets_from_config_file(config_file)
            _validate_cli_overrides(loaded_config=loaded_config, args=args)
        except Exception as error:
            checks.append(
                DiagnosticCheck(
                    section="Config",
                    status="ERROR",
                    label=str(config_file),
                    detail=str(error),
                )
            )
            continue

        provider_names = sorted({target.provider for target in loaded_config.targets})
        checks.append(
            DiagnosticCheck(
                section="Config",
                status="OK",
                label=str(config_file),
                detail=(
                    f"{len(loaded_config.targets)} target(s); "
                    f"providers: {', '.join(provider_names) or 'none'}"
                ),
            )
        )

    return checks


def _diagnostic_checks(args: argparse.Namespace) -> list[DiagnosticCheck]:
    return [
        *_diagnostic_path_checks(),
        *_diagnostic_dependency_checks(),
        *_diagnostic_discovery_checks(),
        *_diagnostic_auth_source_checks(),
        *_diagnostic_config_checks(args),
    ]


def _print_diagnostic_checks(checks: list[DiagnosticCheck]) -> None:
    print("Anvil Validation Diagnostics")

    current_section: str | None = None
    for check in checks:
        if check.section != current_section:
            print()
            print(check.section)
            current_section = check.section

        detail = f" {check.detail}" if check.detail else ""
        print(f"[{check.status}] {check.label}{detail}")


def _cmd_graph(args: argparse.Namespace) -> int:

    for config_file in args.config_file:
        loaded_config = _load_targets_from_config_file(config_file)
        _validate_cli_overrides(loaded_config=loaded_config, args=args)
        render_graph(targets=loaded_config.targets, output_json=args.json)

    return 0


def _result_filters_from_args(args: argparse.Namespace) -> ResultFilters:
    return ResultFilters(
        record_type=args.type,
        status=args.status,
        target=args.target,
        entity=args.entity,
        region=args.region,
        task=args.task,
    )


def _load_filtered_result_records(
    args: argparse.Namespace, *, limit: int | None = None
) -> list[dict[str, object]]:
    return query_result_records(
        results_dir=Path.cwd() / "results",
        files=args.results_file,
        filters=_result_filters_from_args(args),
        limit=limit,
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


def _emit_result_records(
    args: argparse.Namespace, records: list[dict[str, object]]
) -> None:
    fields = parse_fields(args.fields)
    _print_query_payload(
        records, fields=fields, output_json=args.json, output_jsonl=args.jsonl
    )


def _validate_results_rerun_args(
    args: argparse.Namespace, *, parser: argparse.ArgumentParser | None = None
) -> None:
    rejected_flags: list[str] = []
    if getattr(args, "type", None) is not None:
        rejected_flags.append("--type")
    if getattr(args, "fields", None) is not None:
        rejected_flags.append("--fields")
    if getattr(args, "limit", None) is not None:
        rejected_flags.append("--limit")
    if getattr(args, "json", False):
        rejected_flags.append("--json")
    if getattr(args, "jsonl", False):
        rejected_flags.append("--jsonl")
    if getattr(args, "results_dir", None) is not None:
        rejected_flags.append("--results-dir")
    if getattr(args, "processor", None) is not None:
        rejected_flags.append("--processor")
    if getattr(args, "output", None) is not None:
        rejected_flags.append("--output")
    if rejected_flags:
        rejected = ", ".join(rejected_flags)
        message = f"{rejected} cannot be used with --rerun"
        if parser is not None:
            parser.error(message)
        raise ValueError(message)


def _validate_results_processor_args(
    args: argparse.Namespace, *, parser: argparse.ArgumentParser | None = None
) -> None:
    rejected_flags: list[str] = []
    if getattr(args, "rerun", False):
        rejected_flags.append("--rerun")
    if getattr(args, "results_file", None) is not None:
        rejected_flags.append("--results-file")
    if getattr(args, "type", None) is not None:
        rejected_flags.append("--type")
    if getattr(args, "status", None) is not None:
        rejected_flags.append("--status")
    if getattr(args, "target", None) is not None:
        rejected_flags.append("--target")
    if getattr(args, "entity", None) is not None:
        rejected_flags.append("--entity")
    if getattr(args, "region", None) is not None:
        rejected_flags.append("--region")
    if getattr(args, "task", None) is not None:
        rejected_flags.append("--task")
    if getattr(args, "fields", None) is not None:
        rejected_flags.append("--fields")
    if getattr(args, "limit", None) is not None:
        rejected_flags.append("--limit")
    if getattr(args, "json", False):
        rejected_flags.append("--json")
    if getattr(args, "jsonl", False):
        rejected_flags.append("--jsonl")
    if getattr(args, "benchmark", False):
        rejected_flags.append("--benchmark")
    if getattr(args, "dry_run", None) is not None:
        rejected_flags.append("--dry-run")

    if getattr(args, "results_dir", None) is None:
        message = "--results-dir is required with --processor"
        if parser is not None:
            parser.error(message)
        raise ValueError(message)

    if rejected_flags:
        rejected = ", ".join(rejected_flags)
        message = f"{rejected} cannot be used with --processor"
        if parser is not None:
            parser.error(message)
        raise ValueError(message)


def _cmd_results(args: argparse.Namespace) -> int:
    if getattr(args, "processor", None) is not None:
        _validate_results_processor_args(args)
        return _cmd_results_processor(args)

    if getattr(args, "results_dir", None) is not None:
        raise ValueError("--results-dir requires --processor")
    if getattr(args, "output", None) is not None:
        raise ValueError("--output requires --processor")

    if args.rerun:
        _validate_results_rerun_args(args)
        return _cmd_results_rerun(args)

    records = _load_filtered_result_records(args, limit=args.limit)
    _emit_result_records(args, records)
    return 0


def _cmd_results_processor(args: argparse.Namespace) -> int:
    context = load_completed_run_context(results_dir=args.results_dir)
    output = (
        str(resolve_processor_output_path(run_dir=context.run_dir, output=args.output))
        if args.output is not None
        else None
    )
    specs = [ProcessorSpec(processor=args.processor, output=output, metadata={})]
    run_processors(specs=specs, context=context)
    return 0


def _cmd_results_rerun(args: argparse.Namespace) -> int:
    records = _load_filtered_result_records(args)
    failures = failure_records(records)
    if not failures:
        print("No matching failures to rerun.")
        return 0

    records_by_config = config_file_for_failure_records(failures=failures)

    exit_code = 0
    for config_file, config_failures in records_by_config.items():
        loaded_config = _load_targets_from_config_file(config_file)
        rerun_targets = build_rerun_targets(
            loaded_config=loaded_config, failures=config_failures
        )
        if not rerun_targets:
            print(f"No configured targets matched failures for {config_file}.")
            continue

        engine_result: EngineResult = run_multiple_targets(
            targets=rerun_targets,
            max_parallel_targets=loaded_config.max_parallel_targets,
            cli_dry_run=args.dry_run,
            cli_include=None,
            cli_exclude=None,
            benchmark_enabled=getattr(args, "benchmark", False),
        )
        written_results = _write_run_results(
            config_file=config_file, engine_result=engine_result
        )
        if _summary_has_queryable_failures(written_results.summary):
            _print_failure_followups(results_file=written_results.jsonl_path)
        if engine_result.state is not EngineState.COMPLETED_SUCCESS:
            exit_code = 1

    return exit_code


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
        "--status", help="Filter by status: success, error, interrupted, or failed"
    )
    parser.add_argument(
        "--type", choices=["entity", "task"], help="Filter by result record type"
    )
    parser.add_argument("--target", help="Filter by target name")
    parser.add_argument("--entity", help="Filter by entity ID or entity name")
    parser.add_argument("--region", help="Filter by AWS region")
    parser.add_argument("--task", help="Filter by task name")
    parser.add_argument(
        "--fields", help="Comma-separated result fields to include in output"
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


def _validate_list_args(
    args: argparse.Namespace, *, parser: argparse.ArgumentParser | None = None
) -> None:
    selectors = [
        flag_name
        for flag_name in ("tasks", "processors", "providers")
        if (
            getattr(args, flag_name, False)
            if flag_name == "providers"
            else getattr(args, flag_name, None) is not None
        )
    ]
    if len(selectors) > 1:
        selector_display = ", ".join(f"--{selector}" for selector in selectors)
        message = f"{selector_display} cannot be used together"
        if parser is not None:
            parser.error(message)
        raise ValueError(message)

    detail = getattr(args, "detail", False)
    selected_names = args.tasks if args.tasks is not None else args.processors

    if detail and args.providers:
        message = "--detail cannot be used with --providers"
        if parser is not None:
            parser.error(message)
        raise ValueError(message)

    if detail and selected_names is not None and len(selected_names) != 1:
        message = "--detail requires exactly one task or processor name"
        if parser is not None:
            parser.error(message)
        raise ValueError(message)

    if not detail and selected_names:
        message = "task and processor names require --detail"
        if parser is not None:
            parser.error(message)
        raise ValueError(message)

    if not selectors:
        message = "One of --tasks, --processors, or --providers is required."
        if parser is not None:
            parser.error(message)
        raise ValueError(message)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Anvil config-driven provider target processing runner"
    )

    subparsers = parser.add_subparsers(dest="command", required=False)

    run_parser = subparsers.add_parser(
        "run", help="Execute tasks from an organization or provider target config"
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

    list_parser = subparsers.add_parser(
        "list", help="List discovered tasks or processors"
    )
    _add_log_level_arg(list_parser)
    list_parser.add_argument(
        "--tasks",
        nargs="*",
        default=None,
        metavar="TASK",
        help="List available tasks, or show detail for one task with --detail",
    )
    list_parser.add_argument(
        "--processors",
        nargs="*",
        default=None,
        metavar="PROCESSOR",
        help=(
            "List available processors, or show detail for one processor with --detail"
        ),
    )
    list_parser.add_argument(
        "--providers", action="store_true", help="List available providers"
    )
    list_parser.add_argument(
        "--detail",
        action="store_true",
        help="Show detailed documentation for one task or processor",
    )
    list_parser.set_defaults(func=_cmd_list)

    validate_parser = subparsers.add_parser(
        "validate", help="Validate tasks, processors, providers, and authentication"
    )
    _add_log_level_arg(validate_parser)
    validate_parser.add_argument(
        "--tasks",
        nargs="*",
        default=None,
        metavar="TASK",
        help="Validate all discovered tasks, or only the named tasks when provided",
    )
    validate_parser.add_argument(
        "--processors",
        nargs="*",
        default=None,
        metavar="PROCESSOR",
        help=(
            "Validate all discovered processors, or only the named processors "
            "when provided"
        ),
    )
    validate_parser.add_argument(
        "--providers",
        nargs="*",
        default=None,
        metavar="PROVIDER",
        help=(
            "Validate all discovered providers, or only the named providers "
            "when provided"
        ),
    )
    validate_parser.add_argument(
        "--auth",
        action="store_true",
        help="Validate authentication for configured runnable targets",
    )
    validate_parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress validation output and rely on the exit code",
    )
    validate_parser.add_argument(
        "--config-file",
        nargs="+",
        type=Path,
        help=(
            "Path(s) to YAML config file(s) to validate offline, or to use with "
            "--auth"
        ),
    )
    validate_group = validate_parser.add_mutually_exclusive_group()
    validate_group.add_argument(
        "--include",
        nargs="+",
        help=(
            "Narrow authentication targets to specific provider target IDs; "
            "mutually exclusive with --exclude"
        ),
    )
    validate_group.add_argument(
        "--exclude",
        nargs="+",
        help=(
            "Discovery-config only: exclude discovered provider target IDs; "
            "mutually exclusive with --include"
        ),
    )
    validate_parser.set_defaults(func=_cmd_validate)

    graph_parser = subparsers.add_parser(
        "graph", help="Show task dependency graph for configured provider targets"
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
    _add_results_query_args(results_parser)
    results_parser.add_argument(
        "--rerun",
        action="store_true",
        help="Rerun failed targets narrowed to failed entities, regions, and tasks",
    )
    results_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=None,
        help="With --rerun, run without making changes",
    )
    results_parser.add_argument(
        "--benchmark",
        action="store_true",
        help=(
            "With --rerun, "
            "Include diagnostic phase timings in result JSON. "
            "This can significantly increase output size."
        ),
    )
    results_parser.add_argument(
        "--results-dir",
        type=Path,
        help="Completed results run directory to process with --processor",
    )
    results_parser.add_argument(
        "--processor", help="Run a processor against a completed results directory"
    )
    results_parser.add_argument(
        "--output", help="Optional processor-owned output destination"
    )
    results_parser.set_defaults(func=_cmd_results)

    args = parser.parse_args()

    if not args.command:
        parser.error("the following arguments are required: command")
    if args.command == "list":
        _validate_list_args(args, parser=list_parser)
    if args.command == "results" and args.rerun:
        _validate_results_rerun_args(args, parser=results_parser)
    if args.command == "results" and args.processor is not None:
        _validate_results_processor_args(args, parser=results_parser)
    if (
        args.command == "results"
        and args.processor is None
        and args.results_dir is not None
    ):
        results_parser.error("--results-dir requires --processor")
    if args.command == "results" and args.processor is None and args.output is not None:
        results_parser.error("--output requires --processor")
    if args.command == "validate" and args.auth and args.config_file is None:
        validate_parser.error("--config-file is required with --auth")
    log_level = (
        "CRITICAL"
        if args.command == "validate" and getattr(args, "quiet", False)
        else args.log_level
    )
    logging.basicConfig(
        level=getattr(logging, log_level),
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
