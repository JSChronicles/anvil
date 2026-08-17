from __future__ import annotations

import builtins
import sys
from dataclasses import dataclass
from types import ModuleType, SimpleNamespace

import pytest

from anvil.descriptors import TargetDescriptor
from anvil.execution_context import ExecutionContext
from anvil.providers.base import ExecutionTarget
from anvil.providers.datadog.config import (
    DEFAULT_API_KEY_ENV,
    DEFAULT_APP_KEY_ENV,
    SUPPORTED_SITES,
)
from anvil.providers.datadog.provider import DatadogExecutionTargetData, DatadogProvider
from anvil.providers.datadog.session import (
    DatadogAuthSettings,
    DatadogDependencyError,
    DatadogProviderError,
    DatadogSession,
    DatadogSessionFactory,
)
from anvil.results import ExecutionStatus
from anvil.runner import _execute_provider_execution_target
from anvil.task_loader import ResolvedTask, TaskScope


@dataclass
class FakeClient:
    """Minimal closable generated-client double."""

    configuration: object | None = None
    closed: bool = False

    def close(self) -> None:
        """Record client cleanup."""

        self.closed = True


class PaginatedClient(FakeClient):
    """Fake SDK client exposing an operation-specific pagination iterator."""

    def __init__(self, *, pages: list[list[SimpleNamespace]]) -> None:
        super().__init__()
        self.pages = pages
        self.page_count = 0

    def list_monitors_with_pagination(self):
        """Yield every monitor from each mocked API page."""

        for page in self.pages:
            self.page_count += 1
            yield from page


class FakeSessionFactory:
    """Session-factory double for provider and runtime tests."""

    def __init__(self) -> None:
        self.validate_calls: list[object] = []
        self.session_calls: list[dict[str, object]] = []
        self.clients: list[FakeClient] = []

    def validate_auth(self, *, settings) -> str:
        """Record a successful authentication check."""

        self.validate_calls.append(settings)
        return f"environment:{settings.api_key_env}+{settings.app_key_env}"

    def create_session(self, *, target_id, region_name, settings):
        """Return a closable Datadog session double."""

        self.session_calls.append(
            {"target_id": target_id, "region_name": region_name, "settings": settings}
        )
        client = FakeClient()
        self.clients.append(client)
        return DatadogSession(
            target_id=target_id,
            region_name=region_name,
            site=settings.site,
            auth_source="environment:test",
            client=client,
        )


class PaginatedSessionFactory(FakeSessionFactory):
    """Return one session backed by a paginated SDK boundary double."""

    def __init__(self, *, pages: list[list[SimpleNamespace]]) -> None:
        super().__init__()
        self.paginated_client = PaginatedClient(pages=pages)

    def create_session(self, *, target_id, region_name, settings):
        """Return the paginated session used by a normal task invocation."""

        self.clients.append(self.paginated_client)
        return DatadogSession(
            target_id=target_id,
            region_name=region_name,
            site=settings.site,
            auth_source="environment:test",
            client=self.paginated_client,
        )


class FakePreparationCache:
    """Unused preparation-cache double required by the provider contract."""

    def get_or_create(self, *, key, create):
        """Fail if the no-preflight provider unexpectedly uses the cache."""

        raise AssertionError("Datadog preparation must not use shared discovery cache")


@pytest.fixture(autouse=True)
def _datadog_credentials(monkeypatch) -> None:
    monkeypatch.setenv(DEFAULT_API_KEY_ENV, "api-secret")
    monkeypatch.setenv(DEFAULT_APP_KEY_ENV, "app-secret")
    monkeypatch.delenv("DD_SITE", raising=False)


def _target(**overrides) -> TargetDescriptor:
    values = {
        "name": "production-observability",
        "provider": "datadog",
        "mode": "organization",
    }
    values.update(overrides)
    return TargetDescriptor(**values)


def _context() -> ExecutionContext:
    return ExecutionContext(regions=["global"], dry_run=False, tasks=[], metadata={})


def test_provider_metadata_and_global_coordinate() -> None:
    provider = DatadogProvider()

    assert provider.metadata.name == "datadog"
    assert provider.metadata.default_regions == ("global",)
    assert provider.metadata.supported_task_scopes == frozenset({"region", "target"})
    assert provider.discover_regions(_target())[0].name == "global"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"mode": "organizations"}, "Unsupported Datadog target mode"),
        ({"regions": ["us-east-1"]}, "only region 'global'"),
        ({"include": ["child"]}, "does not allow include or exclude"),
        (
            {"provider_options": {"site": "https://api.datadoghq.com"}},
            "must be a hostname",
        ),
        (
            {"provider_options": {"api_key_env": "NOT-AN-ENV"}},
            "valid environment variable name",
        ),
    ],
)
def test_validate_target_rejects_unsupported_shapes(overrides, message) -> None:
    with pytest.raises(ValueError, match=message):
        DatadogProvider().validate_target(_target(**overrides))


def test_auth_check_returns_actionable_missing_credential(monkeypatch) -> None:
    monkeypatch.delenv(DEFAULT_APP_KEY_ENV)

    result = DatadogProvider().auth_check(_target())

    assert result.status is ExecutionStatus.ERROR
    assert result.source == "environment"
    assert DEFAULT_APP_KEY_ENV in (result.message or "")
    assert "provider.options" in (result.remediation or "")
    assert "api-secret" not in repr(result)


def test_auth_check_uses_injected_factory_and_normalized_site() -> None:
    session_factory = FakeSessionFactory()
    provider = DatadogProvider(session_factory=session_factory)

    result = provider.auth_check(_target(provider_options={"site": "DATADOGHQ.EU"}))

    assert result.status is ExecutionStatus.SUCCESS
    assert result.source == "environment:DD_API_KEY+DD_APP_KEY"
    assert session_factory.validate_calls[0].site == "datadoghq.eu"


def test_session_factory_validates_both_keys_and_closes_probe_client(
    monkeypatch,
) -> None:
    probe_client = FakeClient()
    probe_calls: list[object] = []

    class FakeKeyManagementApi:
        def __init__(self, client) -> None:
            probe_calls.append(client)

        def validate_api_key(self):
            return SimpleNamespace(status="ok")

    key_management_module = ModuleType("datadog_api_client.v2.api.key_management_api")
    key_management_module.KeyManagementApi = FakeKeyManagementApi
    monkeypatch.setitem(
        sys.modules,
        "datadog_api_client.v2.api.key_management_api",
        key_management_module,
    )
    monkeypatch.setattr(
        DatadogSessionFactory,
        "_create_api_client",
        staticmethod(lambda *, auth_settings: probe_client),
    )
    settings = (
        DatadogProvider()
        .resolve_execution_targets(
            target=_target(), regions=["global"], include=None, exclude=None
        )
        .execution_targets[0]
        .provider_data.settings
    )

    source = DatadogSessionFactory().validate_auth(settings=settings)

    assert source == "environment:DD_API_KEY+DD_APP_KEY"
    assert probe_calls == [probe_client]
    assert probe_client.closed is True


def test_session_factory_reports_missing_optional_dependency(monkeypatch) -> None:
    original_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "datadog_api_client" or name.startswith("datadog_api_client."):
            raise ImportError("Datadog SDK intentionally unavailable")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    settings = (
        DatadogProvider()
        .resolve_execution_targets(
            target=_target(), regions=["global"], include=None, exclude=None
        )
        .execution_targets[0]
        .provider_data.settings
    )

    with pytest.raises(DatadogDependencyError, match="anvil\\[datadog\\]"):
        DatadogSessionFactory().create_session(
            target_id="production-observability",
            region_name="global",
            settings=settings,
        )


def test_auth_cache_identity_changes_when_key_rotates(monkeypatch) -> None:
    provider = DatadogProvider()
    target = _target()

    initial = provider.auth_cache_key(target)
    monkeypatch.setenv(DEFAULT_API_KEY_ENV, "rotated-api-secret")

    assert provider.auth_cache_key(target) != initial
    assert "api-secret" not in repr(initial)


def test_resolve_execution_target_is_typed_and_deterministic() -> None:
    session_factory = FakeSessionFactory()
    provider = DatadogProvider(session_factory=session_factory)
    target = _target(provider_options={"site": "us5.datadoghq.com"})

    first = provider.resolve_execution_targets(
        target=target, regions=["global"], include=None, exclude=None
    )
    second = provider.resolve_execution_targets(
        target=target, regions=["global"], include=None, exclude=None
    )

    assert first.execution_targets == second.execution_targets
    execution_target = first.execution_targets[0]
    assert (
        execution_target.id,
        execution_target.name,
        execution_target.type,
        execution_target.provider,
        execution_target.regions,
    ) == (
        "production-observability",
        "production-observability",
        "organization",
        "datadog",
        ["global"],
    )
    assert execution_target.metadata == {
        "datadog_organization": "production-observability",
        "datadog_site": "us5.datadoghq.com",
    }
    assert isinstance(execution_target.provider_data, DatadogExecutionTargetData)


def test_prepare_target_returns_empty_provider_state() -> None:
    preparation = DatadogProvider().prepare_target(
        target=_target(),
        context=_context(),
        include=None,
        exclude=None,
        cache=FakePreparationCache(),
        benchmark=None,
    )

    assert preparation.data is None
    assert preparation.exclusive_execution_keys == ()


def test_runtime_builds_task_session_and_closes_client() -> None:
    session_factory = FakeSessionFactory()
    provider = DatadogProvider(session_factory=session_factory)
    target = _target()
    execution_target = provider.resolve_execution_targets(
        target=target, regions=["global"], include=None, exclude=None
    ).execution_targets[0]

    runtime = provider.prepare_execution_runtime(
        target=target, execution_target=execution_target, context=_context()
    )
    session = runtime.build_session(region="global")

    assert session.target_id == "production-observability"
    assert session.region_name == "global"
    assert session.site == "datadoghq.com"
    assert session.client is session_factory.clients[0]
    runtime.close()
    assert session_factory.clients[0].closed is True


def test_prepare_runtime_rejects_foreign_execution_target() -> None:
    execution_target = ExecutionTarget(
        id="foreign",
        name="foreign",
        type="organization",
        provider="github",
        regions=["global"],
    )

    with pytest.raises(ValueError, match="is not datadog"):
        DatadogProvider().prepare_execution_runtime(
            target=_target(), execution_target=execution_target, context=_context()
        )


def test_session_factory_configures_generated_client_and_keys(monkeypatch) -> None:
    created_clients: list[FakeClient] = []

    class FakeConfiguration:
        def __init__(self) -> None:
            self.server_variables: dict[str, str] = {}
            self.api_key: dict[str, str] = {}

    def fake_api_client(configuration):
        client = FakeClient(configuration=configuration)
        created_clients.append(client)
        return client

    sdk_module = ModuleType("datadog_api_client")
    sdk_module.ApiClient = fake_api_client
    sdk_module.Configuration = FakeConfiguration
    monkeypatch.setitem(sys.modules, "datadog_api_client", sdk_module)

    provider = DatadogProvider()
    target = _target(provider_options={"site": "ap2.datadoghq.com"})
    execution_target = provider.resolve_execution_targets(
        target=target, regions=["global"], include=None, exclude=None
    ).execution_targets[0]
    runtime = provider.prepare_execution_runtime(
        target=target, execution_target=execution_target, context=_context()
    )

    session = runtime.build_session(region="global")
    configuration = session.client.configuration

    assert configuration.server_variables == {"site": "ap2.datadoghq.com"}
    assert configuration.api_key == {
        "apiKeyAuth": "api-secret",
        "appKeyAuth": "app-secret",
    }
    assert "api-secret" not in repr(session)
    assert "app-secret" not in repr(session)
    runtime.close()
    assert created_clients[0].closed is True


@pytest.mark.parametrize("site", sorted(SUPPORTED_SITES))
def test_validate_target_accepts_every_sdk_supported_site(site: str) -> None:
    """Keep provider site validation aligned with the generated SDK."""

    DatadogProvider().validate_target(_target(provider_options={"site": site}))


def test_validate_target_rejects_hostname_not_supported_by_sdk() -> None:
    with pytest.raises(ValueError, match="Unsupported Datadog site"):
        DatadogProvider().validate_target(
            _target(provider_options={"site": "demo.datadoghq.com"})
        )


def test_environment_site_and_custom_credential_sources_are_resolved(
    monkeypatch,
) -> None:
    monkeypatch.setenv("DD_SITE", "US3.DATADOGHQ.COM")
    monkeypatch.setenv("TEAM_API_KEY", " team-api-secret ")
    monkeypatch.setenv("TEAM_APP_KEY", " team-app-secret ")
    target = _target(
        provider_options={"api_key_env": "TEAM_API_KEY", "app_key_env": "TEAM_APP_KEY"}
    )
    data = (
        DatadogProvider()
        .resolve_execution_targets(
            target=target, regions=["global"], include=None, exclude=None
        )
        .execution_targets[0]
        .provider_data
    )

    auth_settings = DatadogSessionFactory().resolve_auth_settings(
        settings=data.settings
    )

    assert isinstance(auth_settings, DatadogAuthSettings)
    assert data.settings.site == "us3.datadoghq.com"
    assert auth_settings.api_key == "team-api-secret"
    assert auth_settings.app_key == "team-app-secret"
    assert "team-api-secret" not in repr(auth_settings)
    assert "team-app-secret" not in repr(auth_settings)


def test_auth_check_reports_all_missing_credentials(monkeypatch) -> None:
    monkeypatch.delenv(DEFAULT_API_KEY_ENV)
    monkeypatch.delenv(DEFAULT_APP_KEY_ENV)

    result = DatadogProvider().auth_check(_target())

    assert result.status is ExecutionStatus.ERROR
    assert DEFAULT_API_KEY_ENV in (result.message or "")
    assert DEFAULT_APP_KEY_ENV in (result.message or "")


def test_authentication_failure_maps_status_and_redacts_secrets(monkeypatch) -> None:
    class FailingKeyManagementApi:
        def __init__(self, client) -> None:
            self.client = client

        def validate_api_key(self):
            error = RuntimeError(
                "rejected api-secret and app-secret with response headers"
            )
            error.status = 403
            error.reason = "invalid api-secret and app-secret"
            raise error

    key_management_module = ModuleType("datadog_api_client.v2.api.key_management_api")
    key_management_module.KeyManagementApi = FailingKeyManagementApi
    monkeypatch.setitem(
        sys.modules,
        "datadog_api_client.v2.api.key_management_api",
        key_management_module,
    )
    probe_client = FakeClient()
    monkeypatch.setattr(
        DatadogSessionFactory,
        "_create_api_client",
        staticmethod(lambda *, auth_settings: probe_client),
    )

    result = DatadogProvider().auth_check(_target())

    assert result.status is ExecutionStatus.ERROR
    assert result.source == "datadog"
    assert result.message == (
        "Datadog authentication validation failed for site 'datadoghq.com': "
        "HTTP 403: invalid <redacted> and <redacted>"
    )
    assert "api-secret" not in repr(result)
    assert "app-secret" not in repr(result)
    assert probe_client.closed is True


def test_authentication_failure_survives_secret_safe_cleanup_failure(
    monkeypatch,
) -> None:
    class FailingClient(FakeClient):
        def close(self) -> None:
            raise RuntimeError("cleanup exposed api-secret")

    class FailingKeyManagementApi:
        def __init__(self, client) -> None:
            self.client = client

        def validate_api_key(self):
            raise RuntimeError("authentication exposed app-secret")

    key_management_module = ModuleType("datadog_api_client.v2.api.key_management_api")
    key_management_module.KeyManagementApi = FailingKeyManagementApi
    monkeypatch.setitem(
        sys.modules,
        "datadog_api_client.v2.api.key_management_api",
        key_management_module,
    )
    monkeypatch.setattr(
        DatadogSessionFactory,
        "_create_api_client",
        staticmethod(lambda *, auth_settings: FailingClient()),
    )
    settings = (
        DatadogProvider()
        .resolve_execution_targets(
            target=_target(), regions=["global"], include=None, exclude=None
        )
        .execution_targets[0]
        .provider_data.settings
    )

    with pytest.raises(RuntimeError, match="authentication exposed <redacted>") as exc:
        DatadogSessionFactory().validate_auth(settings=settings)

    assert exc.value.__notes__ == [
        "Datadog authentication probe cleanup also failed: cleanup exposed <redacted>"
    ]


@pytest.mark.parametrize(
    ("include_override", "exclude_override"),
    [(["production-observability"], None), (None, ["production-observability"])],
)
def test_resolve_target_filters_rejects_cli_child_filters(
    include_override, exclude_override
) -> None:
    with pytest.raises(ValueError, match="do not support --include or --exclude"):
        DatadogProvider().resolve_target_filters(
            target=_target(),
            include_override=include_override,
            exclude_override=exclude_override,
        )


@pytest.mark.parametrize(
    ("regions", "include", "exclude", "preparation", "message"),
    [
        (["us-east-1"], None, None, None, "must resolve to"),
        (["global"], ["child"], None, None, "does not accept target filters"),
        (["global"], None, ["child"], None, "does not accept target filters"),
        (["global"], None, None, object(), "does not accept provider preparation"),
    ],
)
def test_resolution_rejects_non_organization_execution_shapes(
    regions, include, exclude, preparation, message
) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        DatadogProvider().resolve_execution_targets(
            target=_target(),
            regions=regions,
            include=include,
            exclude=exclude,
            preparation=preparation,
        )


def test_configured_target_names_produce_distinct_stable_ids() -> None:
    provider = DatadogProvider()

    ids = [
        provider.resolve_execution_targets(
            target=_target(name=name), regions=["global"], include=None, exclude=None
        )
        .execution_targets[0]
        .id
        for name in ("production-observability", "security-observability")
    ]

    assert ids == ["production-observability", "security-observability"]


def test_runtime_rejects_unknown_region_and_missing_provider_data() -> None:
    provider = DatadogProvider(session_factory=FakeSessionFactory())
    target = _target()
    execution_target = provider.resolve_execution_targets(
        target=target, regions=["global"], include=None, exclude=None
    ).execution_targets[0]
    runtime = provider.prepare_execution_runtime(
        target=target, execution_target=execution_target, context=_context()
    )

    with pytest.raises(ValueError, match="does not define execution region"):
        runtime.build_session(region="us-east-1")

    missing_data_target = ExecutionTarget(
        id=target.name,
        name=target.name,
        type="organization",
        provider="datadog",
        regions=["global"],
    )
    with pytest.raises(TypeError, match="missing DatadogExecutionTargetData"):
        provider.prepare_execution_runtime(
            target=target, execution_target=missing_data_target, context=_context()
        )


def test_runtime_closes_all_created_sessions_and_is_idempotent() -> None:
    session_factory = FakeSessionFactory()
    provider = DatadogProvider(session_factory=session_factory)
    target = _target()
    execution_target = provider.resolve_execution_targets(
        target=target, regions=["global"], include=None, exclude=None
    ).execution_targets[0]
    runtime = provider.prepare_execution_runtime(
        target=target, execution_target=execution_target, context=_context()
    )

    runtime.build_session(region="global")
    runtime.build_session(region="global")
    runtime.close()
    runtime.close()

    assert [client.closed for client in session_factory.clients] == [True, True]


def test_session_cleanup_redacts_original_credentials_after_rotation(
    monkeypatch,
) -> None:
    class FailingClient(FakeClient):
        def close(self) -> None:
            raise RuntimeError("cleanup exposed api-secret and app-secret")

    settings = (
        DatadogProvider()
        .resolve_execution_targets(
            target=_target(), regions=["global"], include=None, exclude=None
        )
        .execution_targets[0]
        .provider_data.settings
    )
    auth_settings = DatadogSessionFactory().resolve_auth_settings(settings=settings)
    monkeypatch.setattr(
        DatadogSessionFactory,
        "_create_api_client",
        staticmethod(lambda *, auth_settings: FailingClient()),
    )
    session = DatadogSessionFactory().create_session(
        target_id="production-observability", region_name="global", settings=settings
    )
    monkeypatch.setenv(DEFAULT_API_KEY_ENV, "rotated-api-secret")
    monkeypatch.setenv(DEFAULT_APP_KEY_ENV, "rotated-app-secret")

    with pytest.raises(DatadogProviderError, match="<redacted>") as exc:
        session.close()

    assert auth_settings.api_key not in str(exc.value)
    assert auth_settings.app_key not in str(exc.value)


def test_normal_paginated_task_receives_complete_context_and_result_structure() -> None:
    pages = [
        [SimpleNamespace(id="monitor-b"), SimpleNamespace(id="monitor-a")],
        [SimpleNamespace(id="monitor-c")],
    ]
    session_factory = PaginatedSessionFactory(pages=pages)
    provider = DatadogProvider(session_factory=session_factory)
    target = _target(metadata={"target_value": "target"})
    execution_target = provider.resolve_execution_targets(
        target=target, regions=["global"], include=None, exclude=None
    ).execution_targets[0]

    def run(
        *,
        provider,
        execution_target_id,
        execution_target_name,
        execution_target_type,
        region,
        session,
        dry_run,
        metadata,
        dependency_data,
        actions,
    ):
        monitor_ids = sorted(
            monitor.id for monitor in session.client.list_monitors_with_pagination()
        )
        return {
            "provider": provider,
            "execution_target_id": execution_target_id,
            "execution_target_name": execution_target_name,
            "execution_target_type": execution_target_type,
            "region": region,
            "site": session.site,
            "dry_run": dry_run,
            "metadata": metadata,
            "dependency_data": dependency_data,
            "action_count": len(actions.actions),
            "monitor_ids": monitor_ids,
        }

    task = ResolvedTask(
        name="list_monitors",
        run=run,
        depends_on=[],
        scope=TaskScope.REGION,
        metadata={"task_value": "task"},
    )
    context = ExecutionContext(
        regions=["global"], dry_run=False, tasks=[task], metadata=target.metadata
    )

    result = _execute_provider_execution_target(
        provider=provider,
        target=target,
        execution_target=execution_target,
        context=context,
    )

    assert result.status is ExecutionStatus.SUCCESS
    assert result.id == "production-observability"
    assert result.type == "organization"
    assert result.provider == "datadog"
    assert result.tasks[0].result == {
        "provider": "datadog",
        "execution_target_id": "production-observability",
        "execution_target_name": "production-observability",
        "execution_target_type": "organization",
        "region": "global",
        "site": "datadoghq.com",
        "dry_run": False,
        "metadata": {"target_value": "target", "task_value": "task"},
        "dependency_data": {},
        "action_count": 0,
        "monitor_ids": ["monitor-a", "monitor-b", "monitor-c"],
    }
    assert session_factory.paginated_client.page_count == 2
    assert session_factory.paginated_client.closed is True
    payload = result.to_dict()
    assert payload["id"] == "production-observability"
    assert payload["status"] == "success"
    assert payload["tasks"][0]["result"]["monitor_ids"] == [
        "monitor-a",
        "monitor-b",
        "monitor-c",
    ]
