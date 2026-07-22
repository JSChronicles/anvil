from __future__ import annotations

import builtins
import sys
from dataclasses import dataclass
from types import ModuleType
from types import SimpleNamespace

import pytest

from anvil.descriptors import ConfigBranch, TargetDescriptor
from anvil.execution_context import ExecutionContext
from anvil.providers.azure.provider import (
    AzureExecutionTargetData,
    AzureProvider,
    AzureSubscription,
    AzureSessionFactory,
)
from anvil.providers.base import ProviderRegion
from anvil.results import ExecutionStatus


@dataclass(frozen=True)
class FakeSession:
    subscription_id: str
    location: str


class FakeSessionFactory:
    def __init__(self, *, subscriptions: list[AzureSubscription] | None = None) -> None:
        self.calls: list[dict[str, str | None]] = []
        self.list_calls: list[dict[str, str | None]] = []
        self.location_calls: list[dict[str, str | None]] = []
        self.subscriptions = subscriptions or [
            AzureSubscription(subscription_id="sub-a"),
            AzureSubscription(subscription_id="sub-b"),
        ]

    def create_session(
        self,
        *,
        subscription_id: str,
        location: str,
        tenant_id: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
    ) -> FakeSession:
        self.calls.append(
            {
                "subscription_id": subscription_id,
                "location": location,
                "tenant_id": tenant_id,
                "client_id": client_id,
                "client_secret": client_secret,
            }
        )
        return FakeSession(subscription_id=subscription_id, location=location)

    def validate_auth(
        self,
        *,
        tenant_id: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
    ) -> None:
        self.calls.append(
            {
                "subscription_id": None,
                "location": None,
                "tenant_id": tenant_id,
                "client_id": client_id,
                "client_secret": client_secret,
            }
        )

    def list_subscriptions(
        self,
        *,
        tenant_id: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
    ) -> list[AzureSubscription]:
        self.list_calls.append(
            {
                "tenant_id": tenant_id,
                "client_id": client_id,
                "client_secret": client_secret,
            }
        )
        return list(self.subscriptions)

    def list_locations(
        self,
        *,
        subscription_id: str,
        tenant_id: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
    ) -> list[ProviderRegion]:
        self.location_calls.append(
            {
                "subscription_id": subscription_id,
                "tenant_id": tenant_id,
                "client_id": client_id,
                "client_secret": client_secret,
            }
        )
        return [
            ProviderRegion(name="centralus", status="available"),
            ProviderRegion(name="eastus", status="available"),
            ProviderRegion(name="eastus2", status="available"),
            ProviderRegion(name="westus2", status="available"),
        ]


def _target(**overrides) -> TargetDescriptor:
    values = {
        "config_branch": ConfigBranch.TARGETS,
        "name": "azure-subscriptions",
        "provider": "azure",
        "mode": "subscriptions",
        "include": ["sub-a"],
    }
    values.update(overrides)
    if values.get("include") is None:
        values["mode"] = "tenant"
    return TargetDescriptor(**values)


def _raw_target(**overrides):
    values = {
        "config_branch": ConfigBranch.TARGETS,
        "include": ["sub-a"],
        "exclude": None,
        "provider": "azure",
        "provider_options": {},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _context() -> ExecutionContext:
    return ExecutionContext(
        regions=["eastus"], role_name=None, dry_run=False, tasks=[], metadata={}
    )


def test_azure_provider_metadata_and_default_locations():
    provider = AzureProvider()

    assert provider.metadata.name == "azure"
    assert provider.metadata.default_regions == ("eastus",)
    assert provider.metadata.supported_task_scopes == frozenset({"region", "target"})
    assert [region.name for region in provider.discover_regions(_target())] == [
        "eastus"
    ]


def test_azure_provider_rejects_organization_targets():
    provider = AzureProvider()
    target = TargetDescriptor(config_branch=ConfigBranch.TARGETS, name="mgmt")

    with pytest.raises(ValueError, match="provider 'azure'"):
        provider.validate_target(target)


def test_azure_provider_rejects_tenant_id_without_client_secret():
    provider = AzureProvider()
    target = _raw_target(provider_options={"tenant_id": "tenant-a"})

    with pytest.raises(ValueError, match="tenant_id.*client_secret"):
        provider.validate_target(target)


def test_azure_provider_rejects_client_secret_without_tenant_and_client_id():
    provider = AzureProvider()
    target = _raw_target(provider_options={"client_secret": "secret-a"})

    with pytest.raises(ValueError, match="client_secret.*tenant_id.*client_id"):
        provider.validate_target(target)


def test_azure_resolves_explicit_subscription_targets_deterministically():
    session_factory = FakeSessionFactory(
        subscriptions=[
            AzureSubscription(subscription_id="sub-a", display_name="Subscription A"),
            AzureSubscription(subscription_id="sub-b", display_name="Subscription B"),
        ]
    )
    provider = AzureProvider(session_factory=session_factory)
    target = _target(include=["sub-b"])

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
    assert [execution_target.name for execution_target in plan.execution_targets] == [
        "Subscription A",
        "Subscription B",
    ]
    assert [execution_target.type for execution_target in plan.execution_targets] == [
        "subscription",
        "subscription",
    ]
    assert all(
        isinstance(execution_target.provider_data, AzureExecutionTargetData)
        for execution_target in plan.execution_targets
    )
    assert session_factory.list_calls == [
        {"tenant_id": None, "client_id": None, "client_secret": None}
    ]
    assert session_factory.location_calls == []


def test_azure_subscription_targets_exclude_each_selected_subscription():
    provider = AzureProvider(session_factory=FakeSessionFactory())
    target = _target(include=["sub-a", "sub-b"])

    keys = provider.execution_exclusion_keys(
        target=target, include=["sub-b", "sub-c"], exclude=None
    )

    assert keys == (
        ("azure", "subscription", "sub-b"),
        ("azure", "subscription", "sub-c"),
    )


def test_azure_tenant_preflight_discovers_selected_subscriptions_for_exclusion():
    provider = AzureProvider(session_factory=FakeSessionFactory())
    target = _target(
        mode="tenant",
        include=["sub-a"],
        provider_options={
            "tenant_id": "tenant-a",
            "client_id": "client-a",
            "client_secret": "secret-a",
        },
    )

    result = provider.preflight_execution(
        target=target, regions=["eastus"], include=target.include, exclude=None
    )

    assert result.data is not None
    assert [
        subscription.subscription_id for subscription in result.data.subscriptions
    ] == ["sub-a"]
    assert result.exclusive_execution_keys == (("azure", "subscription", "sub-a"),)


def test_azure_tenant_preflight_without_tenant_id_uses_default_credential_discovery():
    provider = AzureProvider(session_factory=FakeSessionFactory())
    target = _target(mode="tenant", include=["sub-a"])

    result = provider.preflight_execution(
        target=target, regions=["eastus"], include=target.include, exclude=None
    )

    assert result.data is not None
    assert [
        subscription.subscription_id for subscription in result.data.subscriptions
    ] == ["sub-a"]
    assert result.exclusive_execution_keys == (("azure", "subscription", "sub-a"),)


def test_azure_preflight_records_location_validation_benchmark_data():
    session_factory = FakeSessionFactory(
        subscriptions=[
            AzureSubscription(subscription_id="sub-a"),
            AzureSubscription(subscription_id="sub-b"),
        ]
    )
    provider = AzureProvider(session_factory=session_factory)
    target = _target(mode="tenant", include=["sub-a"])
    benchmark: dict[str, object] = {}

    result = provider.preflight_execution(
        target=target,
        regions=["eastus", "westus2"],
        include=target.include,
        exclude=None,
        benchmark=benchmark,
    )

    assert result.data is not None
    assert result.data.location_statuses_by_subscription == {
        "sub-a": {
            "centralus": "available",
            "eastus": "available",
            "eastus2": "available",
            "westus2": "available",
        }
    }
    assert benchmark["azure_selected_subscription_count"] == 1
    assert benchmark["azure_validated_subscription_count"] == 1
    assert benchmark["azure_selected_location_count"] == 2
    assert benchmark["azure_discovered_location_count"] == 4
    assert benchmark["azure_discover_subscriptions_seconds"] >= 0.0
    assert benchmark["azure_discover_locations_seconds"] >= 0.0


def test_azure_explicit_subscription_names_fall_back_to_ids_when_lookup_fails():
    class FailingSessionFactory(FakeSessionFactory):
        def list_subscriptions(self, **kwargs):
            raise RuntimeError("denied")

    provider = AzureProvider(session_factory=FailingSessionFactory())
    target = _target(include=["sub-a"])

    plan = provider.resolve_execution_targets(
        target=target, regions=["eastus"], include=target.include, exclude=None
    )

    assert [execution_target.id for execution_target in plan.execution_targets] == [
        "sub-a"
    ]
    assert [execution_target.name for execution_target in plan.execution_targets] == [
        "sub-a"
    ]


def test_azure_resolves_location_selectors_per_subscription():
    session_factory = FakeSessionFactory()
    provider = AzureProvider(session_factory=session_factory)
    target = _target(
        include=["sub-a"],
        regions=["east*", "centralus"],
        provider_options={
            "tenant_id": "tenant-a",
            "client_id": "client-a",
            "client_secret": "secret-a",
        },
    )

    plan = provider.resolve_execution_targets(
        target=target, regions=target.regions, include=target.include, exclude=None
    )

    assert plan.execution_targets[0].provider_data.locations == [
        "eastus",
        "eastus2",
        "centralus",
    ]
    assert session_factory.location_calls == [
        {
            "subscription_id": "sub-a",
            "tenant_id": "tenant-a",
            "client_id": "client-a",
            "client_secret": "secret-a",
        }
    ]


def test_azure_resolves_all_locations_per_subscription():
    session_factory = FakeSessionFactory()
    provider = AzureProvider(session_factory=session_factory)
    target = _target(include=["sub-a"], regions=["all"])

    plan = provider.resolve_execution_targets(
        target=target, regions=target.regions, include=target.include, exclude=None
    )

    assert plan.execution_targets[0].provider_data.locations == [
        "centralus",
        "eastus",
        "eastus2",
        "westus2",
    ]


def test_azure_rejects_unknown_location_selector():
    provider = AzureProvider(session_factory=FakeSessionFactory())
    target = _target(include=["sub-a"], regions=["moon*"])

    with pytest.raises(ValueError, match="matched no known locations"):
        provider.resolve_execution_targets(
            target=target, regions=target.regions, include=target.include, exclude=None
        )


def test_azure_subscription_discovery_resolves_listed_subscriptions():
    session_factory = FakeSessionFactory(
        subscriptions=[
            AzureSubscription(subscription_id="sub-b", display_name="Subscription B"),
            AzureSubscription(subscription_id="sub-a", display_name="Subscription A"),
        ]
    )
    provider = AzureProvider(session_factory=session_factory)
    target = _target(include=None)

    plan = provider.resolve_execution_targets(
        target=target, regions=["eastus"], include=None, exclude=None
    )

    assert [execution_target.id for execution_target in plan.execution_targets] == [
        "sub-a",
        "sub-b",
    ]
    assert [execution_target.name for execution_target in plan.execution_targets] == [
        "Subscription A",
        "Subscription B",
    ]
    assert session_factory.list_calls == [
        {"tenant_id": None, "client_id": None, "client_secret": None}
    ]


def test_azure_resolve_execution_targets_reuses_preflight_subscriptions():
    session_factory = FakeSessionFactory(
        subscriptions=[
            AzureSubscription(subscription_id="sub-a", display_name="Subscription A"),
            AzureSubscription(subscription_id="sub-b", display_name="Subscription B"),
        ]
    )
    provider = AzureProvider(session_factory=session_factory)
    target = _target(include=None)
    preflight = provider.preflight_execution(
        target=target, regions=["eastus"], include=["sub-b"], exclude=None
    )

    plan = provider.resolve_execution_targets(
        target=target,
        regions=["eastus"],
        include=["sub-b"],
        exclude=None,
        preflight_data=preflight.data,
    )

    assert [execution_target.id for execution_target in plan.execution_targets] == [
        "sub-b"
    ]
    assert [execution_target.name for execution_target in plan.execution_targets] == [
        "Subscription B"
    ]
    assert session_factory.list_calls == [
        {"tenant_id": None, "client_id": None, "client_secret": None}
    ]


def test_azure_tenant_mode_discovers_subscriptions_with_filters():
    session_factory = FakeSessionFactory(
        subscriptions=[
            AzureSubscription(subscription_id="sub-a"),
            AzureSubscription(subscription_id="sub-b"),
        ]
    )
    provider = AzureProvider(session_factory=session_factory)
    target = _target(
        config_branch=ConfigBranch.TARGETS,
        mode="tenant",
        include=["sub-b"],
        provider_options={
            "tenant_id": "tenant-a",
            "client_id": "client-a",
            "client_secret": "secret-a",
        },
    )

    plan = provider.resolve_execution_targets(
        target=target, regions=["eastus"], include=target.include, exclude=None
    )

    assert [execution_target.id for execution_target in plan.execution_targets] == [
        "sub-b"
    ]
    assert session_factory.list_calls == [
        {"tenant_id": "tenant-a", "client_id": "client-a", "client_secret": "secret-a"}
    ]


def test_azure_subscription_discovery_applies_include_and_exclude_filters():
    session_factory = FakeSessionFactory(
        subscriptions=[
            AzureSubscription(subscription_id="sub-a"),
            AzureSubscription(subscription_id="sub-b"),
            AzureSubscription(subscription_id="sub-c"),
        ]
    )
    provider = AzureProvider(session_factory=session_factory)
    target = _target(include=None)

    included_plan = provider.resolve_execution_targets(
        target=target, regions=["eastus"], include=["sub-c", "sub-a"], exclude=None
    )
    excluded_plan = provider.resolve_execution_targets(
        target=target, regions=["eastus"], include=None, exclude=["sub-b"]
    )

    assert [
        execution_target.id for execution_target in included_plan.execution_targets
    ] == ["sub-c", "sub-a"]
    assert [
        execution_target.id for execution_target in excluded_plan.execution_targets
    ] == ["sub-a", "sub-c"]


def test_azure_subscription_discovery_reports_unknown_filters():
    provider = AzureProvider(session_factory=FakeSessionFactory())
    target = _target(include=None)

    with pytest.raises(ValueError, match="unknown subscription IDs: missing-sub"):
        provider.resolve_execution_targets(
            target=target, regions=["eastus"], include=["missing-sub"], exclude=None
        )


def test_azure_subscription_discovery_errors_are_actionable():
    class FailingSessionFactory(FakeSessionFactory):
        def list_subscriptions(self, **kwargs):
            raise RuntimeError("Azure provider could not discover subscriptions: boom")

    provider = AzureProvider(session_factory=FailingSessionFactory())
    target = _target(include=None)

    with pytest.raises(RuntimeError, match="could not discover subscriptions: boom"):
        provider.resolve_execution_targets(
            target=target, regions=["eastus"], include=None, exclude=None
        )


def test_azure_runtime_uses_injected_session_factory():
    session_factory = FakeSessionFactory()
    provider = AzureProvider(session_factory=session_factory)
    target = _target(
        provider_options={
            "tenant_id": "tenant-a",
            "client_id": "client-a",
            "client_secret": "secret-a",
        }
    )
    execution_target = provider.resolve_execution_targets(
        target=target, regions=["eastus"], include=target.include, exclude=None
    ).execution_targets[0]

    runtime = provider.prepare_execution_runtime(
        target=target, execution_target=execution_target, context=_context()
    )

    assert runtime.build_session(region="eastus") == FakeSession(
        subscription_id="sub-a", location="eastus"
    )
    assert session_factory.calls == [
        {
            "subscription_id": "sub-a",
            "location": "eastus",
            "tenant_id": "tenant-a",
            "client_id": "client-a",
            "client_secret": "secret-a",
        }
    ]


def test_azure_session_factory_imports_sdk_only_when_session_is_built(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "azure.identity":
            raise ImportError("missing azure identity")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(RuntimeError, match=r"azure-identity.*anvil\[azure\]"):
        AzureSessionFactory().create_session(subscription_id="sub-a", location="eastus")


def test_azure_auth_check_reports_missing_identity_dependency(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "azure.identity":
            raise ImportError("missing azure identity")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    result = AzureProvider().auth_check(_target())

    assert result.status is ExecutionStatus.ERROR
    assert result.source == "azure"
    assert "azure-identity" in result.message
    assert "uv sync --extra azure" in result.remediation


def test_azure_auth_check_validates_arm_token_with_session_factory():
    session_factory = FakeSessionFactory()

    result = AzureProvider(session_factory=session_factory).auth_check(
        _target(
            provider_options={
                "tenant_id": "tenant-a",
                "client_id": "client-a",
                "client_secret": "secret-a",
            }
        )
    )

    assert result.status is ExecutionStatus.SUCCESS
    assert result.source == "azure"
    assert result.message == "Azure ARM authentication validated."
    assert session_factory.calls == [
        {
            "subscription_id": None,
            "location": None,
            "tenant_id": "tenant-a",
            "client_id": "client-a",
            "client_secret": "secret-a",
        }
    ]


def test_azure_auth_check_reports_arm_token_validation_failure():
    class FailingAuthSessionFactory(FakeSessionFactory):
        def validate_auth(self, **kwargs):
            raise RuntimeError("Azure provider could not validate ARM authentication")

    result = AzureProvider(session_factory=FailingAuthSessionFactory()).auth_check(
        _target()
    )

    assert result.status is ExecutionStatus.ERROR
    assert result.source == "azure"
    assert "could not validate ARM authentication" in result.message


def test_azure_session_factory_uses_client_secret_credential(monkeypatch):
    class FakeClientSecretCredential:
        def __init__(self, *, tenant_id, client_id, client_secret):
            self.kwargs = {
                "tenant_id": tenant_id,
                "client_id": client_id,
                "client_secret": client_secret,
            }

    class FakeDefaultAzureCredential:
        def __init__(self, **kwargs):
            raise AssertionError("DefaultAzureCredential should not be used")

    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "azure.identity":
            return type(
                "_AzureIdentity",
                (),
                {
                    "ClientSecretCredential": FakeClientSecretCredential,
                    "DefaultAzureCredential": FakeDefaultAzureCredential,
                },
            )()
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    session = AzureSessionFactory().create_session(
        subscription_id="sub-a",
        location="eastus",
        tenant_id="tenant-a",
        client_id="client-a",
        client_secret="secret-a",
    )

    assert session.subscription_id == "sub-a"
    assert session.credential.kwargs == {
        "tenant_id": "tenant-a",
        "client_id": "client-a",
        "client_secret": "secret-a",
    }


def test_azure_session_factory_uses_managed_identity_client_id(monkeypatch):
    class FakeClientSecretCredential:
        pass

    class FakeDefaultAzureCredential:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "azure.identity":
            return type(
                "_AzureIdentity",
                (),
                {
                    "ClientSecretCredential": FakeClientSecretCredential,
                    "DefaultAzureCredential": FakeDefaultAzureCredential,
                },
            )()
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    session = AzureSessionFactory().create_session(
        subscription_id="sub-a", location="eastus", client_id="client-a"
    )

    assert session.credential.kwargs == {"managed_identity_client_id": "client-a"}


def test_azure_session_factory_list_locations_uses_subscription_sdk(monkeypatch):
    credential = object()
    credential_calls: list[dict[str, str]] = []
    client_credentials: list[object] = []

    class FakeDefaultAzureCredential:
        def __init__(self, **kwargs):
            credential_calls.append(kwargs)

    class FakeSubscriptionOperations:
        def list_locations(self, subscription_id):
            assert subscription_id == "sub-a"
            return [
                SimpleNamespace(name="eastus", display_name="East US"),
                SimpleNamespace(name="westus2", display_name="West US 2"),
            ]

    class FakeSubscriptionClient:
        def __init__(self, credential):
            client_credentials.append(credential)
            self.subscriptions = FakeSubscriptionOperations()

    identity_module = ModuleType("azure.identity")
    identity_module.ClientSecretCredential = lambda **kwargs: (_ for _ in ()).throw(
        AssertionError("ClientSecretCredential should not be used")
    )
    identity_module.DefaultAzureCredential = lambda **kwargs: credential
    subscriptions_module = ModuleType("azure.mgmt.resource.subscriptions")
    subscriptions_module.SubscriptionClient = FakeSubscriptionClient
    azure_module = ModuleType("azure")
    mgmt_module = ModuleType("azure.mgmt")
    resource_module = ModuleType("azure.mgmt.resource")

    monkeypatch.setitem(sys.modules, "azure", azure_module)
    monkeypatch.setitem(sys.modules, "azure.identity", identity_module)
    monkeypatch.setitem(sys.modules, "azure.mgmt", mgmt_module)
    monkeypatch.setitem(sys.modules, "azure.mgmt.resource", resource_module)
    monkeypatch.setitem(
        sys.modules, "azure.mgmt.resource.subscriptions", subscriptions_module
    )

    locations = AzureSessionFactory().list_locations(subscription_id="sub-a")

    assert [location.name for location in locations] == ["eastus", "westus2"]
    assert all(location.available for location in locations)
    assert [location.status for location in locations] == ["available", "available"]
    assert client_credentials == [credential]
    assert credential_calls == []


def test_azure_client_secret_requires_tenant_and_client_id(monkeypatch):
    class FakeClientSecretCredential:
        pass

    class FakeDefaultAzureCredential:
        pass

    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "azure.identity":
            return type(
                "_AzureIdentity",
                (),
                {
                    "ClientSecretCredential": FakeClientSecretCredential,
                    "DefaultAzureCredential": FakeDefaultAzureCredential,
                },
            )()
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(RuntimeError, match="client_secret requires tenant_id"):
        AzureSessionFactory().create_session(
            subscription_id="sub-a", location="eastus", client_secret="secret-a"
        )
