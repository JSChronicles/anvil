from __future__ import annotations

import pytest

from anvil.providers.aws import create_provider as create_aws_provider
from anvil.providers.azure import create_provider as create_azure_provider
from anvil.providers.base import ProviderMetadata, validate_provider_contract
from anvil.providers.gcp import create_provider as create_gcp_provider
from anvil.providers.github import create_provider as create_github_provider


def test_first_party_providers_satisfy_provider_contract():
    providers = [
        create_aws_provider(),
        create_azure_provider(),
        create_gcp_provider(),
        create_github_provider(),
    ]

    for provider in providers:
        validate_provider_contract(provider)


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


