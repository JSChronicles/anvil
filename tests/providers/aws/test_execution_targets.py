from __future__ import annotations

from dataclasses import dataclass

from anvil.descriptors import TargetDescriptor
from anvil.providers.aws.provider import (
    AwsExecutionTargetData,
    AwsPreflightData,
    AwsProvider,
)
from anvil.providers.aws.account import AccountAccessStrategy


@dataclass
class BaseSession:
    profile_name: str | None = "profile-a"


class FakeSessionFactory:
    def __init__(self) -> None:
        self.base_session_calls = []

    def create_base_session(self, **kwargs):
        self.base_session_calls.append(kwargs)
        return BaseSession(profile_name=kwargs["profile_name"])


def _preflight_data(
    *,
    session_factory: FakeSessionFactory,
    base_session: BaseSession | None = None,
    base_session_account_id: str = "111111111111",
    discovered_accounts: dict[str, dict[str, str]] | None = None,
    region_statuses: dict[str, str] | None = None,
) -> AwsPreflightData:
    return AwsPreflightData(
        session_factory=session_factory,
        base_session=base_session or BaseSession(),
        organization_id="o-shared",
        management_account_id="111111111111",
        base_session_account_id=base_session_account_id,
        discovered_accounts=discovered_accounts
        or {
            "111111111111": {
                "account_number": "111111111111",
                "account_alias": "management",
            },
            "222222222222": {
                "account_number": "222222222222",
                "account_alias": "member",
            },
        },
        region_statuses=region_statuses or {"us-east-1": "ENABLED_BY_DEFAULT"},
    )


def test_resolve_execution_targets_maps_explicit_assume_role_accounts(monkeypatch):
    session_factory = FakeSessionFactory()
    monkeypatch.setattr(
        "anvil.providers.aws.provider.SessionFactory", lambda: session_factory
    )
    target = TargetDescriptor(
        name="selected",
        provider="aws",
        mode="accounts",
        provider_options={"profile": "tooling", "role_name": "SecurityAccessRole"},
        include=["111111111111", "222222222222"],
    )

    plan = AwsProvider().resolve_execution_targets(
        target=target,
        regions=["us-east-1"],
        include=target.include,
        exclude=target.exclude,
    )

    assert [execution_target.id for execution_target in plan.execution_targets] == [
        "111111111111",
        "222222222222",
    ]
    assert [
        execution_target.metadata["access_strategy"]
        for execution_target in plan.execution_targets
    ] == ["assume_role", "assume_role"]
    assert all(
        isinstance(execution_target.provider_data, AwsExecutionTargetData)
        for execution_target in plan.execution_targets
    )
    assert session_factory.base_session_calls == [
        {"profile_name": "tooling", "region_name": "us-east-1"}
    ]


def test_resolve_execution_targets_maps_explicit_direct_profile_account(monkeypatch):
    session_factory = FakeSessionFactory()
    monkeypatch.setattr(
        "anvil.providers.aws.provider.SessionFactory", lambda: session_factory
    )
    target = TargetDescriptor(
        name="current",
        provider="aws",
        mode="accounts",
        provider_options={"profile": "dev-admin"},
        include=["111111111111"],
    )

    plan = AwsProvider().resolve_execution_targets(
        target=target,
        regions=["us-east-1"],
        include=target.include,
        exclude=target.exclude,
    )

    assert [execution_target.id for execution_target in plan.execution_targets] == [
        "111111111111"
    ]
    assert plan.execution_targets[0].metadata["access_strategy"] == "direct_profile"


def test_resolve_execution_targets_maps_explicit_assume_role_accounts_with_provider_options(
    monkeypatch,
):
    session_factory = FakeSessionFactory()
    monkeypatch.setattr(
        "anvil.providers.aws.provider.SessionFactory", lambda: session_factory
    )
    target = TargetDescriptor(
        name="selected",
        provider="aws",
        mode="accounts",
        provider_options={"profile": "tooling", "role_name": "SecurityAccessRole"},
        include=["111111111111", "222222222222"],
    )

    plan = AwsProvider().resolve_execution_targets(
        target=target,
        regions=["us-east-1"],
        include=target.include,
        exclude=target.exclude,
    )

    assert [execution_target.id for execution_target in plan.execution_targets] == [
        "111111111111",
        "222222222222",
    ]
    assert session_factory.base_session_calls == [
        {"profile_name": "tooling", "region_name": "us-east-1"}
    ]


def test_resolve_execution_targets_maps_organization_accounts_and_execution_key():
    session_factory = FakeSessionFactory()
    base_session = BaseSession(profile_name="shared")
    target = TargetDescriptor(
        name="org-a",
        provider="aws",
        mode="organization",
        provider_options={"profile": "shared"},
        include=["222222222222"],
    )

    plan = AwsProvider().resolve_execution_targets(
        target=target,
        regions=["us-east-1"],
        include=target.include,
        exclude=target.exclude,
        preparation=_preflight_data(
            session_factory=session_factory, base_session=base_session
        ),
    )

    assert [execution_target.id for execution_target in plan.execution_targets] == [
        "222222222222"
    ]
    assert plan.execution_targets[0].name == "member"
    assert plan.execution_targets[0].metadata == {
        "account_id": "222222222222",
        "account_alias": "member",
        "is_management": False,
        "access_strategy": "assume_role",
    }
    assert session_factory.base_session_calls == []


def test_organization_configured_target_uses_management_base_session_identity():
    session_factory = FakeSessionFactory()
    base_session = BaseSession(profile_name="management")
    target = TargetDescriptor(
        name="org-a",
        provider="aws",
        mode="organization",
        provider_options={"profile": "management"},
        exclude=["111111111111"],
    )

    plan = AwsProvider().resolve_execution_targets(
        target=target,
        regions=["us-east-1"],
        include=target.include,
        exclude=target.exclude,
        preparation=_preflight_data(
            session_factory=session_factory,
            base_session=base_session,
            base_session_account_id="111111111111",
        ),
    )

    assert [execution_target.id for execution_target in plan.execution_targets] == [
        "222222222222"
    ]
    assert plan.configured_target is not None
    assert (
        plan.configured_target.id,
        plan.configured_target.name,
        plan.configured_target.type,
        plan.configured_target.regions,
    ) == ("111111111111", "management", "configured_target", ["us-east-1"])
    assert isinstance(plan.configured_target.provider_data, AwsExecutionTargetData)
    assert (
        plan.configured_target.provider_data.access_strategy
        is AccountAccessStrategy.BASE_SESSION
    )
    assert plan.configured_target.provider_data.base_session is base_session


def test_organization_configured_target_assumes_management_role_when_needed():
    target = TargetDescriptor(
        name="org-a",
        provider="aws",
        mode="organization",
        provider_options={"role_name": "ManagementAccessRole"},
        include=["222222222222"],
    )

    plan = AwsProvider().resolve_execution_targets(
        target=target,
        regions=["us-east-1"],
        include=target.include,
        exclude=target.exclude,
        preparation=_preflight_data(
            session_factory=FakeSessionFactory(), base_session_account_id="333333333333"
        ),
    )

    assert plan.configured_target is not None
    assert isinstance(plan.configured_target.provider_data, AwsExecutionTargetData)
    assert (
        plan.configured_target.provider_data.access_strategy
        is AccountAccessStrategy.ASSUME_ROLE
    )
    assert plan.configured_target.provider_data.role_name == "ManagementAccessRole"
    assert plan.configured_target.provider_data.account_id == "111111111111"


def test_organization_configured_target_is_stable_when_management_is_selected():
    target = TargetDescriptor(
        name="org-a",
        provider="aws",
        mode="organization",
        include=["111111111111", "222222222222"],
    )

    plan = AwsProvider().resolve_execution_targets(
        target=target,
        regions=["us-east-1"],
        include=target.include,
        exclude=target.exclude,
        preparation=_preflight_data(session_factory=FakeSessionFactory()),
    )

    assert [execution_target.id for execution_target in plan.execution_targets] == [
        "111111111111",
        "222222222222",
    ]
    assert plan.configured_target is not None
    assert plan.configured_target.id == "111111111111"
    assert plan.configured_target.name == "management"


def test_organization_management_keyword_selects_management_account():
    target = TargetDescriptor(
        name="org-a", provider="aws", mode="organization", include=["management"]
    )

    plan = AwsProvider().resolve_execution_targets(
        target=target,
        regions=["us-east-1"],
        include=target.include,
        exclude=target.exclude,
        preparation=_preflight_data(session_factory=FakeSessionFactory()),
    )

    assert [execution_target.id for execution_target in plan.execution_targets] == [
        "111111111111"
    ]
    assert plan.execution_targets[0].metadata["is_management"] is True


def test_organization_payer_keyword_excludes_management_account():
    target = TargetDescriptor(
        name="org-a", provider="aws", mode="organization", exclude=["payer"]
    )

    plan = AwsProvider().resolve_execution_targets(
        target=target,
        regions=["us-east-1"],
        include=target.include,
        exclude=target.exclude,
        preparation=_preflight_data(session_factory=FakeSessionFactory()),
    )

    assert [execution_target.id for execution_target in plan.execution_targets] == [
        "222222222222"
    ]


def test_organization_configured_target_uses_concrete_resolved_regions():
    target = TargetDescriptor(
        name="org-a", provider="aws", mode="organization", regions=["us-*"]
    )

    plan = AwsProvider().resolve_execution_targets(
        target=target,
        regions=["us-*"],
        include=target.include,
        exclude=target.exclude,
        preparation=_preflight_data(
            session_factory=FakeSessionFactory(),
            region_statuses={"us-east-1": "ENABLED_BY_DEFAULT", "us-west-2": "ENABLED"},
        ),
    )

    assert plan.configured_target is not None
    assert plan.configured_target.regions == ["us-east-1", "us-west-2"]


def test_single_explicit_account_is_configured_target_identity(monkeypatch):
    session_factory = FakeSessionFactory()
    monkeypatch.setattr(
        "anvil.providers.aws.provider.SessionFactory", lambda: session_factory
    )
    target = TargetDescriptor(
        name="one-account",
        provider="aws",
        mode="accounts",
        provider_options={"profile": "tooling", "role_name": "SecurityAccessRole"},
        include=["222222222222"],
    )

    plan = AwsProvider().resolve_execution_targets(
        target=target,
        regions=["us-east-1"],
        include=target.include,
        exclude=target.exclude,
    )

    assert plan.configured_target is not None
    assert (
        plan.configured_target.id,
        plan.configured_target.name,
        plan.configured_target.type,
        plan.configured_target.regions,
    ) == ("222222222222", "222222222222", "configured_target", ["us-east-1"])
    assert (
        plan.configured_target.provider_data is plan.execution_targets[0].provider_data
    )


def test_multiple_explicit_accounts_do_not_select_configured_target(monkeypatch):
    session_factory = FakeSessionFactory()
    monkeypatch.setattr(
        "anvil.providers.aws.provider.SessionFactory", lambda: session_factory
    )
    target = TargetDescriptor(
        name="many-accounts",
        provider="aws",
        mode="accounts",
        provider_options={"role_name": "SecurityAccessRole"},
        include=["111111111111", "222222222222"],
    )

    plan = AwsProvider().resolve_execution_targets(
        target=target,
        regions=["us-east-1"],
        include=target.include,
        exclude=target.exclude,
    )

    assert plan.configured_target is None


def test_resolve_execution_targets_maps_organization_accounts_with_provider_options():
    session_factory = FakeSessionFactory()
    base_session = BaseSession(profile_name="shared")
    target = TargetDescriptor(
        name="org-a",
        provider="aws",
        mode="organization",
        provider_options={"profile": "shared"},
        include=["222222222222"],
    )

    plan = AwsProvider().resolve_execution_targets(
        target=target,
        regions=["us-east-1"],
        include=target.include,
        exclude=target.exclude,
        preparation=_preflight_data(
            session_factory=session_factory, base_session=base_session
        ),
    )

    assert [execution_target.id for execution_target in plan.execution_targets] == [
        "222222222222"
    ]
    assert session_factory.base_session_calls == []


def test_resolve_execution_targets_preserves_unknown_include_warning(caplog):
    target = TargetDescriptor(
        name="org-a", provider="aws", mode="organization", include=["999999999999"]
    )

    plan = AwsProvider().resolve_execution_targets(
        target=target,
        regions=["us-east-1"],
        include=target.include,
        exclude=target.exclude,
        preparation=_preflight_data(
            session_factory=FakeSessionFactory(),
            discovered_accounts={
                "111111111111": {
                    "account_number": "111111111111",
                    "account_alias": "management",
                }
            },
        ),
    )

    assert plan.execution_targets == []
    assert "include list contains unknown account IDs: 999999999999" in caplog.text


def test_resolve_execution_targets_preserves_unknown_exclude_warning(caplog):
    target = TargetDescriptor(
        name="org-a", provider="aws", mode="organization", exclude=["999999999999"]
    )

    plan = AwsProvider().resolve_execution_targets(
        target=target,
        regions=["us-east-1"],
        include=target.include,
        exclude=target.exclude,
        preparation=_preflight_data(
            session_factory=FakeSessionFactory(),
            discovered_accounts={
                "111111111111": {
                    "account_number": "111111111111",
                    "account_alias": "management",
                }
            },
        ),
    )

    assert [execution_target.id for execution_target in plan.execution_targets] == [
        "111111111111"
    ]
    assert "exclude list contains unknown account IDs: 999999999999" in caplog.text
