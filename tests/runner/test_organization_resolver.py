from __future__ import annotations

import pytest

from anvil.descriptors import ConfigBranch, TargetDescriptor
from anvil.execution_context import ExecutionContext
from anvil.providers.aws.account import AccountAccessStrategy
from anvil.providers.aws.config import DEFAULT_ORGANIZATION_ROLE_NAME
from anvil.providers.aws.organization import OrganizationResolver
from anvil.providers.aws.regions import AwsRegionService


class FakePaginator:
    def __init__(self, pages: list[dict]) -> None:
        self._pages = pages

    def paginate(self):
        return iter(self._pages)


class FakeClient:
    def __init__(self, *, paginators: dict[str, FakePaginator]) -> None:
        self._paginators = paginators

    def get_paginator(self, name: str) -> FakePaginator:
        return self._paginators[name]


class FakeSession:
    def __init__(self, *, clients: dict[str, FakeClient] | None = None) -> None:
        self.clients = clients or {}

    def client(self, service_name, **kwargs):
        return self.clients[service_name]


class FailingSessionFactory:
    def create_base_session(self, **kwargs):
        raise AssertionError("preflight base_session should be reused")


def _context(*, regions: list[str] | None = None) -> ExecutionContext:
    return ExecutionContext(
        regions=regions or ["us-east-1"], dry_run=True, tasks=[], metadata={}
    )


def _target(**kwargs) -> TargetDescriptor:
    return TargetDescriptor(
        config_branch=ConfigBranch.TARGETS,
        name="org-a",
        provider="aws",
        mode="organization",
        provider_options={"profile": "profile-a"},
        regions=kwargs.pop("regions", ["us-east-1"]),
        **kwargs,
    )


def test_discover_accounts_keeps_active_accounts_and_defaults_alias():
    session = FakeSession(
        clients={
            "organizations": FakeClient(
                paginators={
                    "list_accounts": FakePaginator(
                        [
                            {
                                "Accounts": [
                                    {
                                        "Id": "111111111111",
                                        "Name": "active-account",
                                        "Status": "ACTIVE",
                                    },
                                    {"Id": "222222222222", "Status": "ACTIVE"},
                                    {
                                        "Id": "333333333333",
                                        "Name": "suspended-account",
                                        "Status": "SUSPENDED",
                                    },
                                ]
                            }
                        ]
                    )
                }
            )
        }
    )

    accounts = OrganizationResolver.discover_accounts(session)

    assert accounts == {
        "111111111111": {
            "account_number": "111111111111",
            "account_alias": "active-account",
        },
        "222222222222": {
            "account_number": "222222222222",
            "account_alias": "222222222222",
        },
    }


def test_aws_region_service_keeps_enabled_and_disabled_statuses():
    session = FakeSession(
        clients={
            "account": FakeClient(
                paginators={
                    "list_regions": FakePaginator(
                        [
                            {
                                "Regions": [
                                    {
                                        "RegionName": "us-west-2",
                                        "RegionOptStatus": "ENABLED",
                                    },
                                    {
                                        "RegionName": "us-east-1",
                                        "RegionOptStatus": "ENABLED_BY_DEFAULT",
                                    },
                                    {
                                        "RegionName": "ap-south-1",
                                        "RegionOptStatus": "DISABLED",
                                    },
                                    {"RegionOptStatus": "ENABLED"},
                                ]
                            }
                        ]
                    )
                }
            )
        }
    )

    assert AwsRegionService().discover_region_statuses(session=session) == {
        "us-east-1": "ENABLED_BY_DEFAULT",
        "us-west-2": "ENABLED",
        "ap-south-1": "DISABLED",
    }


def test_filter_accounts_intersects_include_and_exclude_filters():
    all_accounts = {
        "111111111111": {"account_number": "111111111111", "account_alias": "a"},
        "222222222222": {"account_number": "222222222222", "account_alias": "b"},
    }

    included = OrganizationResolver(
        descriptor=_target(include=["222222222222", "999999999999"]), context=_context()
    )._filter_accounts(all_accounts)
    excluded = OrganizationResolver(
        descriptor=_target(exclude=["111111111111", "999999999999"]), context=_context()
    )._filter_accounts(all_accounts)

    assert list(included) == ["222222222222"]
    assert list(excluded) == ["222222222222"]


def test_resolve_accounts_uses_default_management_account_direct_mode():
    discovered_accounts = {
        "111111111111": {
            "account_number": "111111111111",
            "account_alias": "management",
        },
        "222222222222": {"account_number": "222222222222", "account_alias": "member"},
    }
    base_session = object()

    resolver = OrganizationResolver(
        descriptor=_target(regions=["us-east-1", "us-west-2"]),
        context=_context(regions=["us-east-1", "us-west-2"]),
        management_account_id="111111111111",
        base_session_account_id="111111111111",
        session_factory=FailingSessionFactory(),
        base_session=base_session,
        discovered_accounts=discovered_accounts,
        region_statuses={"us-east-1": "ENABLED_BY_DEFAULT"},
    )

    accounts = resolver.resolve_accounts()

    assert [account.account_id for account in accounts] == [
        "111111111111",
        "222222222222",
    ]
    assert accounts[0].is_management is True
    assert accounts[0].access_strategy is AccountAccessStrategy.BASE_SESSION
    assert accounts[1].is_management is False
    assert accounts[1].access_strategy is AccountAccessStrategy.ASSUME_ROLE
    assert accounts[1].role_name == DEFAULT_ORGANIZATION_ROLE_NAME
    assert accounts[0]._regions == ["us-east-1"]
    assert accounts[1]._regions == ["us-east-1"]


def test_resolve_accounts_uses_base_session_account_direct_mode_for_delegated_admin():
    resolver = OrganizationResolver(
        descriptor=_target(),
        context=_context(),
        management_account_id="111111111111",
        base_session_account_id="222222222222",
        base_session=object(),
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

    accounts = resolver.resolve_accounts()

    assert accounts[0].is_management is True
    assert [account.access_strategy for account in accounts] == [
        AccountAccessStrategy.ASSUME_ROLE,
        AccountAccessStrategy.BASE_SESSION,
    ]


def test_resolve_accounts_uses_base_session_account_direct_for_management_auth():
    resolver = OrganizationResolver(
        descriptor=_target(),
        context=_context(),
        management_account_id="111111111111",
        base_session_account_id="111111111111",
        base_session=object(),
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

    accounts = resolver.resolve_accounts()

    assert accounts[0].is_management is True
    assert [account.access_strategy for account in accounts] == [
        AccountAccessStrategy.BASE_SESSION,
        AccountAccessStrategy.ASSUME_ROLE,
    ]


def test_resolve_accounts_expands_all_region_selector():
    resolver = OrganizationResolver(
        descriptor=_target(regions=["all"]),
        context=_context(regions=["all"]),
        management_account_id="111111111111",
        base_session_account_id="111111111111",
        base_session=object(),
        discovered_accounts={
            "111111111111": {
                "account_number": "111111111111",
                "account_alias": "management",
            }
        },
        region_statuses={
            "us-east-1": "ENABLED_BY_DEFAULT",
            "us-west-1": "DISABLED",
            "us-west-2": "ENABLED",
        },
    )

    accounts = resolver.resolve_accounts()

    assert accounts[0]._regions == ["us-east-1", "us-west-2"]


def test_resolve_accounts_expands_glob_and_explicit_region_selectors(caplog):
    resolver = OrganizationResolver(
        descriptor=_target(regions=["us-*", "ca-central-1"]),
        context=_context(regions=["us-*", "ca-central-1"]),
        management_account_id="111111111111",
        base_session_account_id="111111111111",
        base_session=object(),
        discovered_accounts={
            "111111111111": {
                "account_number": "111111111111",
                "account_alias": "management",
            }
        },
        region_statuses={
            "ca-central-1": "ENABLED",
            "eu-west-1": "ENABLED",
            "us-east-1": "ENABLED_BY_DEFAULT",
            "us-west-1": "DISABLED",
            "us-west-2": "ENABLED",
        },
    )

    accounts = resolver.resolve_accounts()

    assert accounts[0]._regions == ["us-east-1", "us-west-2", "ca-central-1"]
    assert "configured unavailable regions: us-west-1" in caplog.text


def test_resolve_accounts_rejects_glob_matching_no_known_regions():
    resolver = OrganizationResolver(
        descriptor=_target(regions=["moon-*"]),
        context=_context(regions=["moon-*"]),
        management_account_id="111111111111",
        base_session_account_id="111111111111",
        base_session=object(),
        discovered_accounts={},
        region_statuses={"us-east-1": "ENABLED_BY_DEFAULT"},
    )

    with pytest.raises(ValueError, match="matched no known regions"):
        resolver.resolve_accounts()


def test_resolve_accounts_raises_when_no_effective_regions_remain():
    resolver = OrganizationResolver(
        descriptor=_target(regions=["us-east-1"]),
        context=_context(regions=["us-east-1"]),
        management_account_id="111111111111",
        base_session_account_id="111111111111",
        base_session=object(),
        discovered_accounts={},
        region_statuses={"us-west-2": "ENABLED"},
    )

    with pytest.raises(ValueError, match="No effective configured regions"):
        resolver.resolve_accounts()


def test_resolve_accounts_raises_when_selector_matches_only_disabled_regions(caplog):
    resolver = OrganizationResolver(
        descriptor=_target(regions=["ap-*"]),
        context=_context(regions=["ap-*"]),
        management_account_id="111111111111",
        base_session_account_id="111111111111",
        base_session=object(),
        discovered_accounts={},
        region_statuses={"ap-south-1": "DISABLED", "us-east-1": "ENABLED"},
    )

    with pytest.raises(ValueError, match="No effective configured regions"):
        resolver.resolve_accounts()

    assert "configured unavailable regions: ap-south-1" in caplog.text
