from __future__ import annotations

from dataclasses import dataclass

from anvil.descriptors import ConfigBranch, TargetDescriptor
from anvil.providers.aws.provider import AwsExecutionTargetData, AwsProvider


@dataclass
class BaseSession:
    profile_name: str | None = "profile-a"


class FakeSessionFactory:
    def __init__(self) -> None:
        self.base_session_calls = []

    def create_base_session(self, **kwargs):
        self.base_session_calls.append(kwargs)
        return BaseSession(profile_name=kwargs["profile_name"])


def test_resolve_execution_targets_maps_explicit_assume_role_accounts():
    session_factory = FakeSessionFactory()
    target = TargetDescriptor(
        config_branch=ConfigBranch.ACCOUNTS,
        name="selected",
        profile="tooling",
        role_name="SecurityAccessRole",
        include=["111111111111", "222222222222"],
    )

    plan = AwsProvider().resolve_execution_targets(
        target=target,
        regions=["us-east-1"],
        include=target.include,
        exclude=target.exclude,
        session_factory=session_factory,
    )

    assert plan.exclusive_execution_key is None
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


def test_resolve_execution_targets_maps_explicit_direct_profile_account():
    session_factory = FakeSessionFactory()
    target = TargetDescriptor(
        config_branch=ConfigBranch.ACCOUNTS,
        name="current",
        profile="dev-admin",
        include=["111111111111"],
    )

    plan = AwsProvider().resolve_execution_targets(
        target=target,
        regions=["us-east-1"],
        include=target.include,
        exclude=target.exclude,
        session_factory=session_factory,
    )

    assert [execution_target.id for execution_target in plan.execution_targets] == [
        "111111111111"
    ]
    assert plan.execution_targets[0].metadata["access_strategy"] == "direct_profile"


def test_resolve_execution_targets_maps_organization_accounts_and_execution_key():
    session_factory = FakeSessionFactory()
    base_session = BaseSession(profile_name="shared")
    target = TargetDescriptor(
        config_branch=ConfigBranch.ORGANIZATIONS,
        name="org-a",
        profile="shared",
        include=["222222222222"],
    )

    plan = AwsProvider().resolve_execution_targets(
        target=target,
        regions=["us-east-1"],
        include=target.include,
        exclude=target.exclude,
        session_factory=session_factory,
        base_session=base_session,
        organization_id="o-shared",
        management_account_id="111111111111",
        base_session_account_id="111111111111",
        discovered_accounts={
            "111111111111": {
                "account_number": "111111111111",
                "account_alias": "management",
            },
            "222222222222": {
                "account_number": "222222222222",
                "account_alias": "member",
            },
        },
        region_statuses={"us-east-1": "ENABLED_BY_DEFAULT"},
    )

    assert plan.exclusive_execution_key == "o-shared"
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


def test_resolve_execution_targets_preserves_unknown_include_warning(caplog):
    target = TargetDescriptor(
        config_branch=ConfigBranch.ORGANIZATIONS, name="org-a", include=["999999999999"]
    )

    plan = AwsProvider().resolve_execution_targets(
        target=target,
        regions=["us-east-1"],
        include=target.include,
        exclude=target.exclude,
        session_factory=FakeSessionFactory(),
        base_session=BaseSession(),
        organization_id="o-shared",
        management_account_id="111111111111",
        base_session_account_id="111111111111",
        discovered_accounts={
            "111111111111": {
                "account_number": "111111111111",
                "account_alias": "management",
            }
        },
        region_statuses={"us-east-1": "ENABLED_BY_DEFAULT"},
    )

    assert plan.execution_targets == []
    assert "include list contains unknown account IDs: 999999999999" in caplog.text


def test_resolve_execution_targets_preserves_unknown_exclude_warning(caplog):
    target = TargetDescriptor(
        config_branch=ConfigBranch.ORGANIZATIONS, name="org-a", exclude=["999999999999"]
    )

    plan = AwsProvider().resolve_execution_targets(
        target=target,
        regions=["us-east-1"],
        include=target.include,
        exclude=target.exclude,
        session_factory=FakeSessionFactory(),
        base_session=BaseSession(),
        organization_id="o-shared",
        management_account_id="111111111111",
        base_session_account_id="111111111111",
        discovered_accounts={
            "111111111111": {
                "account_number": "111111111111",
                "account_alias": "management",
            }
        },
        region_statuses={"us-east-1": "ENABLED_BY_DEFAULT"},
    )

    assert [execution_target.id for execution_target in plan.execution_targets] == [
        "111111111111"
    ]
    assert "exclude list contains unknown account IDs: 999999999999" in caplog.text
