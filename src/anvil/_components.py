"""Shared lazy component discovery and resolution primitives.

This module deliberately knows nothing about task, processor, or provider runtime
contracts. Component-specific loaders validate those contracts after selection.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from importlib.metadata import EntryPoint
from inspect import Parameter, signature
from types import MappingProxyType
from typing import Generic, TypeVar


T = TypeVar("T")


class ComponentKind(StrEnum):
    """Kinds of extension components understood by Anvil."""

    TASK = "task"
    PROCESSOR = "processor"
    PROVIDER = "provider"


class ComponentOrigin(StrEnum):
    """Origin of a discovered component."""

    STOCK = "stock"
    PLUGIN = "plugin"


@dataclass(frozen=True, slots=True)
class ComponentSource:
    """Structured origin information for a discovered component."""

    origin: ComponentOrigin
    package: str
    label: str
    distribution: str | None = None
    entry_point_group: str | None = None
    entry_point_name: str | None = None
    provider: str | None = None

    def __str__(self) -> str:
        return self.label


@dataclass(frozen=True, slots=True)
class ComponentDescriptor(Generic[T]):
    """A named component and a loader that imports only that component."""

    name: str
    source: ComponentSource
    load: Callable[[], T] = field(compare=False, hash=False, repr=False)


@dataclass(frozen=True, slots=True)
class DiscoveryIssue:
    """A non-fatal problem encountered while inspecting a component source."""

    name: str
    source: ComponentSource
    error: str


@dataclass(frozen=True, slots=True)
class ComponentCatalog(Generic[T]):
    """Immutable discovery snapshot retaining every candidate by name."""

    descriptors: tuple[ComponentDescriptor[T], ...]
    issues: tuple[DiscoveryIssue, ...] = ()
    inventory: Mapping[str, tuple[ComponentDescriptor[T], ...]] = field(init=False)

    def __post_init__(self) -> None:
        grouped: dict[str, list[ComponentDescriptor[T]]] = defaultdict(list)
        for descriptor in self.descriptors:
            grouped[descriptor.name].append(descriptor)
        inventory = {
            name: tuple(candidates) for name, candidates in sorted(grouped.items())
        }
        object.__setattr__(self, "inventory", MappingProxyType(inventory))

    @classmethod
    def build(
        cls,
        descriptors: Iterable[ComponentDescriptor[T]],
        issues: Iterable[DiscoveryIssue] = (),
    ) -> ComponentCatalog[T]:
        """Build a deterministically ordered immutable catalog."""

        return cls(
            descriptors=tuple(sorted(descriptors, key=_descriptor_sort_key)),
            issues=tuple(sorted(issues, key=_issue_sort_key)),
        )


class ComponentResolutionError(RuntimeError):
    """Raised when a component name is missing or ambiguous."""


@dataclass(frozen=True, slots=True)
class PackageComponentSource(Generic[T]):
    """Discover immediate public children of one importable package root."""

    package_name: str
    source: ComponentSource
    component_loader: Callable[[str, str, ComponentSource], T]
    reserved_children: frozenset[str] = frozenset()

    def discover(
        self, *, issue_name: str | None = None
    ) -> tuple[tuple[ComponentDescriptor[T], ...], tuple[DiscoveryIssue, ...]]:
        """Return lazy child descriptors without importing child modules."""

        try:
            package = importlib.import_module(self.package_name)
        except Exception as error:
            return (), (
                DiscoveryIssue(
                    name=issue_name or self.package_name,
                    source=self.source,
                    error=f"package import failed ({error})",
                ),
            )

        package_path = getattr(package, "__path__", None)
        if package_path is None:
            return (), (
                DiscoveryIssue(
                    name=issue_name or self.package_name,
                    source=self.source,
                    error="component source must reference a package",
                ),
            )

        descriptors: list[ComponentDescriptor[T]] = []
        for module_info in pkgutil.iter_modules(package_path):
            name = module_info.name
            if name.startswith("_") or name in self.reserved_children:
                continue
            descriptors.append(
                ComponentDescriptor(
                    name=name,
                    source=self.source,
                    load=lambda n=name: self.component_loader(
                        self.package_name, n, self.source
                    ),
                )
            )
        return tuple(descriptors), ()


class ComponentResolver(Generic[T]):
    """Resolve unique names and load only selected components."""

    def __init__(
        self,
        *,
        kind: ComponentKind,
        catalog: ComponentCatalog[T],
        error_type: type[Exception] = ComponentResolutionError,
        context: str | None = None,
    ) -> None:
        self._kind = kind
        self._catalog = catalog
        self._error_type = error_type
        self._context = context

    def catalog(self) -> ComponentCatalog[T]:
        """Return the resolver's immutable discovery snapshot."""

        return self._catalog

    def descriptor(self, name: str) -> ComponentDescriptor[T]:
        """Return the sole descriptor for name or raise an actionable error."""

        candidates = self._catalog.inventory.get(name, ())
        context = f" {self._context}" if self._context else ""
        if not candidates:
            available = ", ".join(self._catalog.inventory) or "none"
            raise self._error_type(
                f"Unknown {self._kind.value}{context} '{name}'. "
                f"Available {self._kind.value}s: {available}"
            )
        if len(candidates) > 1:
            sources = ", ".join(str(candidate.source) for candidate in candidates)
            raise self._error_type(
                f"{self._kind.value.capitalize()} '{name}' is ambiguous{context}; "
                f"found in multiple sources: {sources}"
            )
        return candidates[0]

    def load(self, name: str) -> T:
        """Load the uniquely selected component."""

        return self.descriptor(name).load()


def validate_keyword_only_invocation(
    callable_object: Callable[..., object], *, keyword_names: frozenset[str]
) -> None:
    """Validate that a callable accepts the supplied runtime keywords.

    Additional optional keyword-only parameters and ``**kwargs`` are allowed.
    Additional required parameters are rejected because the runtime cannot
    supply them.

    Args:
        callable_object: Callable to inspect.
        keyword_names: Runtime keyword names that will always be supplied.

    Raises:
        ValueError: If the signature cannot accept the runtime invocation.
    """

    try:
        callable_signature = signature(callable_object)
    except (TypeError, ValueError) as error:
        raise ValueError("unable to inspect callable signature") from error

    parameters = callable_signature.parameters
    missing = keyword_names - set(parameters)
    if missing:
        raise ValueError(f"missing required parameters: {sorted(missing)}")

    unsupported_parameters = sorted(
        parameter.name
        for parameter in parameters.values()
        if parameter.kind not in {Parameter.KEYWORD_ONLY, Parameter.VAR_KEYWORD}
    )
    if unsupported_parameters:
        raise ValueError(f"parameters must be keyword-only: {unsupported_parameters}")

    invocation_kwargs = {name: object() for name in keyword_names}
    try:
        callable_signature.bind(**invocation_kwargs)
    except TypeError as error:
        raise ValueError(f"cannot be invoked with runtime keywords: {error}") from error


def source_from_entry_point(
    *,
    entry_point: EntryPoint,
    package: str,
    label_prefix: str = "plugin:",
    provider: str | None = None,
) -> ComponentSource:
    """Build structured source metadata for a package entry point."""

    distribution = entry_point.dist.name if entry_point.dist is not None else None
    distribution_label = distribution or "unpackaged"
    return ComponentSource(
        origin=ComponentOrigin.PLUGIN,
        package=package,
        label=f"{label_prefix} {distribution_label}",
        distribution=distribution,
        entry_point_group=entry_point.group,
        entry_point_name=entry_point.name,
        provider=provider,
    )


def _descriptor_sort_key(
    descriptor: ComponentDescriptor[T],
) -> tuple[str, str, str, str]:
    return (
        descriptor.source.label,
        descriptor.name,
        descriptor.source.entry_point_name or "",
        descriptor.source.package,
    )


def _issue_sort_key(issue: DiscoveryIssue) -> tuple[str, str, str]:
    return (issue.source.label, issue.name, issue.error)
