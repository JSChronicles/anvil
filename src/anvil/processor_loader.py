from __future__ import annotations

import importlib
import json
import logging
import pkgutil
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from importlib.metadata import entry_points
from pathlib import Path

from anvil.descriptors import ConfigBranch
from anvil.result_query import JSONL_FILENAME

__LOGGER__ = logging.getLogger(__name__)

PROCESSOR_ENTRY_POINT_GROUP = "anvil.processors"

# ============================================================================
# Models
# ============================================================================


class ProcessorConfigError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ProcessorSpec:
    """Declarative processor configuration loaded from YAML."""

    processor: str
    output: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProcessorDescriptor:
    """Discovered processor and its source package."""

    name: str
    run: Callable
    source: str


@dataclass(frozen=True, slots=True)
class ProcessorRunContext:
    """Completed run data passed to post-run processors."""

    config_branch: ConfigBranch
    run_dir: Path
    summary_path: Path
    jsonl_path: Path
    summary: dict[str, object]
    target_result_paths: dict[str, Path]
    target_name: str | None = None
    target_result: object | None = None
    target_result_path: Path | None = None
    target_metadata: dict[str, object] = field(default_factory=dict)
    results_jsonl_records: list[dict[str, object]] = field(default_factory=list)
    target_results: list[object] = field(default_factory=list)


# ============================================================================
# Internal helpers
# ============================================================================


def _parse_processor_specs(
    raw_specs: list[dict[str, object]] | None,
) -> list[ProcessorSpec]:
    """Build processor specs from schema-validated config data."""
    if raw_specs is None:
        return []

    specs: list[ProcessorSpec] = []
    for raw_spec in raw_specs:
        metadata = raw_spec.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ProcessorConfigError("processor metadata must be a mapping")

        output = raw_spec.get("output")
        specs.append(
            ProcessorSpec(
                processor=str(raw_spec["processor"]),
                output=output if isinstance(output, str) else None,
                metadata=metadata,
            )
        )

    return specs


def _safe_output_filename(name: str) -> str:
    safe_name = "".join(
        character if character.isalnum() or character in {".", "-", "_"} else "_"
        for character in name
    )
    return safe_name.strip("._") or "target"


def _output_path_parts(output: str) -> list[str]:
    output_path = Path(output)
    parts = [
        part
        for part in output_path.parts
        if part not in {output_path.anchor, ".", ""}
    ]

    if parts and parts[0].lower() == "reports":
        parts = parts[1:]

    return parts


def _available_output_path(
    *, output_path: Path, reserved_paths: set[Path] | None = None
) -> Path:
    reserved_paths = reserved_paths if reserved_paths is not None else set()
    candidate = output_path
    suffix = 1

    while candidate.exists() or candidate in reserved_paths:
        candidate = output_path.with_name(
            f"{output_path.stem}-{suffix}{output_path.suffix}"
        )
        suffix += 1

    reserved_paths.add(candidate)
    return candidate


def resolve_processor_output_path(
    *,
    run_dir: Path,
    output: str,
    target_name: str | None = None,
    reserved_paths: set[Path] | None = None,
) -> Path:
    """Resolve a processor output under the run's reports directory."""
    parts = _output_path_parts(output)
    reports_dir = run_dir / "reports"

    if not parts:
        output_name = "output"
        output_dirs: list[str] = []
    else:
        output_name = parts[-1]
        output_dirs = parts[:-1]

    safe_output_name = _safe_output_filename(output_name)
    if target_name is not None:
        safe_output_name = f"{_safe_output_filename(target_name)}-{safe_output_name}"

    output_path = reports_dir.joinpath(
        *(_safe_output_filename(part) for part in output_dirs), safe_output_name
    )
    return _available_output_path(
        output_path=output_path, reserved_paths=reserved_paths
    )


def _processor_specs_by_target_name(
    targets: list[object],
) -> dict[str, tuple[object, list[ProcessorSpec]]]:
    processors_by_name: dict[str, tuple[object, list[ProcessorSpec]]] = {}

    for target in targets:
        target_name = getattr(target, "name", None)
        if not isinstance(target_name, str) or not target_name:
            continue

        raw_specs = getattr(target, "post_run", [])
        processors_by_name[target_name] = (
            target,
            _parse_processor_specs(raw_specs),
        )

    return processors_by_name


def resolve_target_processor_specs(
    *,
    specs: list[ProcessorSpec],
    run_dir: Path,
    target_name: str,
    reserved_paths: set[Path],
) -> list[ProcessorSpec]:
    """Resolve target processor output paths under run_dir/reports."""
    resolved_specs: list[ProcessorSpec] = []

    for spec in specs:
        output = (
            str(
                resolve_processor_output_path(
                    run_dir=run_dir,
                    output=spec.output,
                    target_name=target_name,
                    reserved_paths=reserved_paths,
                )
            )
            if spec.output is not None
            else None
        )
        resolved_specs.append(
            ProcessorSpec(
                processor=spec.processor, output=output, metadata=spec.metadata
            )
        )

    return resolved_specs


def run_configured_post_processors(
    *,
    config_branch: ConfigBranch,
    targets: list[object],
    target_results: list[object],
    run_dir: Path,
    summary_path: Path,
    jsonl_path: Path,
    summary: dict[str, object],
    target_result_paths: dict[str, Path],
) -> None:
    """Run configured target-level processors for successful target results."""
    processors_by_name = _processor_specs_by_target_name(targets)
    reserved_output_paths: set[Path] = set()

    for target_result in target_results:
        target_name = getattr(target_result, "target_name")
        target_entry = processors_by_name.get(target_name)
        if target_entry is None:
            continue

        target, specs = target_entry
        if not specs or getattr(target_result, "has_failures"):
            continue

        context = ProcessorRunContext(
            config_branch=config_branch,
            run_dir=run_dir,
            summary_path=summary_path,
            jsonl_path=jsonl_path,
            summary=summary,
            target_result_paths=target_result_paths,
            target_name=target_name,
            target_result=target_result,
            target_result_path=target_result_paths.get(target_name),
            target_metadata=dict(getattr(target, "metadata", {})),
            target_results=[target_result],
        )
        resolved_specs = resolve_target_processor_specs(
            specs=specs,
            run_dir=run_dir,
            target_name=target_name,
            reserved_paths=reserved_output_paths,
        )
        run_processors(specs=resolved_specs, context=context)


def _load_core_processor(processor_name: str) -> Callable:
    module_name = f"anvil.processors.{processor_name}"

    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as error:
        raise ProcessorConfigError(
            f"Core processor '{processor_name}' not found"
        ) from error

    run = getattr(module, "run", None)
    if not callable(run):
        raise ProcessorConfigError(
            f"Core processor '{processor_name}' must define a callable run(...)"
        )

    return run


def _load_plugin_processor(processor_name: str) -> Callable:
    eps = entry_points(group=PROCESSOR_ENTRY_POINT_GROUP)

    discovered_plugins: list[str] = []
    import_failures: list[str] = []

    for entry_point in eps:
        discovered_plugins.append(entry_point.name)

        # Import plugin package
        try:
            pkg = importlib.import_module(entry_point.value)
        except Exception as exc:
            __LOGGER__.debug(
                f"Failed importing processor plugin package '{entry_point.value}' "
                f"(entry point '{entry_point.name}'): {exc}"
            )
            import_failures.append(f"{entry_point.name}: package import failed ({exc})")
            continue

        # Try loading processor module inside plugin
        try:
            module = importlib.import_module(f"{pkg.__name__}.{processor_name}")
        except ModuleNotFoundError:
            # Plugin simply doesn't provide this processor
            continue
        except Exception as exc:
            raise ProcessorConfigError(
                f"Plugin processor '{processor_name}' in plugin "
                f"'{entry_point.name}' failed during import: {exc}"
            ) from exc

        run = getattr(module, "run", None)
        if not callable(run):
            raise ProcessorConfigError(
                f"Plugin processor '{processor_name}' in plugin "
                f"'{entry_point.name}' must define callable run(...)"
            )

        return run

    if import_failures:
        __LOGGER__.debug(
            f"Plugin import issues encountered while resolving '{processor_name}': "
            f"{import_failures}"
        )

    raise ProcessorConfigError(
        f"Plugin processor '{processor_name}' not found in registered entry points: "
        f"{discovered_plugins}"
    )


# ============================================================================
# Public API
# ============================================================================


@lru_cache(maxsize=128)
def load_processor_callable(processor_name: str) -> Callable:
    """Resolve a processor run callable by name."""
    try:
        return _load_core_processor(processor_name)
    except ProcessorConfigError:
        return _load_plugin_processor(processor_name)


def discover_processors() -> list[ProcessorDescriptor]:
    """Discover stock and plugin processors without executing them."""
    processors: list[ProcessorDescriptor] = []

    # Core processors
    import anvil.processors

    for module_info in pkgutil.iter_modules(anvil.processors.__path__):
        name = module_info.name
        if name.startswith("_"):
            continue

        processors.append(
            ProcessorDescriptor(
                name=name, run=lambda n=name: _load_core_processor(n), source="stock"
            )
        )

    # Plugin processors (package scan, no imports)
    for entry_point in entry_points(group=PROCESSOR_ENTRY_POINT_GROUP):
        try:
            pkg = importlib.import_module(entry_point.value)
        except Exception as exc:
            __LOGGER__.debug(
                f"Skipping processor plugin '{entry_point.name}' due to import error: "
                f"{exc}"
            )
            continue

        for module_info in pkgutil.iter_modules(pkg.__path__):
            name = module_info.name
            if name.startswith("_"):
                continue

            source = (
                f"plugin: {entry_point.dist.name}"
                if entry_point.dist is not None
                else "plugin (unpackaged)"
            )
            processors.append(
                ProcessorDescriptor(
                    name=name,
                    run=lambda n=name: _load_plugin_processor(n),
                    source=source,
                )
            )

    return processors


def list_processors() -> list[ProcessorDescriptor]:
    """Return processors sorted by source and name."""
    return sorted(
        discover_processors(), key=lambda processor: (processor.source, processor.name)
    )


def run_processors(
    *, specs: list[ProcessorSpec], context: ProcessorRunContext
) -> list[object]:
    """Run configured processors sequentially in declaration order."""
    results: list[object] = []

    for spec in specs:
        run = load_processor_callable(spec.processor)
        __LOGGER__.info(
            f"Running processor '{spec.processor}' for target "
            f"'{context.target_name or 'run'}'"
        )
        results.append(
            run(context=context, output=spec.output, metadata=spec.metadata)
        )

    return results


def load_historical_run_context(*, results_dir: Path) -> ProcessorRunContext:
    """Build a processor context from a historical results directory."""
    summary_path = results_dir / "summary.json"
    jsonl_path = results_dir / JSONL_FILENAME

    if not summary_path.exists():
        raise FileNotFoundError(f"summary.json not found in results dir: {results_dir}")
    if not jsonl_path.exists():
        raise FileNotFoundError(
            f"{JSONL_FILENAME} not found in results dir: {results_dir}"
        )

    summary = _load_json_object(summary_path)
    records = _load_jsonl_records(jsonl_path)
    target_results: list[dict[str, object]] = []
    target_result_paths: dict[str, Path] = {}
    config_branch = ConfigBranch.ORGANIZATIONS

    for branch, directory_name, target_key in (
        (ConfigBranch.ORGANIZATIONS, "organizations", "organization"),
        (ConfigBranch.ACCOUNTS, "account-groups", "account_group"),
    ):
        target_dir = results_dir / directory_name
        if not target_dir.exists():
            continue

        config_branch = branch
        for result_path in sorted(target_dir.glob("*.json")):
            target_result = _load_json_object(result_path)
            target_results.append(target_result)
            target_name = target_result.get(target_key)
            if isinstance(target_name, str) and target_name:
                target_result_paths[target_name] = result_path

    return ProcessorRunContext(
        config_branch=config_branch,
        run_dir=results_dir,
        summary_path=summary_path,
        jsonl_path=jsonl_path,
        summary=summary,
        target_result_paths=target_result_paths,
        results_jsonl_records=records,
        target_results=target_results,
    )


def _load_json_object(path: Path) -> dict[str, object]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)

    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object in {path}")

    return payload


def _load_jsonl_records(path: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            payload = json.loads(stripped)
            if not isinstance(payload, dict):
                raise ValueError(
                    f"Invalid JSONL in {path} on line {line_number}: expected object"
                )
            records.append(payload)

    return records
