from __future__ import annotations

import sys
from dataclasses import dataclass
from types import ModuleType

import pytest

from anvil.actions import ActionRecorder
from anvil.providers.azure.tasks.count_resource_groups import run


@dataclass(frozen=True)
class FakeResourceGroup:
    name: str
    location: str
    id: str


@dataclass(frozen=True)
class FakeAzureSession:
    subscription_id: str = "sub-a"
    location: str = "eastus"
    credential: object = object()


class FakeResourceGroups:
    def __init__(self, resource_groups: list[object]) -> None:
        self._resource_groups = resource_groups

    def list(self) -> list[object]:
        return list(self._resource_groups)


class FakeResourceManagementClient:
    calls: list[dict[str, object]] = []
    resource_groups: list[object] = []

    def __init__(self, credential: object, subscription_id: str) -> None:
        self.calls.append(
            {"credential": credential, "subscription_id": subscription_id}
        )
        self.resource_groups = FakeResourceGroups(type(self).resource_groups)


@pytest.fixture
def fake_azure_resource_sdk(monkeypatch):
    azure_module = ModuleType("azure")
    azure_mgmt_module = ModuleType("azure.mgmt")
    azure_mgmt_resource_module = ModuleType("azure.mgmt.resource")
    azure_mgmt_resource_module.ResourceManagementClient = FakeResourceManagementClient

    monkeypatch.setitem(sys.modules, "azure", azure_module)
    monkeypatch.setitem(sys.modules, "azure.mgmt", azure_mgmt_module)
    monkeypatch.setitem(sys.modules, "azure.mgmt.resource", azure_mgmt_resource_module)

    FakeResourceManagementClient.calls = []
    FakeResourceManagementClient.resource_groups = []
    return FakeResourceManagementClient


def _run_task(
    *, session: FakeAzureSession, dry_run: bool = False
) -> tuple[dict, list[str]]:
    actions = ActionRecorder(actions=[])
    result = run(
        provider="azure",
        execution_target_id=session.subscription_id,
        execution_target_name=session.subscription_id,
        execution_target_type="subscription",
        region=session.location,
        location=session.location,
        session=session,
        dry_run=dry_run,
        metadata={},
        actions=actions,
    )
    return result, actions.actions


def test_count_resource_groups_counts_and_lists_small_subscriptions(
    fake_azure_resource_sdk,
):
    session = FakeAzureSession()
    fake_azure_resource_sdk.resource_groups = [
        FakeResourceGroup(
            name="rg-a",
            location="eastus",
            id="/subscriptions/sub-a/resourceGroups/rg-a",
        ),
        FakeResourceGroup(
            name="rg-b",
            location="westus2",
            id="/subscriptions/sub-a/resourceGroups/rg-b",
        ),
    ]

    result, actions = _run_task(session=session)

    assert fake_azure_resource_sdk.calls == [
        {"credential": session.credential, "subscription_id": "sub-a"}
    ]
    assert result == {
        "subscription_id": "sub-a",
        "location": "eastus",
        "resource_group_count": 2,
        "resource_groups": [
            {
                "name": "rg-a",
                "location": "eastus",
                "id": "/subscriptions/sub-a/resourceGroups/rg-a",
            },
            {
                "name": "rg-b",
                "location": "westus2",
                "id": "/subscriptions/sub-a/resourceGroups/rg-b",
            },
        ],
    }
    assert actions == [
        "Counted 2 Azure resource group(s) in subscription sub-a location eastus"
    ]


def test_count_resource_groups_omits_large_resource_group_list(fake_azure_resource_sdk):
    fake_azure_resource_sdk.resource_groups = [
        {"name": f"rg-{index}", "location": "eastus", "id": f"rg-{index}"}
        for index in range(101)
    ]

    result, actions = _run_task(session=FakeAzureSession(), dry_run=True)

    assert result == {
        "subscription_id": "sub-a",
        "location": "eastus",
        "resource_group_count": 101,
    }
    assert actions == [
        "Counted 101 Azure resource group(s) in subscription sub-a location eastus"
    ]


def test_count_resource_groups_imports_azure_sdk_lazily(monkeypatch):
    real_import = __import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "azure.mgmt.resource":
            raise ImportError("missing azure mgmt resource")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr("builtins.__import__", fake_import)

    with pytest.raises(RuntimeError, match=r"azure-mgmt-resource.*anvil\[azure\]"):
        _run_task(session=FakeAzureSession())


def test_count_resource_groups_requires_azure_subscription_target(
    fake_azure_resource_sdk,
):
    with pytest.raises(RuntimeError, match="azure provider"):
        run(
            provider="aws",
            execution_target_id="123456789012",
            execution_target_name="test",
            execution_target_type="account",
            region="us-east-1",
            location="us-east-1",
            session=FakeAzureSession(),
            dry_run=False,
            metadata={},
            actions=ActionRecorder(actions=[]),
        )
