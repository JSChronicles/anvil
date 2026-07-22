from __future__ import annotations

import pytest

from anvil.providers.aws import create_provider_instance as create_aws_provider_instance
from anvil.providers.azure import (
    create_provider_instance as create_azure_provider_instance,
)
from anvil.providers.base import (
    ProviderMetadata,
    configured_or_default_regions,
    validate_provider_contract,
    validate_resolved_regions,
)
from anvil.providers.gcp import create_provider_instance as create_gcp_provider_instance
from anvil.providers.github import (
    create_provider_instance as create_github_provider_instance,
)


def test_first_party_providers_satisfy_provider_contract():
    providers = [
        create_aws_provider_instance(),
        create_azure_provider_instance(),
        create_gcp_provider_instance(),
        create_github_provider_instance(),
    ]

    for provider in providers:
        validate_provider_contract(provider)
        assert provider.metadata.default_regions


def test_configured_or_default_regions_preserves_explicit_values():
    assert configured_or_default_regions(configured=None, default=("eastus",)) == [
        "eastus"
    ]
    assert configured_or_default_regions(
        configured=["us-east-1"], default=("eastus",)
    ) == ["us-east-1"]


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        (create_aws_provider_instance(), ["us-east-1"]),
        (create_azure_provider_instance(), ["eastus"]),
        (create_gcp_provider_instance(), ["us-central1"]),
        (create_github_provider_instance(), ["global"]),
    ],
)
def test_omitted_regions_resolve_from_provider_metadata(provider, expected):
    assert (
        configured_or_default_regions(
            configured=None, default=provider.metadata.default_regions
        )
        == expected
    )


@pytest.mark.parametrize("regions", [[], [""], ["eastus", "eastus"]])
def test_validate_resolved_regions_rejects_invalid_values(regions):
    with pytest.raises(ValueError, match="regions"):
        validate_resolved_regions(regions=regions)


def test_provider_contract_rejects_empty_metadata_name():
    class BrokenProvider:
        metadata = ProviderMetadata(name="", display_name="Broken")

        def validate_target(self, target):
            return None

        def default_regions(self, target):
            return []

        def auth_cache_key(self, target):
            return None

        def auth_check(self, target):
            return None

        def discover_regions(self, target):
            return []

        def resolve_execution_targets(self, *, target, regions, include, exclude):
            return None

        def prepare_execution_runtime(self, *, target, execution_target, context):
            return None

    with pytest.raises(ValueError, match="metadata name"):
        validate_provider_contract(BrokenProvider())


def test_provider_contract_rejects_empty_display_name():
    class BrokenProvider:
        metadata = ProviderMetadata(name="broken", display_name="")

        def validate_target(self, target):
            return None

        def default_regions(self, target):
            return []

        def auth_cache_key(self, target):
            return None

        def auth_check(self, target):
            return None

        def discover_regions(self, target):
            return []

        def resolve_execution_targets(self, *, target, regions, include, exclude):
            return None

        def prepare_execution_runtime(self, *, target, execution_target, context):
            return None

    with pytest.raises(ValueError, match="display_name"):
        validate_provider_contract(BrokenProvider())


def test_provider_contract_rejects_missing_contract_parameter():
    class BrokenProvider:
        metadata = ProviderMetadata(name="broken", display_name="Broken")

        def validate_target(self, target):
            return None

        def default_regions(self, target):
            return []

        def auth_cache_key(self, target):
            return None

        def auth_check(self, target):
            return None

        def discover_regions(self, target):
            return []

        def resolve_execution_targets(self, *, target, regions, include):
            return None

        def prepare_execution_runtime(self, *, target, execution_target, context):
            return None

    with pytest.raises(TypeError, match="resolve_execution_targets.*exclude"):
        validate_provider_contract(BrokenProvider())
