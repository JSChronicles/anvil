from __future__ import annotations

import pytest

from anvil.descriptors import TargetDescriptor
from anvil.providers.aws.provider import AwsProvider
from anvil.providers.azure.provider import AzureProvider


def _target(
    *,
    provider: str,
    mode: str,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    provider_options: dict[str, object] | None = None,
) -> TargetDescriptor:
    return TargetDescriptor(
        name=f"{provider}-{mode}",
        provider=provider,
        mode=mode,
        include=include,
        exclude=exclude,
        provider_options=provider_options or {},
    )


def test_shared_descriptor_accepts_plugin_owned_modes_and_option_shapes() -> None:
    target = _target(
        provider="Acme",
        mode="Fleet",
        provider_options={"nested": {"enabled": True}, "batch_size": 25},
    )

    assert target.provider == "acme"
    assert target.mode == "fleet"
    assert target.provider_options == {"nested": {"enabled": True}, "batch_size": 25}


def test_first_party_provider_rejects_an_unknown_mode() -> None:
    target = _target(provider="azure", mode="projects")

    with pytest.raises(ValueError, match="Unsupported Azure target mode"):
        AzureProvider().validate_target(target)


def test_aws_explicit_accounts_without_role_are_direct_and_single_account() -> None:
    target = _target(
        provider="aws", mode="accounts", include=["111111111111", "222222222222"]
    )

    with pytest.raises(ValueError, match="without role_name.*exactly one"):
        AwsProvider().validate_target(target)


def test_aws_explicit_accounts_do_not_receive_the_organization_default_role() -> None:
    target = _target(provider="aws", mode="accounts", include=["111111111111"])

    AwsProvider().validate_target(target)
    assert target.provider_options.get("role_name") is None


@pytest.mark.parametrize("keyword", ["management", "payer", "MANAGEMENT", "PAYER"])
def test_aws_organization_accepts_management_account_keywords(keyword: str) -> None:
    target = _target(provider="aws", mode="organization", include=[keyword])

    AwsProvider().validate_target(target)


@pytest.mark.parametrize("keyword", ["management", "payer"])
def test_aws_accounts_mode_rejects_management_account_keywords(keyword: str) -> None:
    target = _target(provider="aws", mode="accounts", include=[keyword])

    with pytest.raises(ValueError, match=f"keyword '{keyword}'.*organization mode"):
        AwsProvider().validate_target(target)


def test_provider_owns_cli_filter_semantics() -> None:
    target = _target(
        provider="azure",
        mode="subscriptions",
        include=["subscription-a", "subscription-b"],
    )

    assert AzureProvider().resolve_target_filters(
        target=target,
        include_override=["subscription-b", "not-configured"],
        exclude_override=None,
    ) == (["subscription-b"], None)

    with pytest.raises(ValueError, match="does not allow --exclude"):
        AzureProvider().resolve_target_filters(
            target=target, include_override=None, exclude_override=["subscription-a"]
        )
