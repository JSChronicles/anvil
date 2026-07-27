"""Lazy provider discovery and construction."""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from functools import lru_cache
from importlib.metadata import EntryPoint, entry_points
from typing import cast

from anvil._components import (
    ComponentCatalog,
    ComponentDescriptor,
    ComponentKind,
    ComponentOrigin,
    ComponentResolver,
    ComponentSource,
    DiscoveryIssue as CatalogDiscoveryIssue,
    PackageComponentSource,
    source_from_entry_point,
)
from anvil.providers.base import Provider, ProviderMetadata

PROVIDER_PACKAGE_ENTRY_POINT_GROUP = "anvil.provider_packages"
_STOCK_PROVIDER_PACKAGE = "anvil.providers"
_RESERVED_PROVIDER_CHILDREN = frozenset({"base", "tasks"})


ProviderDescriptor = ComponentDescriptor[Provider]


@dataclass(frozen=True, slots=True)
class ProviderDiscoveryResult:
    """Discovered providers and non-fatal package discovery issues."""

    providers: list[ProviderDescriptor]
    issues: list[CatalogDiscoveryIssue]


def _load_provider_from_package(
    package_name: str, provider_name: str, source: ComponentSource
) -> Provider:
    """Construct one selected provider using its package convention."""

    module_name = f"{package_name}.{provider_name}"
    try:
        package = importlib.import_module(module_name)
    except Exception as error:
        raise TypeError(
            f"Provider '{provider_name}' ({source}) failed during import: {error}"
        ) from error
    factory = getattr(package, "create_provider_instance", None)
    if not callable(factory):
        raise TypeError(
            f"provider package '{module_name}' must expose create_provider_instance()"
        )
    return _validate_provider_instance(
        provider=factory(), provider_name=provider_name, source=source
    )


def _validate_provider_instance(
    *, provider: object, provider_name: str, source: ComponentSource | str
) -> Provider:
    metadata = getattr(provider, "metadata", None)
    if not isinstance(metadata, ProviderMetadata):
        raise TypeError(
            f"provider '{provider_name}' ({source}) returned provider without "
            "ProviderMetadata"
        )
    if metadata.name != provider_name:
        raise TypeError(
            f"provider package name '{provider_name}' does not match metadata name "
            f"'{metadata.name}'"
        )
    return cast(Provider, provider)


def _entry_point_source(entry_point: EntryPoint) -> ComponentSource:
    package_name = entry_point.value.split(":", maxsplit=1)[0]
    return source_from_entry_point(entry_point=entry_point, package=package_name)


@lru_cache(maxsize=16)
def _provider_catalog_for_entry_points(
    package_entry_points: tuple[EntryPoint, ...],
) -> ComponentCatalog[Provider]:
    descriptors = []
    issues: list[CatalogDiscoveryIssue] = []

    stock_source = ComponentSource(
        origin=ComponentOrigin.STOCK, package=_STOCK_PROVIDER_PACKAGE, label="stock"
    )
    stock_descriptors, stock_issues = PackageComponentSource(
        package_name=_STOCK_PROVIDER_PACKAGE,
        source=stock_source,
        component_loader=_load_provider_from_package,
        reserved_children=_RESERVED_PROVIDER_CHILDREN,
    ).discover()
    descriptors.extend(stock_descriptors)
    issues.extend(stock_issues)

    for entry_point in package_entry_points:
        entry_point_value = getattr(entry_point, "value", None)
        if not isinstance(entry_point_value, str):
            continue
        package_name = entry_point_value.split(":", maxsplit=1)[0]
        source = _entry_point_source(entry_point)
        package_descriptors, package_issues = PackageComponentSource(
            package_name=package_name,
            source=source,
            component_loader=_load_provider_from_package,
        ).discover(issue_name=entry_point.name)
        descriptors.extend(package_descriptors)
        issues.extend(package_issues)

    catalog = ComponentCatalog.build(descriptors, issues)
    duplicate_issues = list(catalog.issues)
    for name, candidates in catalog.inventory.items():
        if len(candidates) < 2:
            continue
        for candidate in candidates:
            duplicate_issues.append(
                CatalogDiscoveryIssue(
                    name=name,
                    source=candidate.source,
                    error="provider duplicates an existing provider name",
                )
            )
    return ComponentCatalog.build(catalog.descriptors, duplicate_issues)


@lru_cache(maxsize=1)
def _provider_catalog() -> ComponentCatalog[Provider]:
    """Return the process-local provider discovery snapshot."""

    return _provider_catalog_for_entry_points(
        tuple(entry_points(group=PROVIDER_PACKAGE_ENTRY_POINT_GROUP))
    )


def _clear_provider_caches() -> None:
    """Clear provider discovery snapshots and derived catalogs."""

    _provider_catalog.cache_clear()
    _provider_catalog_for_entry_points.cache_clear()


def discover_providers() -> ProviderDiscoveryResult:
    """Discover provider folders without constructing providers."""

    catalog = _provider_catalog()
    return ProviderDiscoveryResult(
        providers=list(catalog.descriptors), issues=list(catalog.issues)
    )


def list_providers() -> list[ProviderDescriptor]:
    """Return all provider candidates in deterministic source/name order."""

    return discover_providers().providers


def load_provider(provider_name: str) -> Provider:
    """Load a uniquely selected provider and return a fresh instance."""

    resolver: ComponentResolver[Provider] = ComponentResolver(
        kind=ComponentKind.PROVIDER, catalog=_provider_catalog(), error_type=ValueError
    )
    return resolver.load(provider_name)
