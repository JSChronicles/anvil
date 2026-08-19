from __future__ import annotations

import builtins
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock
from types import ModuleType, SimpleNamespace

import pytest

from anvil.descriptors import TargetDescriptor
from anvil.execution_context import ExecutionContext
from anvil.provider_profiles import ProviderProfileConfig
from anvil.providers.cloudflare.auth import (
    CloudflareAuthSettings,
    resolve_auth_settings,
)
from anvil.providers.cloudflare.provider import (
    CloudflareExecutionTargetData,
    CloudflarePreflightData,
    CloudflareProvider,
)
from anvil.providers.cloudflare.session import (
    CloudflareAccount,
    CloudflareSession,
    CloudflareSessionFactory,
    CloudflareZone,
)
from anvil.results import EngineState, ExecutionStatus
from anvil.runner import _SingleFlightCache, run_multiple_targets
from anvil.task_context import TaskCallContext
from anvil.task_loader import ResolvedExecution, ResolvedTask

ACCOUNT_A = "a" * 32
ACCOUNT_B = "b" * 32
ZONE_A = "1" * 32
ZONE_B = "2" * 32


class FakeClient:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeSessionFactory:
    def __init__(self) -> None:
        self.accounts = [
            CloudflareAccount(account_id=ACCOUNT_B, display_name="Account B"),
            CloudflareAccount(account_id=ACCOUNT_A, display_name="Account A"),
        ]
        self.zones = [
            CloudflareZone(
                zone_id=ZONE_B,
                display_name="b.example",
                account_id=ACCOUNT_A,
                account_name="Account A",
                status="active",
            ),
            CloudflareZone(
                zone_id=ZONE_A,
                display_name="a.example",
                account_id=ACCOUNT_A,
                account_name="Account A",
                status="pending",
            ),
        ]
        self.validation_calls = []
        self.account_list_calls = []
        self.zone_list_calls = []
        self.session_calls = []
        self.closed_sessions = []

    def validate_client(self, *, settings) -> None:
        self.validation_calls.append(settings)

    def list_accounts(self, *, settings):
        self.account_list_calls.append(settings)
        return list(self.accounts)

    def list_zones(self, *, settings, account_id=None):
        self.zone_list_calls.append((settings, account_id))
        return list(self.zones)

    def create_session(
        self, *, settings, target_type, target_id, region_name, account_id, zone_id
    ):
        session = CloudflareSession(
            client=FakeClient(),
            target_type=target_type,
            target_id=target_id,
            region_name=region_name,
            auth_source=settings.source,
            account_id=account_id,
            zone_id=zone_id,
        )
        self.session_calls.append(session)
        return session

    def close_session(self, *, session) -> None:
        session.client.close()
        self.closed_sessions.append(session)


@pytest.fixture(autouse=True)
def cloudflare_credentials(monkeypatch):
    for environment_name in (
        "CLOUDFLARE_API_TOKEN",
        "CLOUDFLARE_API_KEY",
        "CLOUDFLARE_EMAIL",
        "CLOUDFLARE_BASE_URL",
        "CUSTOM_CF_TOKEN",
        "CUSTOM_CF_KEY",
        "CUSTOM_CF_EMAIL",
    ):
        monkeypatch.delenv(environment_name, raising=False)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "test-token")


def _target(**overrides) -> TargetDescriptor:
    values = {
        "name": "cloudflare-target",
        "provider": "cloudflare",
        "mode": "accounts",
        "include": [ACCOUNT_A],
    }
    values.update(overrides)
    return TargetDescriptor(**values)


def _context(*, benchmark_enabled: bool = False) -> ExecutionContext:
    return ExecutionContext(
        regions=["global"],
        dry_run=False,
        tasks=[],
        metadata={},
        benchmark_enabled=benchmark_enabled,
    )


def test_cloudflare_provider_metadata_and_global_coordinate():
    provider = CloudflareProvider(session_factory=FakeSessionFactory())

    assert provider.metadata.name == "cloudflare"
    assert provider.metadata.default_regions == ("global",)
    assert provider.metadata.supported_task_scopes == frozenset({"region", "target"})
    assert [region.name for region in provider.discover_regions(_target())] == [
        "global"
    ]


def test_cloudflare_rejects_unknown_modes_and_non_global_regions():
    provider = CloudflareProvider(session_factory=FakeSessionFactory())

    with pytest.raises(ValueError, match="Unsupported Cloudflare target mode"):
        provider.validate_target(_target(mode="organizations"))
    with pytest.raises(ValueError, match=r"only regions: \[global\]"):
        provider.validate_target(_target(regions=["us-east-1"]))


def test_cloudflare_rejects_invalid_ids_and_account_option_in_accounts_mode():
    provider = CloudflareProvider(session_factory=FakeSessionFactory())

    with pytest.raises(ValueError, match="32-character hexadecimal"):
        provider.validate_target(_target(include=["short-id"]))
    with pytest.raises(ValueError, match="32-character hexadecimal"):
        provider.validate_target(_target(include=["z" * 32]))
    with pytest.raises(ValueError, match="supported only in zones mode"):
        provider.validate_target(_target(provider_options={"account_id": ACCOUNT_A}))


def test_cloudflare_rejects_mixed_and_incomplete_explicit_credentials():
    provider = CloudflareProvider(session_factory=FakeSessionFactory())

    with pytest.raises(ValueError, match="cannot be combined"):
        provider.validate_target(
            _target(
                provider_options={
                    "api_token_env": "CUSTOM_CF_TOKEN",
                    "api_key_env": "CUSTOM_CF_KEY",
                    "api_email_env": "CUSTOM_CF_EMAIL",
                }
            )
        )
    with pytest.raises(ValueError, match="requires both"):
        provider.validate_target(
            _target(provider_options={"api_key_env": "CUSTOM_CF_KEY"})
        )


def test_cloudflare_named_profile_allows_target_account_option(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[providers.cloudflare.work]\napi_token_env = "CUSTOM_CF_TOKEN"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CUSTOM_CF_TOKEN", "profile-token")
    session_factory = FakeSessionFactory()
    provider = CloudflareProvider(
        session_factory=session_factory,
        profile_config=ProviderProfileConfig(path=config_path),
    )
    target = _target(
        mode="zones", provider_options={"profile": "work", "account_id": ACCOUNT_A}
    )

    result = provider.auth_check(target)

    assert result.status is ExecutionStatus.SUCCESS
    assert session_factory.validation_calls[0].source == "api_token:CUSTOM_CF_TOKEN"


def test_cloudflare_auth_resolution_prefers_token_and_supports_legacy(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_KEY", "legacy-key")
    monkeypatch.setenv("CLOUDFLARE_EMAIL", "operator@example.com")

    token_settings = resolve_auth_settings(provider_options={})

    assert token_settings.source == "api_token:CLOUDFLARE_API_TOKEN"
    assert token_settings.api_token == "test-token"
    assert "test-token" not in repr(token_settings)

    monkeypatch.delenv("CLOUDFLARE_API_TOKEN")
    legacy_settings = resolve_auth_settings(provider_options={})

    assert legacy_settings.source.startswith("global_api_key:")
    assert legacy_settings.api_key == "legacy-key"
    assert legacy_settings.api_email == "operator@example.com"
    assert "legacy-key" not in repr(legacy_settings)


def test_cloudflare_custom_credentials_endpoint_and_cache_identity_are_secret_safe(
    monkeypatch,
):
    monkeypatch.setenv("CUSTOM_CF_TOKEN", "custom-secret")
    settings = resolve_auth_settings(
        provider_options={
            "api_token_env": "CUSTOM_CF_TOKEN",
            "base_url": "https://cloudflare.example/client/v4",
        }
    )

    assert settings.source == "api_token:CUSTOM_CF_TOKEN"
    assert settings.base_url == "https://cloudflare.example/client/v4"
    assert "custom-secret" not in repr(settings.cache_identity())
    assert (
        settings.cache_identity()
        != CloudflareAuthSettings(
            source=settings.source,
            api_token="rotated-secret",
            base_url=settings.base_url,
        ).cache_identity()
    )


def test_cloudflare_incomplete_default_legacy_credentials_are_actionable(monkeypatch):
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN")
    monkeypatch.setenv("CLOUDFLARE_API_KEY", "legacy-secret")

    with pytest.raises(RuntimeError, match="Missing CLOUDFLARE_EMAIL") as error:
        resolve_auth_settings(provider_options={})

    assert "legacy-secret" not in str(error.value)


def test_cloudflare_auth_check_reports_missing_credentials(monkeypatch):
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN")
    provider = CloudflareProvider(session_factory=FakeSessionFactory())

    result = provider.auth_check(_target())

    assert result.status is ExecutionStatus.ERROR
    assert "Cloudflare credentials were not found" in str(result.message)
    assert "CLOUDFLARE_API_TOKEN" in str(result.remediation)
    assert "Zone Read" in str(result.remediation)


def test_cloudflare_auth_check_reports_missing_optional_dependency(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "cloudflare":
            raise ImportError("missing cloudflare")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    result = CloudflareProvider().auth_check(_target())

    assert result.status is ExecutionStatus.ERROR
    assert "optional dependency 'cloudflare'" in str(result.message)
    assert "anvil[cloudflare]" in str(result.remediation)


def test_cloudflare_auth_cache_key_is_stable_secret_safe_and_missing_safe(monkeypatch):
    provider = CloudflareProvider(session_factory=FakeSessionFactory())
    first = provider.auth_cache_key(_target())
    second = provider.auth_cache_key(_target())

    assert first == second
    assert "test-token" not in repr(first)

    monkeypatch.delenv("CLOUDFLARE_API_TOKEN")
    assert provider.auth_cache_key(_target()) is None


def test_cloudflare_filter_resolution_narrows_explicit_and_rejects_exclude():
    provider = CloudflareProvider(session_factory=FakeSessionFactory())
    target = _target(include=[ACCOUNT_A, ACCOUNT_B])

    assert provider.resolve_target_filters(
        target=target, include_override=[ACCOUNT_B], exclude_override=None
    ) == ([ACCOUNT_B], None)
    with pytest.raises(ValueError, match="does not allow --exclude"):
        provider.resolve_target_filters(
            target=target, include_override=None, exclude_override=[ACCOUNT_A]
        )


def test_cloudflare_discovery_filter_reports_unknown_exclusions():
    provider = CloudflareProvider(session_factory=FakeSessionFactory())
    target = _target(include=None)

    with pytest.raises(ValueError, match="unknown account IDs"):
        provider.prepare_target(
            target=target,
            context=_context(),
            include=None,
            exclude=["c" * 32],
            cache=_SingleFlightCache(),
            benchmark=None,
        )


def test_cloudflare_resolves_explicit_accounts_offline_in_configured_order():
    session_factory = FakeSessionFactory()
    provider = CloudflareProvider(session_factory=session_factory)
    target = _target(include=[ACCOUNT_B, ACCOUNT_A])

    plan = provider.resolve_execution_targets(
        target=target, regions=["global"], include=target.include, exclude=None
    )

    assert [item.id for item in plan.execution_targets] == [ACCOUNT_B, ACCOUNT_A]
    assert [item.type for item in plan.execution_targets] == ["account", "account"]
    assert all(
        isinstance(item.provider_data, CloudflareExecutionTargetData)
        for item in plan.execution_targets
    )
    assert session_factory.account_list_calls == []


def test_cloudflare_account_preflight_discovers_sorts_excludes_and_keys():
    session_factory = FakeSessionFactory()
    provider = CloudflareProvider(session_factory=session_factory)
    target = _target(include=None, exclude=[ACCOUNT_B])
    benchmark = {}

    preparation = provider.prepare_target(
        target=target,
        context=_context(),
        include=None,
        exclude=target.exclude,
        cache=_SingleFlightCache(),
        benchmark=benchmark,
    )

    assert isinstance(preparation.data, CloudflarePreflightData)
    assert [account.account_id for account in preparation.data.accounts] == [ACCOUNT_A]
    assert preparation.exclusive_execution_keys == (
        ("cloudflare", "account", ACCOUNT_A),
    )
    assert benchmark["cloudflare_selected_target_count"] == 1
    assert session_factory.account_list_calls


def test_cloudflare_zone_preflight_bounds_discovery_and_preserves_parent_metadata():
    session_factory = FakeSessionFactory()
    provider = CloudflareProvider(session_factory=session_factory)
    target = _target(
        mode="zones", include=None, provider_options={"account_id": ACCOUNT_A}
    )

    preparation = provider.prepare_target(
        target=target,
        context=_context(),
        include=None,
        exclude=None,
        cache=_SingleFlightCache(),
        benchmark=None,
    )
    plan = provider.resolve_execution_targets(
        target=target,
        regions=["global"],
        include=None,
        exclude=None,
        preparation=preparation.data,
    )

    assert [item.id for item in plan.execution_targets] == [ZONE_A, ZONE_B]
    assert session_factory.zone_list_calls[0][1] == ACCOUNT_A
    assert plan.execution_targets[0].metadata == {
        "zone_id": ZONE_A,
        "account_id": ACCOUNT_A,
        "account_name": "Account A",
        "zone_status": "pending",
    }
    assert preparation.exclusive_execution_keys == (
        ("cloudflare", "zone", ZONE_A),
        ("cloudflare", "zone", ZONE_B),
    )


def test_cloudflare_preflight_reuses_cache_and_records_hit():
    session_factory = FakeSessionFactory()
    provider = CloudflareProvider(session_factory=session_factory)
    target = _target(include=None)
    cache = _SingleFlightCache()
    first_benchmark = {}
    second_benchmark = {}

    first = provider.prepare_target(
        target=target,
        context=_context(),
        include=None,
        exclude=None,
        cache=cache,
        benchmark=first_benchmark,
    )
    second = provider.prepare_target(
        target=target,
        context=_context(),
        include=None,
        exclude=None,
        cache=cache,
        benchmark=second_benchmark,
    )

    assert first.data == second.data
    assert len(session_factory.account_list_calls) == 1
    assert first_benchmark["cloudflare_discovery_cache_hit"] is False
    assert second_benchmark["cloudflare_discovery_cache_hit"] is True


def test_cloudflare_preflight_cache_is_partitioned_by_zone_account():
    session_factory = FakeSessionFactory()
    provider = CloudflareProvider(session_factory=session_factory)
    cache = _SingleFlightCache()

    for account_id in (ACCOUNT_A, ACCOUNT_B):
        target = _target(
            mode="zones", include=None, provider_options={"account_id": account_id}
        )
        provider.prepare_target(
            target=target,
            context=_context(),
            include=None,
            exclude=None,
            cache=cache,
            benchmark=None,
        )

    assert [call[1] for call in session_factory.zone_list_calls] == [
        ACCOUNT_A,
        ACCOUNT_B,
    ]


def test_cloudflare_preflight_cache_single_flights_concurrent_discovery():
    started = Event()
    release = Event()

    class BlockingFactory(FakeSessionFactory):
        def list_accounts(self, *, settings):
            self.account_list_calls.append(settings)
            started.set()
            assert release.wait(timeout=5)
            return list(self.accounts)

    session_factory = BlockingFactory()
    provider = CloudflareProvider(session_factory=session_factory)
    target = _target(include=None)

    class TrackingCache(_SingleFlightCache):
        def __init__(self):
            super().__init__()
            self.calls = 0
            self.calls_lock = Lock()
            self.second_entered = Event()

        def get_or_create(self, *, key, create):
            with self.calls_lock:
                self.calls += 1
                if self.calls == 2:
                    self.second_entered.set()
            return super().get_or_create(key=key, create=create)

    cache = TrackingCache()
    benchmarks = [{}, {}]

    def prepare(index):
        return provider.prepare_target(
            target=target,
            context=_context(),
            include=None,
            exclude=None,
            cache=cache,
            benchmark=benchmarks[index],
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(prepare, 0)
        assert started.wait(timeout=5)
        second = executor.submit(prepare, 1)
        assert cache.second_entered.wait(timeout=5)
        release.set()
        assert first.result().data == second.result().data

    assert len(session_factory.account_list_calls) == 1
    assert (
        sum(bool(item["cloudflare_discovery_cache_waited"]) for item in benchmarks) == 1
    )


def test_cloudflare_preflight_does_not_cache_discovery_failures():
    class FlakyFactory(FakeSessionFactory):
        def list_accounts(self, *, settings):
            self.account_list_calls.append(settings)
            if len(self.account_list_calls) == 1:
                raise RuntimeError("temporary discovery failure")
            return list(self.accounts)

    session_factory = FlakyFactory()
    provider = CloudflareProvider(session_factory=session_factory)
    target = _target(include=None)
    cache = _SingleFlightCache()

    with pytest.raises(RuntimeError, match="temporary discovery failure"):
        provider.prepare_target(
            target=target,
            context=_context(),
            include=None,
            exclude=None,
            cache=cache,
            benchmark=None,
        )
    preparation = provider.prepare_target(
        target=target,
        context=_context(),
        include=None,
        exclude=None,
        cache=cache,
        benchmark=None,
    )

    assert isinstance(preparation.data, CloudflarePreflightData)
    assert len(session_factory.account_list_calls) == 2


def test_cloudflare_runtime_reuses_and_closes_one_session():
    session_factory = FakeSessionFactory()
    provider = CloudflareProvider(session_factory=session_factory)
    target = _target()
    execution_target = provider.resolve_execution_targets(
        target=target, regions=["global"], include=target.include, exclude=None
    ).execution_targets[0]

    runtime = provider.prepare_execution_runtime(
        target=target, execution_target=execution_target, context=_context()
    )
    first = runtime.build_session(region="global")
    second = runtime.build_session(region="global")
    runtime.close()

    assert first is second
    assert len(session_factory.session_calls) == 1
    assert session_factory.closed_sessions == [first]
    assert first.client.closed is True

    runtime.close()
    with pytest.raises(RuntimeError, match="already closed"):
        runtime.build_session(region="global")


def test_cloudflare_runtime_rejects_non_global_coordinate():
    provider = CloudflareProvider(session_factory=FakeSessionFactory())
    target = _target()
    execution_target = provider.resolve_execution_targets(
        target=target, regions=["global"], include=target.include, exclude=None
    ).execution_targets[0]
    runtime = provider.prepare_execution_runtime(
        target=target, execution_target=execution_target, context=_context()
    )

    with pytest.raises(ValueError, match="requires region 'global'"):
        runtime.build_session(region="earth")

    runtime.close()


def test_cloudflare_session_factory_constructs_token_client_and_consumes_iterator(
    monkeypatch,
):
    constructor_calls = []
    clients = []

    class FakeSdkClient:
        def __init__(self, **kwargs):
            constructor_calls.append(kwargs)
            self.closed = False
            self.accounts = SimpleNamespace(list=self._list_accounts)
            clients.append(self)

        def _list_accounts(self, **kwargs):
            assert kwargs == {"per_page": 50}
            yield SimpleNamespace(id=ACCOUNT_B, name="Account B")
            yield SimpleNamespace(id=ACCOUNT_A, name="Account A")

        def close(self):
            self.closed = True

    cloudflare_module = ModuleType("cloudflare")
    cloudflare_module.Cloudflare = FakeSdkClient
    monkeypatch.setitem(sys.modules, "cloudflare", cloudflare_module)
    settings = resolve_auth_settings(provider_options={"base_url": "https://api.test"})

    accounts = CloudflareSessionFactory().list_accounts(settings=settings)

    assert [account.account_id for account in accounts] == [ACCOUNT_A, ACCOUNT_B]
    assert constructor_calls == [
        {"api_token": "test-token", "base_url": "https://api.test"}
    ]
    assert clients[0].closed is True


def test_cloudflare_session_factory_constructs_legacy_client(monkeypatch):
    constructor_calls = []

    class FakeSdkClient:
        def __init__(self, **kwargs):
            constructor_calls.append(kwargs)

        def close(self):
            return None

    module = ModuleType("cloudflare")
    module.Cloudflare = FakeSdkClient
    monkeypatch.setitem(sys.modules, "cloudflare", module)
    settings = CloudflareAuthSettings(
        source="global_api_key:CLOUDFLARE_API_KEY+CLOUDFLARE_EMAIL",
        api_key="legacy-key",
        api_email="operator@example.com",
    )

    CloudflareSessionFactory().validate_client(settings=settings)

    assert constructor_calls == [
        {"api_key": "legacy-key", "api_email": "operator@example.com"}
    ]


def test_cloudflare_zone_discovery_consumes_all_pages_and_maps_metadata(monkeypatch):
    calls = []
    consumed_pages = []

    class FakePager:
        def __iter__(self):
            consumed_pages.append(1)
            yield {
                "id": ZONE_B,
                "name": "b.example",
                "account": {"id": ACCOUNT_A, "name": "Account A"},
                "status": "active",
            }
            consumed_pages.append(2)
            yield SimpleNamespace(
                id=ZONE_A,
                name="a.example",
                account=SimpleNamespace(id=ACCOUNT_A, name="Account A"),
                status="pending",
            )

    class FakeSdkClient:
        def __init__(self, **kwargs):
            self.zones = SimpleNamespace(list=self._list_zones)

        def _list_zones(self, **kwargs):
            calls.append(kwargs)
            return FakePager()

        def close(self):
            return None

    module = ModuleType("cloudflare")
    module.Cloudflare = FakeSdkClient
    monkeypatch.setitem(sys.modules, "cloudflare", module)

    zones = CloudflareSessionFactory().list_zones(
        settings=resolve_auth_settings(provider_options={}), account_id=ACCOUNT_A
    )

    assert consumed_pages == [1, 2]
    assert calls == [{"per_page": 50, "account": {"id": ACCOUNT_A}}]
    assert [zone.zone_id for zone in zones] == [ZONE_A, ZONE_B]
    assert zones[0].account_name == "Account A"


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (401, "authentication failed"),
        (403, "credentials authorized for Cloudflare account listing"),
        (429, "rate limited"),
    ],
)
def test_cloudflare_discovery_maps_http_failures_without_secrets(
    monkeypatch, status_code, expected
):
    class SdkError(Exception):
        def __init__(self):
            self.status_code = status_code
            super().__init__(f"failure containing test-token ({status_code})")

    class FakeSdkClient:
        def __init__(self, **kwargs):
            self.accounts = SimpleNamespace(list=self._list_accounts)

        def _list_accounts(self, **kwargs):
            raise SdkError()

        def close(self):
            return None

    module = ModuleType("cloudflare")
    module.Cloudflare = FakeSdkClient
    monkeypatch.setitem(sys.modules, "cloudflare", module)

    with pytest.raises(RuntimeError, match=expected) as error:
        CloudflareSessionFactory().list_accounts(
            settings=resolve_auth_settings(provider_options={})
        )

    assert "test-token" not in str(error.value)


def test_cloudflare_unknown_sdk_failure_is_redacted(monkeypatch):
    class FakeSdkClient:
        def __init__(self, **kwargs):
            self.accounts = SimpleNamespace(list=self._list_accounts)

        def _list_accounts(self, **kwargs):
            raise RuntimeError("transport failed for test-token")

        def close(self):
            return None

    module = ModuleType("cloudflare")
    module.Cloudflare = FakeSdkClient
    monkeypatch.setitem(sys.modules, "cloudflare", module)

    with pytest.raises(
        RuntimeError, match=r"transport failed for \[redacted\]"
    ) as error:
        CloudflareSessionFactory().list_accounts(
            settings=resolve_auth_settings(provider_options={})
        )

    assert error.value.__cause__ is None


def test_cloudflare_discovery_preserves_api_error_when_cleanup_also_fails(monkeypatch):
    class SdkError(Exception):
        status_code = 403

    class FakeSdkClient:
        def __init__(self, **kwargs):
            self.zones = SimpleNamespace(list=self._list_zones)

        def _list_zones(self, **kwargs):
            raise SdkError("permission denied")

        def close(self):
            raise OSError("cleanup included test-token")

    module = ModuleType("cloudflare")
    module.Cloudflare = FakeSdkClient
    monkeypatch.setitem(sys.modules, "cloudflare", module)

    with pytest.raises(RuntimeError, match="Grant Zone Read access") as error:
        CloudflareSessionFactory().list_zones(
            settings=resolve_auth_settings(provider_options={})
        )

    assert "test-token" not in str(error.value)


def test_cloudflare_auth_check_maps_client_cleanup_failure(monkeypatch):
    class FakeSdkClient:
        def __init__(self, **kwargs):
            return None

        def close(self):
            raise OSError("cleanup failed with test-token")

    module = ModuleType("cloudflare")
    module.Cloudflare = FakeSdkClient
    monkeypatch.setitem(sys.modules, "cloudflare", module)

    result = CloudflareProvider().auth_check(_target())

    assert result.status is ExecutionStatus.ERROR
    assert result.message == "Cloudflare SDK client cleanup failed (OSError)."
    assert result.remediation is None
    assert "test-token" not in str(result)


def test_cloudflare_zone_discovery_rejects_parent_outside_configured_account(
    monkeypatch,
):
    class FakeSdkClient:
        def __init__(self, **kwargs):
            self.zones = SimpleNamespace(
                list=lambda **parameters: [
                    {
                        "id": ZONE_A,
                        "name": "a.example",
                        "account": {"id": ACCOUNT_B, "name": "Account B"},
                    }
                ]
            )

        def close(self):
            return None

    module = ModuleType("cloudflare")
    module.Cloudflare = FakeSdkClient
    monkeypatch.setitem(sys.modules, "cloudflare", module)

    with pytest.raises(RuntimeError, match="outside configured account"):
        CloudflareSessionFactory().list_zones(
            settings=resolve_auth_settings(provider_options={}), account_id=ACCOUNT_A
        )


def test_cloudflare_runner_passes_account_task_call_context(monkeypatch):
    session_factory = FakeSessionFactory()
    provider = CloudflareProvider(session_factory=session_factory)
    seen = []

    def task(**kwargs):
        seen.append(kwargs)
        return {"target": kwargs["execution_target_id"]}

    monkeypatch.setattr("anvil.runner._load_provider", lambda provider_name: provider)
    monkeypatch.setattr(
        "anvil.runner.resolve_tasks",
        lambda **kwargs: ResolvedExecution(
            ordered=[ResolvedTask("capture", task, depends_on=[])], adjacency={}
        ),
    )

    result = run_multiple_targets(
        targets=[_target(include=[ACCOUNT_B, ACCOUNT_A], tasks=[{"name": "capture"}])],
        max_parallel_targets=1,
        cli_dry_run=None,
        cli_include=None,
        cli_exclude=None,
    )

    assert result.state is EngineState.COMPLETED_SUCCESS
    assert [entity.id for entity in result.target_results[0].entities] == [
        ACCOUNT_A,
        ACCOUNT_B,
    ]
    assert {item["provider"] for item in seen} == {"cloudflare"}
    assert {item["execution_target_type"] for item in seen} == {"account"}
    assert {item["region"] for item in seen} == {"global"}
    assert all(set(item) == TaskCallContext.keyword_names() for item in seen)
    assert all(isinstance(item["session"], CloudflareSession) for item in seen)
    assert all(session.client.closed for session in session_factory.closed_sessions)
    first_entity = result.target_results[0].entities[0]
    assert first_entity.type == "account"
    assert first_entity.provider == "cloudflare"
    assert first_entity.metadata == {"account_id": ACCOUNT_A}
    assert first_entity.status is ExecutionStatus.SUCCESS
    assert first_entity.tasks[0].result == {"target": ACCOUNT_A}


def test_cloudflare_zone_task_can_use_runtime_client_with_target_id(monkeypatch):
    class ManageableZoneClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.zones = SimpleNamespace(get=self._get_zone)

        @staticmethod
        def _get_zone(*, zone_id):
            return {"id": zone_id, "name": "a.example", "manageable": True}

    class ZoneSessionFactory(FakeSessionFactory):
        def create_session(
            self, *, settings, target_type, target_id, region_name, account_id, zone_id
        ):
            session = CloudflareSession(
                client=ManageableZoneClient(),
                target_type=target_type,
                target_id=target_id,
                region_name=region_name,
                auth_source=settings.source,
                account_id=account_id,
                zone_id=zone_id,
            )
            self.session_calls.append(session)
            return session

    session_factory = ZoneSessionFactory()
    provider = CloudflareProvider(session_factory=session_factory)
    seen = []

    def task(*, session, execution_target_id, execution_target_type, **kwargs):
        zone = session.client.zones.get(zone_id=session.target_id)
        seen.append((execution_target_id, execution_target_type, session.zone_id))
        return zone

    monkeypatch.setattr("anvil.runner._load_provider", lambda provider_name: provider)
    monkeypatch.setattr(
        "anvil.runner.resolve_tasks",
        lambda **kwargs: ResolvedExecution(
            ordered=[ResolvedTask("inspect-zone", task, depends_on=[])], adjacency={}
        ),
    )

    result = run_multiple_targets(
        targets=[
            _target(
                mode="zones",
                include=[ZONE_A],
                provider_options={"account_id": ACCOUNT_A},
                tasks=[{"name": "inspect-zone"}],
            )
        ],
        max_parallel_targets=1,
        cli_dry_run=None,
        cli_include=None,
        cli_exclude=None,
    )

    entity = result.target_results[0].entities[0]
    assert result.state is EngineState.COMPLETED_SUCCESS
    assert seen == [(ZONE_A, "zone", ZONE_A)]
    assert entity.id == ZONE_A
    assert entity.type == "zone"
    assert entity.metadata == {"zone_id": ZONE_A, "account_id": ACCOUNT_A}
    assert entity.tasks[0].result == {
        "id": ZONE_A,
        "name": "a.example",
        "manageable": True,
    }
    assert session_factory.closed_sessions[0].client.closed is True
