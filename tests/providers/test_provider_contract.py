from __future__ import annotations

import pytest

from anvil.providers.aws import create_provider_instance as create_aws_provider_instance
from anvil.providers.azure import (
    create_provider_instance as create_azure_provider_instance,
)
from anvil.providers.base import (
    ExecutionTarget,
    ProviderExecutionPlan,
    ProviderMetadata,
    configured_or_default_regions,
    validate_provider_contract,
    validate_resolved_regions,
)
from anvil.providers.cloudflare import (
    create_provider_instance as create_cloudflare_provider_instance,
)
from anvil.providers.datadog import (
    create_provider_instance as create_datadog_provider_instance,
)
from anvil.providers.gcp import create_provider_instance as create_gcp_provider_instance
from anvil.providers.github import (
    create_provider_instance as create_github_provider_instance,
)
from anvil.providers.gitlab import (
    create_provider_instance as create_gitlab_provider_instance,
)
from anvil.providers.pagerduty import (
    create_provider_instance as create_pagerduty_provider_instance,
)


class _CompleteProvider:
    metadata = ProviderMetadata(
        name="complete", display_name="Complete", supported_task_scopes=frozenset()
    )

    def validate_target(self, target):
        return None

    def resolve_target_filters(self, *, target, include_override, exclude_override):
        return target.include, target.exclude

    def auth_cache_key(self, target):
        return None

    def auth_check(self, target):
        return None

    def discover_regions(self, target):
        return []

    def prepare_target(self, *, target, context, include, exclude, cache, benchmark):
        return None

    def resolve_execution_targets(
        self, *, target, regions, include, exclude, preparation=None
    ):
        return None

    def prepare_execution_runtime(self, *, target, execution_target, context):
        return None


def test_first_party_providers_satisfy_provider_contract():
    providers = [
        create_aws_provider_instance(),
        create_azure_provider_instance(),
        create_cloudflare_provider_instance(),
        create_datadog_provider_instance(),
        create_gcp_provider_instance(),
        create_github_provider_instance(),
        create_gitlab_provider_instance(),
        create_pagerduty_provider_instance(),
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
        (create_cloudflare_provider_instance(), ["global"]),
        (create_datadog_provider_instance(), ["global"]),
        (create_gcp_provider_instance(), ["us-central1"]),
        (create_github_provider_instance(), ["global"]),
        (create_gitlab_provider_instance(), ["global"]),
        (create_pagerduty_provider_instance(), ["global"]),
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
    class BrokenProvider(_CompleteProvider):
        metadata = ProviderMetadata(
            name="", display_name="Broken", supported_task_scopes=frozenset()
        )

    with pytest.raises(ValueError, match="metadata name"):
        validate_provider_contract(BrokenProvider())


def test_provider_contract_rejects_empty_display_name():
    class BrokenProvider(_CompleteProvider):
        metadata = ProviderMetadata(
            name="broken", display_name="", supported_task_scopes=frozenset()
        )

    with pytest.raises(ValueError, match="display_name"):
        validate_provider_contract(BrokenProvider())


def test_provider_contract_rejects_missing_contract_parameter():
    class BrokenProvider(_CompleteProvider):
        metadata = ProviderMetadata(
            name="broken", display_name="Broken", supported_task_scopes=frozenset()
        )

        def resolve_execution_targets(  # ty: ignore[invalid-method-override]
            self, *, target, regions, include
        ):
            return None

    with pytest.raises(TypeError, match="resolve_execution_targets.*exclude"):
        validate_provider_contract(BrokenProvider())


def test_provider_contract_rejects_missing_preparation_parameter():
    class BrokenProvider(_CompleteProvider):
        metadata = ProviderMetadata(
            name="broken", display_name="Broken", supported_task_scopes=frozenset()
        )

        def resolve_execution_targets(  # ty: ignore[invalid-method-override]
            self, *, target, regions, include, exclude
        ):
            return None

    with pytest.raises(TypeError, match="resolve_execution_targets.*preparation"):
        validate_provider_contract(BrokenProvider())


def test_configured_target_capability_requires_explicit_provider_hooks():
    class BrokenProvider(_CompleteProvider):
        metadata = ProviderMetadata(
            name="broken",
            display_name="Broken",
            supported_task_scopes=frozenset({"configured_target", "region"}),
        )

    with pytest.raises(
        TypeError, match="configured_target.*validate_task_configuration"
    ):
        validate_provider_contract(BrokenProvider())


def test_configured_target_capability_accepts_complete_provider_hooks():
    class ConfiguredProvider(_CompleteProvider):
        metadata = ProviderMetadata(
            name="configured",
            display_name="Configured",
            supported_task_scopes=frozenset({"configured_target", "region"}),
        )

        def validate_task_configuration(self, *, target, task_scopes):
            return None

        def prepare_configured_target_runtime(
            self, *, target, execution_target, context
        ):
            return None

    validate_provider_contract(ConfiguredProvider())


def test_provider_execution_plan_carries_provider_owned_configured_identity():
    configured_target = ExecutionTarget(
        id="provider-owner",
        name="Provider Owner",
        type="configured_target",
        provider="complete",
        regions=["home-region"],
    )

    plan = ProviderExecutionPlan(
        execution_targets=[], configured_target=configured_target
    )

    assert plan.configured_target is configured_target
