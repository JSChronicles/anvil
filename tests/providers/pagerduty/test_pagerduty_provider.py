from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from types import ModuleType

import pytest

from anvil.descriptors import TargetDescriptor
from anvil.execution_context import ExecutionContext
from anvil.provider_profiles import ProviderProfileConfig
from anvil.providers.pagerduty.auth import PagerDutyAuthSettings, resolve_auth_settings
from anvil.providers.pagerduty.errors import PagerDutyDependencyError
from anvil.providers.pagerduty.provider import (
    PagerDutyExecutionTargetData,
    PagerDutyPreflightData,
    PagerDutyProvider,
)
from anvil.providers.pagerduty.session import (
    PAGERDUTY_EXTRA_REMEDIATION,
    PAGERDUTY_MAX_HTTP_ATTEMPTS,
    PAGERDUTY_RATE_LIMIT_RETRIES,
    PagerDutySession,
    PagerDutySessionFactory,
)
from anvil.results import ExecutionStatus
from anvil.runner import _execute_provider_execution_target
from anvil.task_loader import ResolvedTask


@dataclass
class FakeClient:
    """Minimal closeable PagerDuty REST client double."""

    closed: bool = False

    def close(self) -> None:
        """Record client cleanup."""

        self.closed = True


class FakeSessionFactory:
    """Injected PagerDuty session factory for provider lifecycle tests."""

    def __init__(self) -> None:
        self.validated: list[PagerDutyAuthSettings] = []
        self.created: list[PagerDutySession] = []

    def validate_settings(self, *, settings: PagerDutyAuthSettings) -> None:
        """Record settings validation."""

        self.validated.append(settings)

    def create_session(
        self, *, account_id: str, region_name: str, settings: PagerDutyAuthSettings
    ) -> PagerDutySession:
        """Return a closeable task-facing session."""

        session = PagerDutySession(
            account_id=account_id,
            region_name=region_name,
            client=FakeClient(),
            api_url=settings.api_url,
            auth_source=settings.source,
            auth_type=settings.auth_type,
        )
        self.created.append(session)
        return session


class UnusedPreparationCache:
    """Fail if account-only preparation attempts child discovery caching."""

    def get_or_create(self, *, key: object, create) -> tuple[object, bool, bool]:
        """Reject unexpected cache use."""

        raise AssertionError("PagerDuty account preparation must not discover children")


def _target(**overrides: object) -> TargetDescriptor:
    values: dict[str, object] = {
        "name": "pagerduty-production",
        "provider": "pagerduty",
        "mode": "account",
    }
    values.update(overrides)
    return TargetDescriptor(**values)


def _context() -> ExecutionContext:
    return ExecutionContext(regions=["global"], dry_run=False, tasks=[], metadata={})


def test_provider_metadata_and_global_location() -> None:
    provider = PagerDutyProvider()

    assert provider.metadata.default_regions == ("global",)
    assert provider.metadata.supported_task_scopes == frozenset({"region", "target"})
    assert [region.name for region in provider.discover_regions(_target())] == [
        "global"
    ]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"provider": "aws"}, "provider 'pagerduty' targets only"),
        ({"mode": "teams"}, "Unsupported PagerDuty target mode"),
        ({"regions": ["us"]}, "regions must contain only 'global'"),
        ({"include": ["service-a"]}, "does not allow include or exclude"),
        ({"provider_options": {"unknown": "value"}}, "Unsupported provider.options"),
        ({"provider_options": {"auth_type": "basic"}}, "auth_type must be one of"),
        (
            {"provider_options": {"api_url": "http://api.pagerduty.com"}},
            "must be an HTTPS origin",
        ),
        (
            {"provider_options": {"subdomain": "invalid.example.com"}},
            "valid account subdomain",
        ),
    ],
)
def test_provider_rejects_invalid_account_configuration(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        PagerDutyProvider().validate_target(_target(**overrides))


def test_auth_check_resolves_explicit_environment(monkeypatch) -> None:
    monkeypatch.setenv("CUSTOM_PD_TOKEN", "secret-token")
    session_factory = FakeSessionFactory()
    provider = PagerDutyProvider(session_factory=session_factory)
    target = _target(
        provider_options={
            "token_env": "CUSTOM_PD_TOKEN",
            "auth_type": "bearer",
            "api_url": "https://api.eu.pagerduty.com",
            "from_email": "admin@example.com",
            "subdomain": "acme",
        }
    )

    result = provider.auth_check(target)

    assert result.status is ExecutionStatus.SUCCESS
    assert result.source == "environment:CUSTOM_PD_TOKEN"
    assert result.message is not None
    assert "no API request was made" in result.message
    assert len(session_factory.validated) == 1
    settings = session_factory.validated[0]
    assert settings.auth_type == "bearer"
    assert settings.api_url == "https://api.eu.pagerduty.com"
    assert settings.from_email == "admin@example.com"
    assert settings.subdomain == "acme"
    assert "secret-token" not in repr(settings.cache_identity())


def test_auth_check_resolves_named_provider_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[providers.pagerduty.eu]\n"
        'token_env = "EU_PD_TOKEN"\n'
        'auth_type = "bearer"\n'
        'api_url = "https://api.eu.pagerduty.com"\n'
        'subdomain = "acme"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("EU_PD_TOKEN", "profile-secret")
    session_factory = FakeSessionFactory()
    provider = PagerDutyProvider(
        session_factory=session_factory,
        profile_config=ProviderProfileConfig(path=config_path),
    )

    result = provider.auth_check(_target(provider_options={"profile": "eu"}))

    assert result.status is ExecutionStatus.SUCCESS
    settings = session_factory.validated[0]
    assert settings.auth_type == "bearer"
    assert settings.api_url == "https://api.eu.pagerduty.com"
    assert settings.subdomain == "acme"


def test_auth_check_reports_missing_credentials(monkeypatch) -> None:
    for environment_name in ("PAGERDUTY_API_TOKEN", "PAGERDUTY_USER_API_KEY"):
        monkeypatch.delenv(environment_name, raising=False)

    result = PagerDutyProvider(session_factory=FakeSessionFactory()).auth_check(
        _target()
    )

    assert result.status is ExecutionStatus.ERROR
    assert result.message is not None
    assert "PAGERDUTY_API_TOKEN" in result.message
    assert result.source == "environment:PAGERDUTY_API_TOKEN"
    assert result.remediation == (
        "Set PAGERDUTY_API_TOKEN to a non-empty PagerDuty API token."
    )


def test_missing_explicit_token_does_not_fall_back_or_disclose_other_tokens(
    monkeypatch,
) -> None:
    monkeypatch.delenv("CUSTOM_PD_TOKEN", raising=False)
    monkeypatch.setenv("PAGERDUTY_API_TOKEN", "unrelated-secret")

    result = PagerDutyProvider(session_factory=FakeSessionFactory()).auth_check(
        _target(provider_options={"token_env": "CUSTOM_PD_TOKEN"})
    )

    assert result.status is ExecutionStatus.ERROR
    assert result.message is not None
    assert "CUSTOM_PD_TOKEN" in result.message
    assert "PAGERDUTY_API_TOKEN" not in result.message
    assert "unrelated-secret" not in result.message


def test_explicit_token_environment_is_stripped_and_takes_precedence(
    monkeypatch,
) -> None:
    monkeypatch.setenv("PAGERDUTY_API_TOKEN", "default-token")
    monkeypatch.setenv("CUSTOM_PD_TOKEN", "  explicit-token  ")

    settings = resolve_auth_settings(provider_options={"token_env": "CUSTOM_PD_TOKEN"})

    assert settings.token_env == "CUSTOM_PD_TOKEN"
    assert settings.require_token() == "explicit-token"
    assert "explicit-token" not in repr(settings)
    assert "explicit-token" not in repr(settings.cache_identity())


def test_default_credentials_fall_back_to_user_api_key(monkeypatch) -> None:
    monkeypatch.delenv("PAGERDUTY_API_TOKEN", raising=False)
    monkeypatch.setenv("PAGERDUTY_USER_API_KEY", "user-token")

    settings = resolve_auth_settings(provider_options={})

    assert settings.token_env == "PAGERDUTY_USER_API_KEY"
    assert settings.source == "environment:PAGERDUTY_USER_API_KEY"


def test_auth_cache_key_tracks_secret_changes_without_exposing_them(
    monkeypatch,
) -> None:
    provider = PagerDutyProvider(session_factory=FakeSessionFactory())
    target = _target()
    monkeypatch.setenv("PAGERDUTY_API_TOKEN", "first-secret")
    first_key = provider.auth_cache_key(target)
    monkeypatch.setenv("PAGERDUTY_API_TOKEN", "second-secret")
    second_key = provider.auth_cache_key(target)

    assert first_key != second_key
    assert "first-secret" not in repr(first_key)
    assert "second-secret" not in repr(second_key)


def test_settings_canonicalize_equivalent_account_identity_options(monkeypatch) -> None:
    monkeypatch.setenv("PAGERDUTY_API_TOKEN", "secret-token")

    settings = resolve_auth_settings(
        provider_options={
            "api_url": "https://api.eu.pagerduty.com/",
            "subdomain": "Acme-OnCall",
        }
    )

    assert settings.api_url == "https://api.eu.pagerduty.com"
    assert settings.subdomain == "acme-oncall"


def test_prepare_and_resolve_account_target_deterministically(monkeypatch) -> None:
    monkeypatch.setenv("PAGERDUTY_API_TOKEN", "secret-token")
    session_factory = FakeSessionFactory()
    provider = PagerDutyProvider(session_factory=session_factory)
    target = _target(provider_options={"subdomain": "acme"})

    preparation = provider.prepare_target(
        target=target,
        context=_context(),
        include=None,
        exclude=None,
        cache=UnusedPreparationCache(),
        benchmark=None,
    )
    plan = provider.resolve_execution_targets(
        target=target,
        regions=["global"],
        include=None,
        exclude=None,
        preparation=preparation.data,
    )

    assert isinstance(preparation.data, PagerDutyPreflightData)
    assert preparation.exclusive_execution_keys == (
        ("pagerduty", "account", "https://api.pagerduty.com", "acme"),
    )
    assert [execution_target.id for execution_target in plan.execution_targets] == [
        "acme"
    ]
    execution_target = plan.execution_targets[0]
    assert execution_target.type == "account"
    assert execution_target.regions == ["global"]
    assert execution_target.metadata["pagerduty_api_url"] == (
        "https://api.pagerduty.com"
    )
    assert isinstance(execution_target.provider_data, PagerDutyExecutionTargetData)


def test_account_identity_and_exclusive_key_are_stable_without_subdomain(
    monkeypatch,
) -> None:
    monkeypatch.setenv("PAGERDUTY_API_TOKEN", "secret-token")
    provider = PagerDutyProvider(session_factory=FakeSessionFactory())
    target = _target()

    preparations = [
        provider.prepare_target(
            target=target,
            context=_context(),
            include=None,
            exclude=None,
            cache=UnusedPreparationCache(),
            benchmark=None,
        )
        for _ in range(2)
    ]
    plans = [
        provider.resolve_execution_targets(
            target=target,
            regions=["global"],
            include=None,
            exclude=None,
            preparation=preparation.data,
        )
        for preparation in preparations
    ]

    assert preparations[0].exclusive_execution_keys == (
        preparations[1].exclusive_execution_keys
    )
    assert preparations[0].exclusive_execution_keys == (
        (
            "pagerduty",
            "account",
            "https://api.pagerduty.com",
            ("credential", preparations[0].data.settings.token_fingerprint),
        ),
    )
    assert "secret-token" not in repr(preparations[0].exclusive_execution_keys)
    assert [plan.execution_targets[0].id for plan in plans] == [
        "pagerduty-production",
        "pagerduty-production",
    ]


def test_target_filter_overrides_are_rejected() -> None:
    provider = PagerDutyProvider()

    with pytest.raises(ValueError, match="does not allow target filters"):
        provider.resolve_target_filters(
            target=_target(), include_override=["service-a"], exclude_override=None
        )


def test_resolution_rejects_invalid_preparation_and_runtime_target(monkeypatch) -> None:
    monkeypatch.setenv("PAGERDUTY_API_TOKEN", "secret-token")
    provider = PagerDutyProvider(session_factory=FakeSessionFactory())
    target = _target()

    with pytest.raises(TypeError, match="preparation must be PagerDutyPreflightData"):
        provider.resolve_execution_targets(
            target=target,
            regions=["global"],
            include=None,
            exclude=None,
            preparation=object(),
        )

    execution_target = provider.resolve_execution_targets(
        target=target, regions=["global"], include=None, exclude=None
    ).execution_targets[0]
    execution_target = replace(execution_target, provider_data=None)
    with pytest.raises(TypeError, match="missing PagerDutyExecutionTargetData"):
        provider.prepare_execution_runtime(
            target=target, execution_target=execution_target, context=_context()
        )


def test_runtime_builds_task_session_and_closes_client(monkeypatch) -> None:
    monkeypatch.setenv("PAGERDUTY_API_TOKEN", "secret-token")
    session_factory = FakeSessionFactory()
    provider = PagerDutyProvider(session_factory=session_factory)
    target = _target()
    plan = provider.resolve_execution_targets(
        target=target, regions=["global"], include=None, exclude=None
    )
    runtime = provider.prepare_execution_runtime(
        target=target, execution_target=plan.execution_targets[0], context=_context()
    )

    session = runtime.build_session(region="global")
    runtime.record_region_outcome(
        region="global", duration_seconds=0.1, failed=False, interrupted=False
    )
    runtime.close()

    assert session.account_id == "pagerduty-production"
    assert session.region_name == "global"
    assert isinstance(session.client, FakeClient)
    assert session.client.closed is True


def test_runtime_rejects_non_global_session_and_closes_every_created_client(
    monkeypatch,
) -> None:
    monkeypatch.setenv("PAGERDUTY_API_TOKEN", "secret-token")
    session_factory = FakeSessionFactory()
    provider = PagerDutyProvider(session_factory=session_factory)
    target = _target()
    execution_target = provider.resolve_execution_targets(
        target=target, regions=["global"], include=None, exclude=None
    ).execution_targets[0]
    runtime = provider.prepare_execution_runtime(
        target=target, execution_target=execution_target, context=_context()
    )

    with pytest.raises(ValueError, match="does not support execution region"):
        runtime.build_session(region="us-east-1")
    sessions = [runtime.build_session(region="global") for _ in range(2)]
    runtime.close()

    assert all(session.client.closed is True for session in sessions)
    assert len(session_factory.created) == 2


def test_runtime_cleanup_continues_after_one_client_fails(caplog) -> None:
    class FailingCloseClient(FakeClient):
        def close(self) -> None:
            raise OSError("sensitive transport detail")

    settings = PagerDutyAuthSettings(
        source="environment:PAGERDUTY_API_TOKEN",
        token_env="PAGERDUTY_API_TOKEN",
        token_fingerprint="fingerprint",
        auth_type="token",
        api_url="https://api.pagerduty.com",
        from_email=None,
        subdomain="acme",
    )
    session_factory = FakeSessionFactory()
    provider = PagerDutyProvider(session_factory=session_factory)
    target = _target()
    execution_target = provider.resolve_execution_targets(
        target=target,
        regions=["global"],
        include=None,
        exclude=None,
        preparation=PagerDutyPreflightData(settings=settings),
    ).execution_targets[0]
    runtime = provider.prepare_execution_runtime(
        target=target, execution_target=execution_target, context=_context()
    )
    failing_session = runtime.build_session(region="global")
    succeeding_session = runtime.build_session(region="global")
    failing_session.client = FailingCloseClient()

    runtime.close()

    assert succeeding_session.client.closed is True
    assert "OSError" in caplog.text
    assert "sensitive transport detail" not in caplog.text


def test_runner_passes_pagerduty_session_through_task_call_context(monkeypatch) -> None:
    """Exercise the provider through the ordinary provider-neutral task path."""

    monkeypatch.setenv("PAGERDUTY_API_TOKEN", "secret-token")
    session_factory = FakeSessionFactory()
    provider = PagerDutyProvider(session_factory=session_factory)
    target = _target(metadata={"environment": "production"}, tasks=[])
    execution_target = provider.resolve_execution_targets(
        target=target, regions=["global"], include=None, exclude=None
    ).execution_targets[0]
    invocations: list[dict[str, object]] = []

    def run(**kwargs: object) -> dict[str, bool]:
        invocations.append(kwargs)
        return {"ok": True}

    context = ExecutionContext(
        regions=["global"],
        dry_run=False,
        tasks=[ResolvedTask(name="inspect", run=run, depends_on=[])],
        metadata=target.metadata,
    )

    result = _execute_provider_execution_target(
        provider=provider,
        target=target,
        execution_target=execution_target,
        context=context,
    )

    assert result.status is ExecutionStatus.SUCCESS
    assert len(invocations) == 1
    invocation = invocations[0]
    assert invocation["provider"] == "pagerduty"
    assert invocation["execution_target_id"] == "pagerduty-production"
    assert invocation["execution_target_type"] == "account"
    assert invocation["region"] == "global"
    assert invocation["metadata"] == {"environment": "production"}
    assert isinstance(invocation["session"], PagerDutySession)
    assert isinstance(invocation["session"].client, FakeClient)
    assert invocation["session"].client.closed is True
    assert result.to_dict()["tasks"] == [result.tasks[0].to_dict()]
    assert result.tasks[0].task_id == "inspect"
    assert result.tasks[0].region == "global"
    assert result.tasks[0].result == {"ok": True}


def test_task_can_consume_all_sdk_pagination_through_runtime_session(
    monkeypatch,
) -> None:
    """Keep PagerDuty pagination available without provider resource modeling."""

    monkeypatch.setenv("PAGERDUTY_API_TOKEN", "secret-token")
    pagination_calls: list[tuple[str, dict[str, object]]] = []

    class PaginationClient(FakeClient):
        def iter_all(self, endpoint: str, **params: object):
            pagination_calls.append((endpoint, params))
            yield {"id": "PSERVICE2", "name": "Service B"}
            yield {"id": "PSERVICE1", "name": "Service A"}

    class PaginationSessionFactory(FakeSessionFactory):
        def create_session(
            self, *, account_id: str, region_name: str, settings: PagerDutyAuthSettings
        ) -> PagerDutySession:
            session = PagerDutySession(
                account_id=account_id,
                region_name=region_name,
                client=PaginationClient(),
                api_url=settings.api_url,
                auth_source=settings.source,
                auth_type=settings.auth_type,
            )
            self.created.append(session)
            return session

    session_factory = PaginationSessionFactory()
    provider = PagerDutyProvider(session_factory=session_factory)
    target = _target()
    execution_target = provider.resolve_execution_targets(
        target=target, regions=["global"], include=None, exclude=None
    ).execution_targets[0]

    def run(*, session: PagerDutySession, **kwargs: object) -> dict[str, object]:
        services = list(session.client.iter_all("services", team_ids=["PTEAM1"]))
        return {"services": services}

    result = _execute_provider_execution_target(
        provider=provider,
        target=target,
        execution_target=execution_target,
        context=ExecutionContext(
            regions=["global"],
            dry_run=False,
            tasks=[ResolvedTask(name="list-services", run=run, depends_on=[])],
            metadata={},
        ),
    )

    assert result.status is ExecutionStatus.SUCCESS
    assert pagination_calls == [("services", {"team_ids": ["PTEAM1"]})]
    assert result.tasks[0].result == {
        "services": [
            {"id": "PSERVICE2", "name": "Service B"},
            {"id": "PSERVICE1", "name": "Service A"},
        ]
    }
    assert session_factory.created[0].client.closed is True


def test_sdk_authentication_error_becomes_task_failure_and_closes_session(
    monkeypatch,
) -> None:
    monkeypatch.setenv("PAGERDUTY_API_TOKEN", "secret-token")

    class UnauthorizedClient(FakeClient):
        def get(self, endpoint: str) -> object:
            raise RuntimeError(f"401 Unauthorized from {endpoint}")

    class UnauthorizedSessionFactory(FakeSessionFactory):
        def create_session(
            self, *, account_id: str, region_name: str, settings: PagerDutyAuthSettings
        ) -> PagerDutySession:
            session = PagerDutySession(
                account_id=account_id,
                region_name=region_name,
                client=UnauthorizedClient(),
                api_url=settings.api_url,
                auth_source=settings.source,
                auth_type=settings.auth_type,
            )
            self.created.append(session)
            return session

    session_factory = UnauthorizedSessionFactory()
    provider = PagerDutyProvider(session_factory=session_factory)
    target = _target()
    execution_target = provider.resolve_execution_targets(
        target=target, regions=["global"], include=None, exclude=None
    ).execution_targets[0]

    def run(*, session: PagerDutySession, **kwargs: object) -> object:
        return session.client.get("users/me")

    result = _execute_provider_execution_target(
        provider=provider,
        target=target,
        execution_target=execution_target,
        context=ExecutionContext(
            regions=["global"],
            dry_run=False,
            tasks=[ResolvedTask(name="whoami", run=run, depends_on=[])],
            metadata={},
        ),
    )

    assert result.status is ExecutionStatus.ERROR
    assert result.tasks[0].status is ExecutionStatus.ERROR
    assert result.tasks[0].error == "401 Unauthorized from users/me"
    assert session_factory.created[0].client.closed is True


def test_session_factory_builds_bounded_retry_client(monkeypatch) -> None:
    monkeypatch.setenv("PAGERDUTY_API_TOKEN", "secret-token")
    calls: list[dict[str, object]] = []

    class Client:
        retry = {500: 1}
        max_http_attempts = 10

        def __init__(self, token: str, **kwargs: object) -> None:
            calls.append({"token": token, **kwargs})
            self.retry = dict(type(self).retry)
            self.closed = False

        def close(self) -> None:
            self.closed = True

    module = ModuleType("pagerduty")
    module.RestApiV2Client = Client
    monkeypatch.setattr(
        PagerDutySessionFactory, "_load_pagerduty", staticmethod(lambda: module)
    )
    settings = resolve_auth_settings(
        provider_options={
            "auth_type": "bearer",
            "api_url": "https://api.eu.pagerduty.com",
            "from_email": "admin@example.com",
        }
    )

    session = PagerDutySessionFactory().create_session(
        account_id="acme", region_name="global", settings=settings
    )

    assert calls == [
        {
            "token": "secret-token",
            "auth_type": "bearer",
            "base_url": "https://api.eu.pagerduty.com",
            "default_from": "admin@example.com",
        }
    ]
    assert session.client.retry == {500: 1, 429: PAGERDUTY_RATE_LIMIT_RETRIES}
    assert session.client.max_http_attempts == PAGERDUTY_MAX_HTTP_ATTEMPTS


def test_session_factory_reports_missing_optional_dependency(monkeypatch) -> None:
    monkeypatch.setenv("PAGERDUTY_API_TOKEN", "secret-token")

    def missing_sdk() -> ModuleType:
        raise PagerDutyDependencyError(
            "PagerDuty provider requires optional dependency 'pagerduty'. "
            f"{PAGERDUTY_EXTRA_REMEDIATION}"
        )

    monkeypatch.setattr(
        PagerDutySessionFactory, "_load_pagerduty", staticmethod(missing_sdk)
    )

    result = PagerDutyProvider().auth_check(_target())

    assert result.status is ExecutionStatus.ERROR
    assert result.remediation == PAGERDUTY_EXTRA_REMEDIATION


def test_auth_check_sanitizes_sdk_constructor_failures(monkeypatch) -> None:
    monkeypatch.setenv("PAGERDUTY_API_TOKEN", "super-secret-token")

    class FailingClient:
        def __init__(self, token: str, **kwargs: object) -> None:
            raise ValueError(f"invalid credential {token}")

    module = ModuleType("pagerduty")
    module.RestApiV2Client = FailingClient
    monkeypatch.setattr(
        PagerDutySessionFactory, "_load_pagerduty", staticmethod(lambda: module)
    )

    result = PagerDutyProvider().auth_check(_target())

    assert result.status is ExecutionStatus.ERROR
    assert result.message is not None
    assert "SDK error type: ValueError" in result.message
    assert "super-secret-token" not in result.message
    assert result.remediation == (
        "Verify the PagerDuty token, auth_type, api_url, and from_email settings."
    )


def test_session_factory_closes_client_when_retry_configuration_fails(
    monkeypatch,
) -> None:
    monkeypatch.setenv("PAGERDUTY_API_TOKEN", "secret-token")
    clients: list[object] = []

    class MisconfiguredClient:
        retry = object()

        def __init__(self, token: str, **kwargs: object) -> None:
            self.closed = False
            clients.append(self)

        def close(self) -> None:
            self.closed = True

    module = ModuleType("pagerduty")
    module.RestApiV2Client = MisconfiguredClient
    monkeypatch.setattr(
        PagerDutySessionFactory, "_load_pagerduty", staticmethod(lambda: module)
    )
    settings = resolve_auth_settings(provider_options={})

    with pytest.raises(RuntimeError, match="SDK error type: TypeError") as exc_info:
        PagerDutySessionFactory().create_session(
            account_id="acme", region_name="global", settings=settings
        )

    assert "secret-token" not in str(exc_info.value)
    assert clients[0].closed is True
