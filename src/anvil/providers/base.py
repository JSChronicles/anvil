from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Protocol

from anvil.descriptors import TargetDescriptor
from anvil.execution_context import ExecutionContext
from anvil.results import ExecutionStatus


@dataclass(frozen=True, slots=True)
class ProviderMetadata:
    """Public metadata for a provider implementation."""

    name: str
    display_name: str
    description: str | None = None
    default_regions: tuple[str, ...] = ()
    supported_task_scopes: frozenset[str] = frozenset({"region"})


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
    metadata: dict[str, object] = field(default_factory=dict)
    provider_data: object | None = None


@dataclass(frozen=True, slots=True)
class ProviderExecutionPlan:
    """Execution targets and provider-owned scheduling metadata."""

    execution_targets: list[ExecutionTarget]
    exclusive_execution_key: object | None = None
    benchmark: dict[str, object] | None = None


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

    def auth_cache_key(self, target: TargetDescriptor) -> object | None:
        """Return a cache key for duplicate auth checks."""

    def auth_check(self, target: TargetDescriptor) -> ProviderAuthResult:
        """Run provider authentication checks."""

    def discover_regions(self, target: TargetDescriptor) -> list[ProviderRegion]:
        """Discover provider regions or locations."""

    def resolve_execution_targets(
        self,
        *,
        target: TargetDescriptor,
        regions: list[str],
        include: list[str] | None,
        exclude: list[str] | None,
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
        "auth_cache_key": {"target"},
        "auth_check": {"target"},
        "discover_regions": {"target"},
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
