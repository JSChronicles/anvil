from __future__ import annotations

import pytest

from anvil import provider_loader
from anvil.providers.base import ProviderMetadata


def test_list_providers_returns_aws_without_loading_provider(monkeypatch):
    def fail_load():
        raise AssertionError("listing providers should not instantiate providers")

    monkeypatch.setattr(provider_loader, "_load_aws_provider", fail_load)
    monkeypatch.setattr(provider_loader, "_load_azure_provider", fail_load)
    monkeypatch.setattr(provider_loader, "_load_gcp_provider", fail_load)
    monkeypatch.setattr(provider_loader, "_load_github_provider", fail_load)
    monkeypatch.setattr(provider_loader, "entry_points", lambda *, group: [])

    providers = provider_loader.list_providers()

    assert [
        (provider.name, provider.display_name, provider.source)
        for provider in providers
    ] == [
        ("aws", "AWS", "stock"),
        ("azure", "Azure", "stock"),
        ("gcp", "GCP", "stock"),
        ("github", "GitHub", "stock"),
    ]


def test_discover_providers_reports_duplicate_plugin_name(monkeypatch):
    class EntryPoint:
        name = "aws"

        @property
        def dist(self):
            return None

    monkeypatch.setattr(
        provider_loader, "entry_points", lambda *, group: [EntryPoint()]
    )

    discovery = provider_loader.discover_providers()

    assert [provider.name for provider in discovery.providers] == [
        "aws",
        "azure",
        "gcp",
        "github",
    ]
    assert len(discovery.issues) == 1
    assert discovery.issues[0].name == "aws"
    assert "duplicates" in discovery.issues[0].error


def test_plugin_provider_descriptor_loads_create_provider(monkeypatch):
    class Provider:
        metadata = ProviderMetadata(name="custom", display_name="Custom")

    class EntryPoint:
        name = "custom"

        @property
        def dist(self):
            return None

        def load(self):
            return type(
                "ProviderModule",
                (),
                {"create_provider": staticmethod(lambda: Provider())},
            )

    monkeypatch.setattr(
        provider_loader, "entry_points", lambda *, group: [EntryPoint()]
    )

    descriptor_by_name = {
        descriptor.name: descriptor
        for descriptor in provider_loader.discover_providers().providers
    }
    descriptor = descriptor_by_name["custom"]

    assert descriptor.name == "custom"
    assert descriptor.load().metadata.name == "custom"


def test_plugin_provider_load_error_is_actionable():
    class EntryPoint:
        name = "broken"

        def load(self):
            return object()

    descriptor = provider_loader.ProviderDescriptor(
        name="broken",
        display_name="broken",
        source="plugin (unpackaged)",
        load=lambda: provider_loader._load_plugin_provider(EntryPoint()),
    )

    with pytest.raises(TypeError, match="create_provider"):
        descriptor.load()


