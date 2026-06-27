from __future__ import annotations

import builtins
from dataclasses import dataclass

import pytest

from anvil.descriptors import ConfigBranch, TargetDescriptor
from anvil.execution_context import ExecutionContext
from anvil.providers.azure.provider import (
    AzureExecutionTargetData,
    AzureProvider,
    AzureSessionFactory,
)


@dataclass(frozen=True)
class FakeSession:
    subscription_id: str
    location: str


class FakeSessionFactory:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def create_session(self, *, subscription_id: str, location: str) -> FakeSession:
        self.calls.append({"subscription_id": subscription_id, "location": location})
        return FakeSession(subscription_id=subscription_id, location=location)


def _target(**overrides) -> TargetDescriptor:
    values = {
        "config_branch": ConfigBranch.ACCOUNTS,
        "name": "azure-subscriptions",
        "include": ["sub-a"],
    }
    values.update(overrides)
    return TargetDescriptor(**values)


def _context() -> ExecutionContext:
    return ExecutionContext(
        regions=["eastus"], role_name=None, dry_run=False, tasks=[], metadata={}
    )


def test_azure_provider_metadata_and_default_locations():
    provider = AzureProvider()

    assert provider.metadata.name == "azure"
    assert provider.default_regions(_target()) == ["eastus"]
    assert provider.default_regions(_target(regions=["westus2"])) == ["westus2"]
    assert [region.name for region in provider.discover_regions(_target())] == [
        "eastus"
    ]


def test_azure_provider_rejects_organization_targets():
    provider = AzureProvider()
    target = TargetDescriptor(config_branch=ConfigBranch.ORGANIZATIONS, name="mgmt")

    with pytest.raises(ValueError, match="explicit subscriptions"):
        provider.validate_target(target)


def test_azure_resolves_explicit_subscription_targets_deterministically():
    session_factory = FakeSessionFactory()
    provider = AzureProvider(session_factory=session_factory)
    target = _target(include=["sub-b"], role_name="descriptor-compat")

    plan = provider.resolve_execution_targets(
        target=target,
        regions=["eastus", "westus2"],
        include=["sub-a", "sub-b"],
        exclude=None,
    )

    assert plan.exclusive_execution_key is None
    assert [execution_target.id for execution_target in plan.execution_targets] == [
        "sub-a",
        "sub-b",
    ]
    assert [execution_target.type for execution_target in plan.execution_targets] == [
        "subscription",
        "subscription",
    ]
    assert all(
        isinstance(execution_target.provider_data, AzureExecutionTargetData)
        for execution_target in plan.execution_targets
    )


def test_azure_runtime_uses_injected_session_factory():
    session_factory = FakeSessionFactory()
    provider = AzureProvider(session_factory=session_factory)
    target = _target()
    execution_target = provider.resolve_execution_targets(
        target=target, regions=["eastus"], include=target.include, exclude=None
    ).execution_targets[0]

    runtime = provider.prepare_execution_runtime(
        target=target, execution_target=execution_target, context=_context()
    )

    assert runtime.build_session(region="eastus") == FakeSession(
        subscription_id="sub-a", location="eastus"
    )
    assert session_factory.calls == [{"subscription_id": "sub-a", "location": "eastus"}]


def test_azure_session_factory_imports_sdk_only_when_session_is_built(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "azure.identity":
            raise ImportError("missing azure identity")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(RuntimeError, match="azure-identity"):
        AzureSessionFactory().create_session(subscription_id="sub-a", location="eastus")
