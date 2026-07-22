from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from importlib.metadata import EntryPoint, entry_points

from anvil._components import (
    ComponentCatalog,
    ComponentKind,
    ComponentOrigin,
    ComponentResolver,
    ComponentSource,
    DiscoveryIssue as CatalogDiscoveryIssue,
    PackageComponentSource,
    source_from_entry_point,
)
from anvil.descriptors import ConfigBranch, TargetDescriptor
from anvil.results import TargetResult

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
    run_on_failure: bool = False


@dataclass(frozen=True, slots=True)
class ProcessorDescriptor:
    """Discovered processor and lazy loader for its run callable."""

    name: str
    load: Callable[[], Callable]
    source: str


@dataclass(frozen=True, slots=True)
class ProcessorDiscoveryResult:
    """Discovered processors and non-fatal discovery issues."""

    processors: list[ProcessorDescriptor]
    issues: list[CatalogDiscoveryIssue]


@dataclass(frozen=True, slots=True)
class ProcessorRunContext:
    """Completed run data passed to post-run processors."""

    config_branch: ConfigBranch
    run_dir: Path
    summary_path: Path
    summary: dict[str, object]
    target_result_paths: dict[str, Path]
    target_name: str | None = None
    target_result: TargetResult | dict[str, object] | None = None
    target_result_path: Path | None = None
    target_metadata: dict[str, object] = field(default_factory=dict)
    target_results: Sequence[TargetResult | dict[str, object]] = field(
        default_factory=list
    )


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
        raw_metadata = raw_spec.get("metadata", {})
        if not isinstance(raw_metadata, dict):
            raise ProcessorConfigError("processor metadata must be a mapping")

        metadata: dict[str, object] = {}
        for key, value in raw_metadata.items():
            if not isinstance(key, str):
                raise ProcessorConfigError("processor metadata keys must be strings")
            metadata[key] = value

        output = raw_spec.get("output")
        run_on_failure = raw_spec.get("run_on_failure", False)
        if not isinstance(run_on_failure, bool):
            raise ProcessorConfigError("processor run_on_failure must be a boolean")

        specs.append(
            ProcessorSpec(
                processor=str(raw_spec["processor"]),
                output=output if isinstance(output, str) else None,
                metadata=metadata,
                run_on_failure=run_on_failure,
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
        part for part in output_path.parts if part not in {output_path.anchor, ".", ""}
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
    targets: list[TargetDescriptor],
) -> dict[str, tuple[TargetDescriptor, list[ProcessorSpec]]]:
    processors_by_name: dict[str, tuple[TargetDescriptor, list[ProcessorSpec]]] = {}

    for target in targets:
        processors_by_name[target.name] = (
            target,
            _parse_processor_specs(target.post_run),
        )

    return processors_by_name


def run_configured_post_processors(
    *,
    config_branch: ConfigBranch,
    targets: list[TargetDescriptor],
    target_results: list[TargetResult],
    run_dir: Path,
    summary_path: Path,
    summary: dict[str, object],
    target_result_paths: dict[str, Path],
) -> None:
    """Run configured target-level processors for successful target results."""
    processors_by_name = _processor_specs_by_target_name(targets)
    reserved_output_paths: set[Path] = set()

    for target_result in target_results:
        target_entry = processors_by_name.get(target_result.target_name)
        if target_entry is None:
            continue

        target, specs = target_entry
        if not specs:
            continue

        if target_result.has_failures:
            specs = [spec for spec in specs if spec.run_on_failure]
            if not specs:
                continue

        context = ProcessorRunContext(
            config_branch=config_branch,
            run_dir=run_dir,
            summary_path=summary_path,
            summary=summary,
            target_result_paths=target_result_paths,
            target_name=target_result.target_name,
            target_result=target_result,
            target_result_path=target_result_paths.get(target_result.target_name),
            target_metadata=dict(target.metadata),
            target_results=[target_result],
        )

        resolved_specs: list[ProcessorSpec] = []
        for spec in specs:
            output = (
                str(
                    resolve_processor_output_path(
                        run_dir=run_dir,
                        output=spec.output,
                        target_name=target_result.target_name,
                        reserved_paths=reserved_output_paths,
                    )
                )
                if spec.output is not None
                else None
            )
            resolved_specs.append(
                ProcessorSpec(
                    processor=spec.processor,
                    output=output,
                    metadata=spec.metadata,
                    run_on_failure=spec.run_on_failure,
                )
            )

        run_processors(specs=resolved_specs, context=context)


def _load_processor_from_package(
    package_name: str, processor_name: str, source: ComponentSource
) -> Callable:
    """Import and validate one selected processor module."""

    import importlib

    module_name = f"{package_name}.{processor_name}"
    try:
        module = importlib.import_module(module_name)
    except Exception as error:
        raise ProcessorConfigError(
            f"Processor '{processor_name}' ({source}) failed during import: {error}"
        ) from error
    run = getattr(module, "run", None)
    if not callable(run):
        raise ProcessorConfigError(
            f"Processor '{processor_name}' ({source}) must define callable run(...)"
        )
    return run


def _public_processor_descriptor(descriptor) -> ProcessorDescriptor:
    return ProcessorDescriptor(
        name=descriptor.name, load=descriptor.load, source=str(descriptor.source)
    )


@lru_cache(maxsize=16)
def _processor_catalog_for_entry_points(
    plugin_entry_points: tuple[EntryPoint, ...],
) -> ComponentCatalog[Callable]:
    descriptors = []
    issues: list[CatalogDiscoveryIssue] = []
    stock_source = ComponentSource(
        origin=ComponentOrigin.STOCK, package="anvil.processors", label="stock"
    )
    stock_descriptors, stock_issues = PackageComponentSource(
        kind=ComponentKind.PROCESSOR,
        package_name="anvil.processors",
        source=stock_source,
        component_loader=_load_processor_from_package,
    ).discover()
    descriptors.extend(stock_descriptors)
    issues.extend(stock_issues)

    for entry_point in plugin_entry_points:
        package_name = entry_point.value.split(":", maxsplit=1)[0]
        source = source_from_entry_point(entry_point=entry_point, package=package_name)
        plugin_descriptors, plugin_issues = PackageComponentSource(
            kind=ComponentKind.PROCESSOR,
            package_name=package_name,
            source=source,
            component_loader=_load_processor_from_package,
        ).discover(issue_name=entry_point.name)
        descriptors.extend(plugin_descriptors)
        issues.extend(plugin_issues)

    return ComponentCatalog.build(descriptors, issues)


def _processor_catalog() -> ComponentCatalog[Callable]:
    return _processor_catalog_for_entry_points(
        tuple(entry_points(group=PROCESSOR_ENTRY_POINT_GROUP))
    )


# ============================================================================
# Public API
# ============================================================================


@lru_cache(maxsize=128)
def load_processor_callable(processor_name: str) -> Callable:
    """Resolve a processor run callable by name."""
    return ComponentResolver(
        kind=ComponentKind.PROCESSOR,
        catalog=_processor_catalog(),
        error_type=ProcessorConfigError,
    ).load(processor_name)


def discover_processors() -> ProcessorDiscoveryResult:
    """Discover processors and report plugin packages that cannot be inspected."""
    catalog = _processor_catalog()
    return ProcessorDiscoveryResult(
        processors=[
            _public_processor_descriptor(descriptor)
            for descriptor in catalog.descriptors
        ],
        issues=list(catalog.issues),
    )


def list_processors() -> list[ProcessorDescriptor]:
    """Return processors sorted by source and name."""
    return sorted(
        discover_processors().processors,
        key=lambda processor: (processor.source, processor.name),
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
        results.append(run(context=context, output=spec.output, metadata=spec.metadata))

    return results


def load_completed_run_context(*, results_dir: Path) -> ProcessorRunContext:
    """Build a processor context from a completed current-shape results directory."""
    summary_path = results_dir / "summary.json"
    target_dir = results_dir / "targets"

    if not target_dir.exists():
        raise FileNotFoundError(
            f"targets directory not found in results dir: {results_dir}"
        )

    summary = _load_json_object(summary_path) if summary_path.exists() else {}
    target_results: list[dict[str, object]] = []
    target_result_paths: dict[str, Path] = {}

    for result_path in sorted(target_dir.glob("*.json")):
        target_result = _load_json_object(result_path)
        target_name = target_result.get("target")
        if not isinstance(target_name, str) or not target_name:
            raise ValueError(
                f"Expected target result with string 'target' in {result_path}"
            )

        target_results.append(target_result)
        target_result_paths[target_name] = result_path

    return ProcessorRunContext(
        config_branch=ConfigBranch.TARGETS,
        run_dir=results_dir,
        summary_path=summary_path,
        summary=summary,
        target_result_paths=target_result_paths,
        target_results=target_results,
    )


def _load_json_object(path: Path) -> dict[str, object]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)

    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object in {path}")

    return {key: value for key, value in payload.items() if isinstance(key, str)}
