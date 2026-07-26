from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from anvil.descriptors import TargetDescriptor
from anvil.execution_context import ExecutionContext
from anvil.regions import ALL_REGION_SELECTOR, is_region_selector
from anvil.results import ExecutionStatus


@dataclass(frozen=True, slots=True)
class ProviderMetadata:
    """Public metadata for a provider implementation."""

    name: str
    display_name: str
    supported_task_scopes: frozenset[str]
    description: str | None = None
    default_regions: tuple[str, ...] = ()


def configured_or_default_regions(
    *, configured: list[str] | None, default: tuple[str, ...]
) -> list[str]:
    """Return configured regions or provider defaults when they were omitted."""

    if configured is None:
        return list(default)
    return list(configured)


def validate_resolved_regions(*, regions: list[str]) -> None:
    """Validate concrete regions at the provider-default resolution boundary."""

    if not regions:
        raise ValueError("regions must contain at least one region")
    if any(not isinstance(region, str) or not region.strip() for region in regions):
        raise ValueError("regions must contain only non-empty strings")
    if len(set(regions)) != len(regions):
        raise ValueError("regions must not contain duplicates")


def validate_string_options(
    *, target: TargetDescriptor, allowed_options: frozenset[str]
) -> None:
    """Validate a provider's supported string-valued options."""

    unknown_options = sorted(set(target.provider_options) - allowed_options)
    if unknown_options:
        unknown_display = ", ".join(unknown_options)
        allowed_display = ", ".join(sorted(allowed_options)) or "(none)"
        raise ValueError(
            f"Unsupported provider.options for provider '{target.provider}': "
            f"{unknown_display}. Supported options: {allowed_display}"
        )

    for option_name, option_value in target.provider_options.items():
        if option_value is None:
            continue
        if not isinstance(option_value, str) or not option_value.strip():
            raise ValueError(
                f"provider.options.{option_name} must be a non-empty string"
            )


def validate_region_selectors(
    *, target: TargetDescriptor, selectors_allowed: bool
) -> None:
    """Validate selector syntax according to a provider mode's capabilities."""

    configured_regions = target.regions or []
    if ALL_REGION_SELECTOR in configured_regions and configured_regions != [
        ALL_REGION_SELECTOR
    ]:
        raise ValueError("regions selector 'all' must be the only region value")

    selectors = [region for region in configured_regions if is_region_selector(region)]
    if selectors and not selectors_allowed:
        raise ValueError(
            f"provider '{target.provider}' mode '{target.mode}' requires explicit "
            f"region names; selectors are not allowed: {', '.join(selectors)}"
        )


def narrow_include(
    *, configured: list[str] | None, override: list[str] | None
) -> list[str] | None:
    """Narrow an explicit configured target set with a CLI include override."""

    if override is None:
        return configured
    if configured is None:
        return override
    allowed = set(configured)
    return [target_id for target_id in override if target_id in allowed]


@dataclass(frozen=True, slots=True)
class ProviderAuthResult:
    """Provider-neutral authentication check result."""

    status: ExecutionStatus
    source: str
    message: str | None = None
    remediation: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderRegion:
    """Provider-owned region or location description."""

    name: str
    available: bool = True
    status: str | None = None


@dataclass(frozen=True, slots=True)
class ExecutionTarget:
    """Provider-neutral unit that Anvil can execute."""

    id: str
    name: str
    type: str
    provider: str
    regions: list[str]
    metadata: dict[str, object] = field(default_factory=dict)
    provider_data: object | None = None


@dataclass(frozen=True, slots=True)
class ProviderExecutionPlan:
    """Execution targets and provider-owned scheduling metadata."""

    execution_targets: list[ExecutionTarget]


@dataclass(frozen=True, slots=True)
class ProviderPreparation:
    """Opaque provider preflight state and scheduler admission keys."""

    data: object | None = None
    exclusive_execution_keys: tuple[object, ...] = ()


class ProviderPreparationCache(Protocol):
    """Shared single-flight cache available during provider preparation."""

    def get_or_create(
        self, *, key: object, create: Callable[[], object]
    ) -> tuple[object, bool, bool]:
        """Return a cached or newly created preparation value."""


class ProviderExecutionRuntime(Protocol):
    """Provider-owned lifecycle state for one execution target."""

    def build_session(self, *, region: str) -> object:
        """Build a task session for a concrete provider region."""

    def record_region_outcome(
        self, *, region: str, duration_seconds: float, failed: bool, interrupted: bool
    ) -> None:
        """Record one region outcome for provider lifecycle decisions."""

    def close(self) -> None:
        """Release any provider-owned runtime resources."""


class Provider(Protocol):
    """Provider contract used by multi-cloud execution phases."""

    metadata: ProviderMetadata

    def validate_target(self, target: TargetDescriptor) -> None:
        """Validate provider-specific target options."""

    def resolve_target_filters(
        self,
        *,
        target: TargetDescriptor,
        include_override: list[str] | None,
        exclude_override: list[str] | None,
    ) -> tuple[list[str] | None, list[str] | None]:
        """Resolve provider-specific CLI filter semantics."""

    def auth_cache_key(self, target: TargetDescriptor) -> object | None:
        """Return a cache key for duplicate auth checks."""

    def auth_check(self, target: TargetDescriptor) -> ProviderAuthResult:
        """Run provider authentication checks."""

    def discover_regions(self, target: TargetDescriptor) -> list[ProviderRegion]:
        """Discover provider regions or locations."""

    def prepare_target(
        self,
        *,
        target: TargetDescriptor,
        context: ExecutionContext,
        include: list[str] | None,
        exclude: list[str] | None,
        cache: ProviderPreparationCache,
        benchmark: dict[str, object] | None,
    ) -> ProviderPreparation:
        """Prepare provider-owned state before scheduler admission."""

    def resolve_execution_targets(
        self,
        *,
        target: TargetDescriptor,
        regions: list[str],
        include: list[str] | None,
        exclude: list[str] | None,
        preparation: object | None = None,
    ) -> ProviderExecutionPlan:
        """Resolve one configured target into executable provider targets."""

    def prepare_execution_runtime(
        self,
        *,
        target: TargetDescriptor,
        execution_target: ExecutionTarget,
        context: ExecutionContext,
    ) -> ProviderExecutionRuntime:
        """Prepare lifecycle state for one execution target."""


def validate_provider_contract(provider: Provider) -> None:
    """Validate that a provider exposes the public provider contract."""

    metadata = getattr(provider, "metadata", None)
    if not isinstance(metadata, ProviderMetadata):
        raise TypeError("provider.metadata must be ProviderMetadata")

    if not metadata.name:
        raise ValueError("provider metadata name must not be empty")
    if not metadata.display_name:
        raise ValueError("provider metadata display_name must not be empty")

    required_methods = {
        "validate_target": {"target"},
        "resolve_target_filters": {"target", "include_override", "exclude_override"},
        "auth_cache_key": {"target"},
        "auth_check": {"target"},
        "discover_regions": {"target"},
        "prepare_target": {
            "target",
            "context",
            "include",
            "exclude",
            "cache",
            "benchmark",
        },
        "resolve_execution_targets": {"target", "regions", "include", "exclude"},
        "prepare_execution_runtime": {"target", "execution_target", "context"},
    }
    for method_name, required_parameters in required_methods.items():
        if not callable(getattr(provider, method_name, None)):
            raise TypeError(f"provider missing callable {method_name}()")

        signature = inspect.signature(getattr(provider, method_name))
        parameter_names = set(signature.parameters)
        missing_parameters = sorted(required_parameters - parameter_names)
        if missing_parameters:
            missing_display = ", ".join(missing_parameters)
            raise TypeError(
                f"provider {metadata.name} {method_name}() missing "
                f"parameter(s): {missing_display}"
            )
