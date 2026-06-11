from __future__ import annotations

import importlib
import logging
import pkgutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from importlib.metadata import EntryPoint, entry_points
from types import ModuleType


@dataclass(frozen=True, slots=True)
class DiscoveredModule:
    """Module discovered from a stock package or plugin package."""

    name: str
    source: str
    load: Callable[[], Callable]


@dataclass(frozen=True, slots=True)
class DiscoveryIssue:
    """Problem encountered while discovering plugin modules."""

    name: str
    source: str
    error: str


@dataclass(frozen=True, slots=True)
class ModuleDiscoveryResult:
    """Discovered modules and non-fatal discovery issues."""

    modules: list[DiscoveredModule]
    issues: list[DiscoveryIssue]


def _validate_run_callable(
    *,
    module: ModuleType,
    name: str,
    kind: str,
    source_label: str,
    error_type: type[RuntimeError],
) -> Callable:
    run = getattr(module, "run", None)
    if not callable(run):
        raise error_type(
            f"{source_label} {kind} '{name}' must define a callable run(...)"
        )

    return run


def load_stock_callable(
    *, name: str, kind: str, package_name: str, error_type: type[RuntimeError]
) -> Callable:
    """Load a named stock module and return its run callable."""
    module_name = f"{package_name}.{name}"

    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as error:
        raise error_type(f"Core {kind} '{name}' not found") from error

    return _validate_run_callable(
        module=module, name=name, kind=kind, source_label="Core", error_type=error_type
    )


def load_plugin_callable(
    *,
    name: str,
    kind: str,
    entry_point_group: str,
    error_type: type[RuntimeError],
    logger: logging.Logger,
    import_failure_log_label: str,
    import_issue_log_label: str,
) -> Callable:
    """Load a named plugin module from registered entry points."""
    discovered_plugins: list[str] = []
    import_failures: list[str] = []

    for entry_point in entry_points(group=entry_point_group):
        discovered_plugins.append(entry_point.name)

        try:
            pkg = importlib.import_module(entry_point.value)
        except Exception as exc:
            logger.debug(
                f"Failed importing {import_failure_log_label} '{entry_point.value}' "
                f"(entry point '{entry_point.name}'): {exc}"
            )
            import_failures.append(f"{entry_point.name}: package import failed ({exc})")
            continue

        module_name = f"{pkg.__name__}.{name}"
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name == module_name:
                continue
            raise error_type(
                f"Plugin {kind} '{name}' in plugin "
                f"'{entry_point.name}' failed during import: {exc}"
            ) from exc
        except Exception as exc:
            raise error_type(
                f"Plugin {kind} '{name}' in plugin "
                f"'{entry_point.name}' failed during import: {exc}"
            ) from exc

        run = getattr(module, "run", None)
        if not callable(run):
            raise error_type(
                f"Plugin {kind} '{name}' in plugin "
                f"'{entry_point.name}' must define callable run(...)"
            )

        return run

    if import_failures:
        logger.debug(
            f"Plugin import issues encountered while resolving "
            f"'{import_issue_log_label}': {import_failures}"
        )

    raise error_type(
        f"Plugin {kind} '{name}' not found in registered entry points: "
        f"{discovered_plugins}"
    )


def iter_stock_modules(
    *, package_name: str, load: Callable[[str], Callable]
) -> Iterable[DiscoveredModule]:
    """Yield public modules from a stock package without importing each module."""
    package = importlib.import_module(package_name)

    for module_info in pkgutil.iter_modules(package.__path__):
        name = module_info.name
        if name.startswith("_"):
            continue

        yield DiscoveredModule(name=name, source="stock", load=lambda n=name: load(n))


def plugin_source(entry_point: EntryPoint) -> str:
    """Return the display source for a plugin entry point."""
    return (
        f"plugin: {entry_point.dist.name}"
        if entry_point.dist is not None
        else "plugin (unpackaged)"
    )


def discover_plugin_modules(
    *,
    entry_point_group: str,
    load: Callable[[str], Callable],
    logger: logging.Logger,
    skip_log_label: str,
) -> ModuleDiscoveryResult:
    """Discover plugin modules and capture packages that cannot be imported."""
    modules: list[DiscoveredModule] = []
    issues: list[DiscoveryIssue] = []

    for entry_point in entry_points(group=entry_point_group):
        try:
            pkg = importlib.import_module(entry_point.value)
        except Exception as exc:
            logger.debug(
                f"Skipping {skip_log_label} '{entry_point.name}' due to import error: "
                f"{exc}"
            )
            issues.append(
                DiscoveryIssue(
                    name=entry_point.name,
                    source=plugin_source(entry_point),
                    error=f"package import failed ({exc})",
                )
            )
            continue

        source = plugin_source(entry_point)
        for module_info in pkgutil.iter_modules(pkg.__path__):
            name = module_info.name
            if name.startswith("_"):
                continue

            modules.append(
                DiscoveredModule(name=name, source=source, load=lambda n=name: load(n))
            )

    return ModuleDiscoveryResult(modules=modules, issues=issues)
