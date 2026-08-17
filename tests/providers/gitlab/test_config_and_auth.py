from __future__ import annotations

from types import SimpleNamespace

import pytest

from anvil.descriptors import TargetDescriptor
from anvil.providers.gitlab.auth import resolve_auth_settings
from anvil.providers.gitlab.config import normalize_gitlab_url
from anvil.providers.gitlab.provider import GitLabProvider
from anvil.providers.gitlab.session import GitLabSessionFactory
from anvil.results import ExecutionStatus


def _target(
    *,
    mode: str = "projects",
    regions: list[str] | None = None,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    options: dict[str, object] | None = None,
) -> TargetDescriptor:
    return TargetDescriptor(
        name="gitlab-test",
        provider="gitlab",
        mode=mode,
        regions=["global"] if regions is None else regions,
        include=include,
        exclude=exclude,
        provider_options=(
            {"token_env": "ANVIL_TEST_GITLAB_TOKEN"} if options is None else options
        ),
        tasks=[],
    )


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        (None, "https://gitlab.com"),
        ("HTTPS://GitLab.Example.COM:443/", "https://gitlab.example.com"),
        ("http://GitLab.Example.COM:80/root/", "http://gitlab.example.com/root"),
        ("https://[2001:db8::1]:8443/gitlab/", "https://[2001:db8::1]:8443/gitlab"),
    ],
)
def test_gitlab_url_normalization_supports_saas_and_self_managed(
    configured: str | None, expected: str
) -> None:
    assert normalize_gitlab_url(configured) == expected


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("gitlab.example.com", "http or https"),
        ("ssh://gitlab.example.com", "http or https"),
        ("https://user:secret@gitlab.example.com", "must not contain credentials"),
        ("https://gitlab.example.com?token=secret", "query or fragment"),
        ("https://gitlab.example.com/#fragment", "query or fragment"),
        ("https://gitlab.example.com:invalid", "is invalid"),
    ],
)
def test_gitlab_url_validation_rejects_ambiguous_instance_urls(
    url: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        normalize_gitlab_url(url)


@pytest.mark.parametrize(
    ("target", "message"),
    [
        (_target(mode="instance"), "Unsupported GitLab target mode"),
        (_target(regions=["global", "other"]), "only region 'global'"),
        (_target(include=["0"]), "Invalid GitLab resource ID"),
        (_target(options={"token_env": "TOKEN", "unknown": "value"}), "Unsupported"),
        (_target(options={"token_env": ""}), "non-empty string"),
        (_target(options={"token_env": "TOKEN", "auth_type": "job"}), "auth_type"),
    ],
)
def test_gitlab_target_validation_rejects_unsupported_configuration(
    target: TargetDescriptor, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        GitLabProvider().validate_target(target)


def test_gitlab_target_filters_narrow_explicit_includes() -> None:
    provider = GitLabProvider()
    target = _target(include=["root/a", "root/b"])

    include, exclude = provider.resolve_target_filters(
        target=target,
        include_override=["root/b", "root/missing"],
        exclude_override=None,
    )

    assert include == ["root/b"]
    assert exclude is None
    with pytest.raises(ValueError, match="does not allow --exclude"):
        provider.resolve_target_filters(
            target=target, include_override=None, exclude_override=["root/a"]
        )


def test_gitlab_auth_cache_identity_is_secret_safe_and_credential_sensitive(
    monkeypatch,
) -> None:
    provider = GitLabProvider()
    target = _target()
    monkeypatch.setenv("ANVIL_TEST_GITLAB_TOKEN", "first-secret-token")

    first_key = provider.auth_cache_key(target)
    monkeypatch.setenv("ANVIL_TEST_GITLAB_TOKEN", "second-secret-token")
    second_key = provider.auth_cache_key(target)

    assert first_key != second_key
    assert "first-secret-token" not in repr(first_key)
    assert "second-secret-token" not in repr(second_key)


def test_gitlab_auth_settings_trim_tokens_and_redact_error_text(monkeypatch) -> None:
    monkeypatch.setenv("ANVIL_TEST_GITLAB_TOKEN", "  secret-token  ")
    settings = resolve_auth_settings(target=_target(), require_token=True)

    assert settings.token() == "secret-token"
    assert settings.redact("request failed for secret-token") == (
        "request failed for <redacted>"
    )
    assert "secret-token" not in repr(settings.cache_identity())


def test_gitlab_auth_failure_is_actionable_and_secret_safe(monkeypatch) -> None:
    class FailingAuthFactory:
        def validate_auth(self, *, settings) -> None:
            raise RuntimeError(f"401 Unauthorized for token {settings.token()}")

    monkeypatch.setenv("ANVIL_TEST_GITLAB_TOKEN", "super-secret-token")

    result = GitLabProvider(session_factory=FailingAuthFactory()).auth_check(_target())

    assert result.status is ExecutionStatus.ERROR
    assert result.source == "gitlab"
    assert result.message == "401 Unauthorized for token <redacted>"
    assert "super-secret-token" not in repr(result)
    assert "read_api" in (result.remediation or "")


def test_gitlab_session_auth_failure_closes_client_and_redacts_token(
    monkeypatch,
) -> None:
    closed: list[bool] = []

    class Client:
        session = SimpleNamespace(close=lambda: closed.append(True))

        def auth(self) -> None:
            raise RuntimeError("server rejected super-secret-token")

    monkeypatch.setenv("ANVIL_TEST_GITLAB_TOKEN", "super-secret-token")
    monkeypatch.setattr(
        GitLabSessionFactory,
        "_load_python_gitlab",
        staticmethod(lambda: SimpleNamespace(Gitlab=lambda *args, **kwargs: Client())),
    )
    settings = resolve_auth_settings(target=_target(), require_token=True)

    with pytest.raises(RuntimeError, match="server rejected <redacted>") as error:
        GitLabSessionFactory().validate_auth(settings=settings)

    assert "super-secret-token" not in str(error.value)
    assert closed == [True]
