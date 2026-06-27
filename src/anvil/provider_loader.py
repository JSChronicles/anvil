from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import EntryPoint, entry_points

from anvil._loader_utils import DiscoveryIssue, plugin_source
from anvil.providers.base import Provider, ProviderMetadata

PROVIDER_ENTRY_POINT_GROUP = "anvil.providers"


@dataclass(frozen=True, slots=True)
class ProviderDescriptor:
    """Provider metadata and lazy loader used by CLI discovery."""

    name: str
    display_name: str
    load: Callable[[], Provider]
    source: str
    description: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderDiscoveryResult:
    """Discovered providers and non-fatal plugin discovery issues."""

    providers: list[ProviderDescriptor]
    issues: list[DiscoveryIssue]


def _load_aws_provider() -> Provider:
    from anvil.providers.aws import create_provider

    return create_provider()


def _load_azure_provider() -> Provider:
    from anvil.providers.azure import create_provider

    return create_provider()


def _load_gcp_provider() -> Provider:
    from anvil.providers.gcp import create_provider

    return create_provider()


def _load_plugin_provider(entry_point: EntryPoint) -> Provider:
    loaded = entry_point.load()
    if hasattr(loaded, "create_provider"):
        factory = loaded.create_provider
    else:
        factory = loaded

    if not callable(factory):
        raise TypeError(
            f"provider entry point '{entry_point.name}' must expose create_provider()"
        )

    provider = factory()
    metadata = getattr(provider, "metadata", None)
    if not isinstance(metadata, ProviderMetadata):
        raise TypeError(
            f"provider entry point '{entry_point.name}' returned provider without "
            "ProviderMetadata"
        )

    return provider


def _builtin_provider_descriptors() -> list[ProviderDescriptor]:
    return [
        ProviderDescriptor(
            name="aws",
            display_name="AWS",
            description="Amazon Web Services provider",
            load=_load_aws_provider,
            source="stock",
        ),
        ProviderDescriptor(
            name="azure",
            display_name="Azure",
            description="Microsoft Azure provider",
            load=_load_azure_provider,
            source="stock",
        ),
        ProviderDescriptor(
            name="gcp",
            display_name="GCP",
            description="Google Cloud provider",
            load=_load_gcp_provider,
            source="stock",
        ),
    ]


def _plugin_provider_descriptors() -> tuple[
    list[ProviderDescriptor], list[DiscoveryIssue]
]:
    providers: list[ProviderDescriptor] = []
    issues: list[DiscoveryIssue] = []

    for entry_point in entry_points(group=PROVIDER_ENTRY_POINT_GROUP):
        source = plugin_source(entry_point)
        name = entry_point.name
        providers.append(
            ProviderDescriptor(
                name=name,
                display_name=name,
                load=lambda ep=entry_point: _load_plugin_provider(ep),
                source=source,
            )
        )

    return providers, issues


def discover_providers() -> ProviderDiscoveryResult:
    """Discover first-party and plugin providers without loading providers."""

    providers = {
        descriptor.name: descriptor for descriptor in _builtin_provider_descriptors()
    }
    plugin_providers, issues = _plugin_provider_descriptors()
    for descriptor in plugin_providers:
        if descriptor.name in providers:
            issues.append(
                DiscoveryIssue(
                    name=descriptor.name,
                    source=descriptor.source,
                    error="provider duplicates an existing provider name",
                )
            )
            continue

        providers[descriptor.name] = descriptor

    return ProviderDiscoveryResult(
        providers=sorted(providers.values(), key=lambda item: (item.source, item.name)),
        issues=issues,
    )


def list_providers() -> list[ProviderDescriptor]:
    """Return provider descriptors for CLI listing."""

    return discover_providers().providers
