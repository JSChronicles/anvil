from __future__ import annotations

import pytest

from anvil.descriptors import ConfigBranch, TargetDescriptor
from anvil.providers.aws import AwsProvider


class FakePaginator:
    def __init__(self, pages: list[dict]) -> None:
        self._pages = pages

    def paginate(self):
        return iter(self._pages)


class FakeClient:
    def __init__(self, *, paginator: FakePaginator) -> None:
        self._paginator = paginator

    def get_paginator(self, name: str) -> FakePaginator:
        assert name == "list_regions"
        return self._paginator


class FakeSession:
    def __init__(self) -> None:
        self.client_calls = []

    def client(self, service_name, **kwargs):
        self.client_calls.append((service_name, kwargs))
        assert service_name == "account"
        return FakeClient(
            paginator=FakePaginator(
                [
                    {
                        "Regions": [
                            {"RegionName": "us-west-2", "RegionOptStatus": "ENABLED"},
                            {
                                "RegionName": "us-east-1",
                                "RegionOptStatus": "ENABLED_BY_DEFAULT",
                            },
                            {"RegionName": "ap-south-1", "RegionOptStatus": "DISABLED"},
                            {"RegionOptStatus": "ENABLED"},
                        ]
                    }
                ]
            )
        )


def test_aws_provider_default_regions_preserves_descriptor_default():
    target = TargetDescriptor(config_branch=ConfigBranch.ORGANIZATIONS, name="org-a")

    assert AwsProvider().default_regions(target) == ["us-east-1"]


def test_aws_provider_bootstrap_region_prefers_first_explicit_region():
    provider = AwsProvider()

    assert (
        provider.bootstrap_region(configured_regions=["us-*", "ca-central-1"])
        == "ca-central-1"
    )


def test_aws_provider_bootstrap_region_falls_back_for_selectors_only():
    assert AwsProvider().bootstrap_region(configured_regions=["all"]) == "us-east-1"


def test_aws_provider_discovers_region_statuses_once_from_existing_session():
    session = FakeSession()

    statuses = AwsProvider().discover_region_statuses(session=session)

    assert statuses == {
        "ap-south-1": "DISABLED",
        "us-east-1": "ENABLED_BY_DEFAULT",
        "us-west-2": "ENABLED",
    }
    assert len(session.client_calls) == 1


def test_aws_provider_discover_regions_adapts_availability(monkeypatch):
    session = FakeSession()

    class FakeSessionFactory:
        def create_base_session(self, **kwargs):
            assert kwargs["region_name"] == "us-east-1"
            return session

    monkeypatch.setattr(
        "anvil.providers.aws.provider.SessionFactory", FakeSessionFactory
    )

    target = TargetDescriptor(
        config_branch=ConfigBranch.ORGANIZATIONS, name="org-a", regions=["all"]
    )

    regions = AwsProvider().discover_regions(target)

    assert [(region.name, region.available, region.status) for region in regions] == [
        ("ap-south-1", False, "DISABLED"),
        ("us-east-1", True, "ENABLED_BY_DEFAULT"),
        ("us-west-2", True, "ENABLED"),
    ]
    assert len(session.client_calls) == 1


def test_aws_provider_resolves_all_selector_like_v0292():
    regions = AwsProvider().resolve_regions(
        target_name="org-a",
        configured_regions=["all"],
        region_statuses={
            "us-east-1": "ENABLED_BY_DEFAULT",
            "us-west-1": "DISABLED",
            "us-west-2": "ENABLED",
        },
    )

    assert regions == ["us-east-1", "us-west-2"]


def test_aws_provider_resolves_glob_and_explicit_selectors_like_v0292(caplog):
    regions = AwsProvider().resolve_regions(
        target_name="org-a",
        configured_regions=["us-*", "ca-central-1"],
        region_statuses={
            "ca-central-1": "ENABLED",
            "eu-west-1": "ENABLED",
            "us-east-1": "ENABLED_BY_DEFAULT",
            "us-west-1": "DISABLED",
            "us-west-2": "ENABLED",
        },
    )

    assert regions == ["us-east-1", "us-west-2", "ca-central-1"]
    assert "configured unavailable regions: us-west-1" in caplog.text


def test_aws_provider_rejects_selector_matching_no_known_regions():
    with pytest.raises(ValueError, match="matched no known regions"):
        AwsProvider().resolve_regions(
            target_name="org-a",
            configured_regions=["moon-*"],
            region_statuses={"us-east-1": "ENABLED_BY_DEFAULT"},
        )
