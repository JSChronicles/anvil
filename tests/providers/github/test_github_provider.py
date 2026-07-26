from __future__ import annotations

import builtins
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import pytest

from anvil.descriptors import ConfigBranch, TargetDescriptor
from anvil.execution_context import ExecutionContext
from anvil.providers.base import ExecutionTarget
from anvil.providers.github import create_provider_instance
from anvil.providers.github.provider import (
    CachedGitHubClient,
    DEFAULT_GITHUB_API_VERSION,
    GITHUB_CONFIG_ENV,
    GitHubRateGate,
    GitHubProfileConfig,
    GitHubSessionFactory,
    GithubRepository,
    GithubExecutionTargetData,
    GithubProvider,
)
from anvil.results import ExecutionStatus


@pytest.fixture(autouse=True)
def _isolated_github_config(monkeypatch):
    monkeypatch.setenv(GITHUB_CONFIG_ENV, str(Path.cwd() / ".missing-github-config"))


@dataclass(frozen=True)
class FakeSession:
    target_id: str
    target_type: str
    region_name: str


class FakeSessionFactory:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.repositories: dict[str, list[str]] = {}

    def create_session(
        self,
        *,
        target_id: str,
        target_type: str,
        region_name: str,
        provider_options: dict[str, object],
    ) -> FakeSession:
        self.calls.append(
            {
                "target_id": target_id,
                "target_type": target_type,
                "region_name": region_name,
                "provider_options": dict(provider_options),
            }
        )
        return FakeSession(
            target_id=target_id, target_type=target_type, region_name=region_name
        )

    def list_owner_repositories(
        self, *, owner_logins: list[str], provider_options: dict[str, object]
    ) -> list[GithubRepository]:
        self.calls.append(
            {
                "owner_logins": list(owner_logins),
                "provider_options": dict(provider_options),
            }
        )
        return sorted(
            [
                GithubRepository(full_name=full_name, owner=owner_login)
                for owner_login in owner_logins
                for full_name in self.repositories.get(owner_login, [])
            ],
            key=lambda repository: repository.full_name.lower(),
        )


class FakeTokenAuth:
    def __init__(self, token):
        self.token = token


class FakeInstallationAuth:
    def __init__(self, *, installation_id):
        self.installation_id = installation_id


class FakeAppAuth:
    instances: list["FakeAppAuth"] = []

    def __init__(self, app_id, private_key):
        self.app_id = app_id
        self.private_key = private_key
        self.installation_ids: list[int] = []
        FakeAppAuth.instances.append(self)

    def get_installation_auth(self, installation_id):
        self.installation_ids.append(installation_id)
        return FakeInstallationAuth(installation_id=installation_id)


class FakeAuth:
    Token = FakeTokenAuth
    AppAuth = FakeAppAuth

    class NetrcAuth:
        pass


class FakeGithubRetry:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.status_forcelist = frozenset((*range(500, 600), 403))


class FakeRequester:
    def __init__(self, client):
        self.client = client

    def requestJsonAndCheck(self, method, path, *args, **kwargs):  # noqa: N802
        self.client.rest_calls.append((method, path))
        response = self.client.rest_responses.get(path)
        if isinstance(response, Exception):
            raise response
        if response is None:
            error = RuntimeError("404")
            error.status = 404
            raise error
        return {}, response


class FakeGithubClient:
    instances: list["FakeGithubClient"] = []
    rest_responses: dict[str, object] = {}

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.repo_calls: list[str] = []
        self.organization_calls: list[str] = []
        self.user_calls: list[str] = []
        self.rest_calls: list[tuple[str, str]] = []
        self.search_calls: list[tuple[str, bool]] = []
        self.search_results: object = []
        self.rest_responses = dict(FakeGithubClient.rest_responses)
        self.requester = FakeRequester(self)
        FakeGithubClient.instances.append(self)

    def get_repo(self, full_name_or_id):
        self.repo_calls.append(full_name_or_id)
        return {"repo": full_name_or_id}

    def get_organization(self, login):
        self.organization_calls.append(login)
        if login == "personal-user":
            error = RuntimeError("404")
            error.status = 404
            raise error
        return FakeRepositoryOwner(login=login)

    def get_user(self, login):
        self.user_calls.append(login)
        return FakeRepositoryOwner(login=login)

    def search_code(self, *, query, highlight=False):
        self.search_calls.append((query, highlight))
        return self.search_results


class FakeLazySearchResults:
    def __init__(self, items, *, error=None):
        self.items = items
        self.error = error

    def __iter__(self):
        if self.error is not None:
            raise self.error
        yield from self.items


class FakeRepository:
    def __init__(self, *, full_name, name):
        self.full_name = full_name
        self.name = name


class FakeRepositoryOwner:
    def __init__(self, *, login):
        self.login = login

    def get_repos(self):
        return [
            FakeRepository(full_name=f"{self.login}/alpha", name="alpha"),
            FakeRepository(full_name=f"{self.login}/beta", name="beta"),
        ]


class FakeOrganizationClient:
    def __init__(self, raw_client):
        self.raw_client = raw_client

    def get_organization(self, login):
        self.raw_client.organization_calls.append(login)
        return {"organization": login}


class CountingProfileConfig(GitHubProfileConfig):
    def __init__(self, *, path: Path) -> None:
        super().__init__(path=path)
        self.load_calls = 0

    def load(self) -> dict[str, dict[str, str]]:
        self.load_calls += 1
        return super().load()


def _target(**overrides) -> TargetDescriptor:
    values = {
        "config_branch": ConfigBranch.TARGETS,
        "name": "github-repositories",
        "provider": "github",
        "mode": "repositories",
        "include": ["octo-org/example"],
        "provider_options": {"token_env": "GITHUB_TOKEN"},
    }
    values.update(overrides)
    return TargetDescriptor(**values)


def _context() -> ExecutionContext:
    return ExecutionContext(regions=["global"], dry_run=False, tasks=[], metadata={})


def _install_fake_pygithub(monkeypatch) -> ModuleType:
    FakeGithubClient.instances = []
    FakeGithubClient.rest_responses = {}
    FakeAppAuth.instances = []
    github_module = ModuleType("github")
    github_module.Auth = FakeAuth
    github_module.Github = FakeGithubClient
    github_module.GithubRetry = FakeGithubRetry
    monkeypatch.setitem(sys.modules, "github", github_module)
    return github_module


def test_github_provider_metadata_and_default_location():
    provider = create_provider_instance()
    target = _target()

    assert provider.metadata.name == "github"
    assert provider.metadata.display_name == "GitHub"
    assert target.regions is None
    assert provider.metadata.default_regions == ("global",)
    assert provider.metadata.supported_task_scopes == frozenset({"region", "target"})
    assert provider.discover_regions(target)[0].name == "global"


def test_github_provider_requires_explicit_include():
    provider = create_provider_instance()
    target = _target(include=None)

    with pytest.raises(ValueError, match="requires include"):
        provider.validate_target(target)


def test_github_provider_rejects_exclude():
    with pytest.raises(ValueError, match="does not allow exclude|include and exclude"):
        _target(exclude=["octo-org/skip"])


def test_github_provider_rejects_removed_auth_type():
    provider = create_provider_instance()
    target = _target(provider_options={"auth_type": "token"})

    with pytest.raises(ValueError, match="auth_type"):
        provider.validate_target(target)


def test_github_provider_rejects_removed_installation_id():
    provider = create_provider_instance()
    target = _target(provider_options={"installation_id": "67890"})

    with pytest.raises(ValueError, match="installation_id"):
        provider.validate_target(target)


def test_github_provider_rejects_profile_with_inline_auth():
    provider = create_provider_instance()
    target = _target(provider_options={"profile": "work", "token_env": "GITHUB_TOKEN"})

    with pytest.raises(ValueError, match="profile cannot be combined"):
        provider.validate_target(target)


def test_github_provider_rejects_repository_include_without_owner():
    provider = create_provider_instance()
    target = _target(include=["example"])

    with pytest.raises(ValueError, match="owner/repo"):
        provider.validate_target(target)


def test_github_provider_rejects_organization_include_with_repo_path():
    provider = create_provider_instance()
    target = _target(mode="organizations", include=["octo-org/example"])

    with pytest.raises(ValueError, match="owner logins"):
        provider.validate_target(target)


def test_github_provider_discovers_repository_targets_from_owner_logins():
    session_factory = FakeSessionFactory()
    session_factory.repositories = {
        "octo-org": ["octo-org/example", "octo-org/other"],
        "another-org": ["another-org/repo"],
    }
    provider = GithubProvider(session_factory=session_factory)
    target = _target(
        name="github-organizations",
        mode="organizations",
        include=["octo-org", "another-org"],
    )

    plan = provider.resolve_execution_targets(
        target=target, regions=["global"], include=target.include, exclude=None
    )

    assert [(item.id, item.type) for item in plan.execution_targets] == [
        ("another-org/repo", "repository"),
        ("octo-org/example", "repository"),
        ("octo-org/other", "repository"),
    ]
    assert isinstance(
        plan.execution_targets[0].provider_data, GithubExecutionTargetData
    )


def test_github_provider_uses_owner_targets_for_organization_code_search():
    session_factory = FakeSessionFactory()
    provider = GithubProvider(session_factory=session_factory)
    target = _target(
        name="github-organization-search",
        mode="organizations",
        include=["octo-org", "another-org"],
        tasks=[{"name": "search_code"}],
    )

    plan = provider.resolve_execution_targets(
        target=target, regions=["global"], include=target.include, exclude=None
    )

    assert [(item.id, item.type) for item in plan.execution_targets] == [
        ("octo-org", "organization"),
        ("another-org", "organization"),
    ]
    assert session_factory.calls == []


def test_github_provider_resolves_repository_targets_offline():
    provider = create_provider_instance()
    target = _target(include=["octo-org/example", "octo-org/other"])

    plan = provider.resolve_execution_targets(
        target=target, regions=["global"], include=target.include, exclude=None
    )

    assert [(item.id, item.type) for item in plan.execution_targets] == [
        ("octo-org/example", "repository"),
        ("octo-org/other", "repository"),
    ]


def test_github_auth_check_resolves_inline_token_env(monkeypatch):
    provider = create_provider_instance()
    monkeypatch.setenv("GITHUB_TOKEN", "secret-token")

    result = provider.auth_check(_target())

    assert result.status is ExecutionStatus.SUCCESS
    assert result.source == "inline"


def test_github_auth_check_reports_missing_token_env(monkeypatch):
    provider = create_provider_instance()
    monkeypatch.delenv("MISSING_GITHUB_TOKEN", raising=False)

    result = provider.auth_check(
        _target(provider_options={"token_env": "MISSING_GITHUB_TOKEN"})
    )

    assert result.status is ExecutionStatus.ERROR
    assert result.source == "github"
    assert "MISSING_GITHUB_TOKEN" in str(result.message)


def test_github_auth_check_reports_missing_profile(tmp_path, monkeypatch):
    provider = create_provider_instance()
    config_path = tmp_path / "github-config.toml"
    config_path.write_text('[default]\ntoken_env = "GITHUB_TOKEN"\n', encoding="utf-8")
    monkeypatch.setenv(GITHUB_CONFIG_ENV, str(config_path))

    result = provider.auth_check(_target(provider_options={"profile": "work"}))

    assert result.status is ExecutionStatus.ERROR
    assert result.source == "github"
    assert "profile 'work'" in str(result.message)


def test_github_runtime_uses_injected_session_factory():
    session_factory = FakeSessionFactory()
    provider = GithubProvider(session_factory=session_factory)
    target = _target(
        provider_options={
            "token_env": "WORK_GITHUB_TOKEN",
            "api_url": "https://github.example/api/v3",
        }
    )
    execution_target = provider.resolve_execution_targets(
        target=target, regions=["global"], include=target.include, exclude=None
    ).execution_targets[0]

    runtime = provider.prepare_execution_runtime(
        target=target, execution_target=execution_target, context=_context()
    )

    assert runtime.build_session(region="global") == FakeSession(
        target_id="octo-org/example", target_type="repository", region_name="global"
    )
    assert session_factory.calls == [
        {
            "target_id": "octo-org/example",
            "target_type": "repository",
            "region_name": "global",
            "provider_options": {
                "token_env": "WORK_GITHUB_TOKEN",
                "api_url": "https://github.example/api/v3",
            },
        }
    ]


def test_github_prepare_runtime_rejects_wrong_provider():
    provider = GithubProvider()
    target = _target()
    execution_target = ExecutionTarget(
        id="octo-org/example",
        name="octo-org/example",
        type="repository",
        provider="aws",
        regions=["global"],
        provider_data=GithubExecutionTargetData(
            target_id="octo-org/example",
            target_type="repository",
            provider_options={},
            session_factory=GitHubSessionFactory(),
        ),
    )

    with pytest.raises(ValueError, match="not github"):
        provider.prepare_execution_runtime(
            target=target, execution_target=execution_target, context=_context()
        )


def test_github_prepare_runtime_rejects_wrong_provider_data_type():
    provider = GithubProvider()
    target = _target()
    execution_target = ExecutionTarget(
        id="octo-org/example",
        name="octo-org/example",
        type="repository",
        provider="github",
        regions=["global"],
        provider_data=object(),
    )

    with pytest.raises(TypeError, match="GithubExecutionTargetData"):
        provider.prepare_execution_runtime(
            target=target, execution_target=execution_target, context=_context()
        )


def test_github_resolved_targets_reuse_provider_session_factory():
    session_factory = FakeSessionFactory()
    provider = GithubProvider(session_factory=session_factory)
    target = _target(include=["octo-org/example", "octo-org/other"])

    plan = provider.resolve_execution_targets(
        target=target, regions=["global"], include=target.include, exclude=None
    )

    provider_data = [
        execution_target.provider_data for execution_target in plan.execution_targets
    ]
    assert all(isinstance(data, GithubExecutionTargetData) for data in provider_data)
    assert [
        data.session_factory
        for data in provider_data
        if isinstance(data, GithubExecutionTargetData)
    ] == [session_factory, session_factory]


def test_github_session_factory_imports_pygithub_only_when_session_is_built(
    monkeypatch,
):
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "github":
            raise ImportError("missing PyGithub")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.setenv("GITHUB_TOKEN", "secret-token")

    with pytest.raises(RuntimeError, match=r"PyGithub.*anvil\[github\]"):
        GitHubSessionFactory().create_session(
            target_id="octo-org/example",
            target_type="repository",
            region_name="global",
            provider_options={},
        )


def test_github_session_factory_uses_token_env_api_url_and_default_version(monkeypatch):
    _install_fake_pygithub(monkeypatch)
    monkeypatch.setenv("WORK_GITHUB_TOKEN", "secret-token")

    session = GitHubSessionFactory().create_session(
        target_id="octo-org/example",
        target_type="repository",
        region_name="global",
        provider_options={
            "token_env": "WORK_GITHUB_TOKEN",
            "api_url": "https://github.example/api/v3",
        },
    )

    assert session.target_id == "octo-org/example"
    assert session.region_name == "global"
    assert session.auth_source == "inline"
    assert session.api_url == "https://github.example/api/v3"
    assert session.api_version == DEFAULT_GITHUB_API_VERSION
    assert FakeGithubClient.instances[0].kwargs == {
        "auth": FakeGithubClient.instances[0].kwargs["auth"],
        "base_url": "https://github.example/api/v3",
        "api_version": DEFAULT_GITHUB_API_VERSION,
        "per_page": 100,
        "retry": FakeGithubClient.instances[0].kwargs["retry"],
    }
    assert FakeGithubClient.instances[0].kwargs["retry"].kwargs == {"total": 1}
    assert 403 not in FakeGithubClient.instances[0].kwargs["retry"].status_forcelist
    assert 500 in FakeGithubClient.instances[0].kwargs["retry"].status_forcelist
    assert FakeGithubClient.instances[0].kwargs["auth"].token == "secret-token"


def test_github_session_factory_uses_custom_api_version(monkeypatch):
    _install_fake_pygithub(monkeypatch)
    monkeypatch.setenv("GITHUB_TOKEN", "secret-token")

    session = GitHubSessionFactory().create_session(
        target_id="octo-org/example",
        target_type="repository",
        region_name="global",
        provider_options={"api_version": "2023-01-01"},
    )

    assert session.api_version == "2023-01-01"
    assert FakeGithubClient.instances[0].kwargs["api_version"] == "2023-01-01"


def test_github_session_factory_requires_token_env(monkeypatch):
    _install_fake_pygithub(monkeypatch)
    monkeypatch.delenv("MISSING_GITHUB_TOKEN", raising=False)

    with pytest.raises(RuntimeError, match="MISSING_GITHUB_TOKEN"):
        GitHubSessionFactory().create_session(
            target_id="octo-org/example",
            target_type="repository",
            region_name="global",
            provider_options={"token_env": "MISSING_GITHUB_TOKEN"},
        )


def test_github_session_factory_uses_named_profile(monkeypatch, tmp_path):
    _install_fake_pygithub(monkeypatch)
    monkeypatch.setenv("WORK_GITHUB_TOKEN", "profile-token")
    config_path = tmp_path / "github-config.toml"
    config_path.write_text(
        '[work]\ntoken_env = "WORK_GITHUB_TOKEN"\n'
        'api_url = "https://github.example/api/v3"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv(GITHUB_CONFIG_ENV, str(config_path))

    session = GitHubSessionFactory().create_session(
        target_id="octo-org/example",
        target_type="repository",
        region_name="global",
        provider_options={"profile": "work"},
    )

    assert session.auth_source == "profile:work"
    assert session.api_url == "https://github.example/api/v3"
    assert FakeGithubClient.instances[0].kwargs["auth"].token == "profile-token"


def test_github_session_factory_inline_auth_beats_default_profile(
    monkeypatch, tmp_path
):
    _install_fake_pygithub(monkeypatch)
    monkeypatch.setenv("INLINE_GITHUB_TOKEN", "inline-token")
    monkeypatch.setenv("DEFAULT_GITHUB_TOKEN", "default-token")
    config_path = tmp_path / "github-config.toml"
    config_path.write_text(
        '[default]\ntoken_env = "DEFAULT_GITHUB_TOKEN"\n', encoding="utf-8"
    )
    monkeypatch.setenv(GITHUB_CONFIG_ENV, str(config_path))

    GitHubSessionFactory().create_session(
        target_id="octo-org/example",
        target_type="repository",
        region_name="global",
        provider_options={"token_env": "INLINE_GITHUB_TOKEN"},
    )

    assert FakeGithubClient.instances[0].kwargs["auth"].token == "inline-token"


def test_github_session_factory_default_profile_beats_github_token(
    monkeypatch, tmp_path
):
    _install_fake_pygithub(monkeypatch)
    monkeypatch.setenv("GITHUB_TOKEN", "fallback-token")
    monkeypatch.setenv("DEFAULT_GITHUB_TOKEN", "default-token")
    config_path = tmp_path / "github-config.toml"
    config_path.write_text(
        '[default]\ntoken_env = "DEFAULT_GITHUB_TOKEN"\n', encoding="utf-8"
    )
    monkeypatch.setenv(GITHUB_CONFIG_ENV, str(config_path))

    session = GitHubSessionFactory().create_session(
        target_id="octo-org/example",
        target_type="repository",
        region_name="global",
        provider_options={},
    )

    assert session.auth_source == "profile:default"
    assert FakeGithubClient.instances[0].kwargs["auth"].token == "default-token"


def test_github_session_factory_uses_gh_token_fallback(monkeypatch):
    _install_fake_pygithub(monkeypatch)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(
        GitHubSessionFactory,
        "_has_netrc_credentials",
        staticmethod(lambda *, api_url: False),
    )

    class Result:
        returncode = 0
        stdout = "cli-token\n"

    monkeypatch.setattr(
        "anvil.providers.github.provider.subprocess.run",
        lambda *args, **kwargs: Result(),
    )

    session = GitHubSessionFactory().create_session(
        target_id="octo-org/example",
        target_type="repository",
        region_name="global",
        provider_options={},
    )

    assert session.auth_source == "default:gh"
    assert FakeGithubClient.instances[0].kwargs["auth"].token == "cli-token"


def test_github_session_factory_uses_netrc_before_gh(monkeypatch):
    _install_fake_pygithub(monkeypatch)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(
        GitHubSessionFactory,
        "_has_netrc_credentials",
        staticmethod(lambda *, api_url: True),
    )

    def fail_gh(*args, **kwargs):
        raise AssertionError("gh auth token should not run when netrc is available")

    monkeypatch.setattr("anvil.providers.github.provider.subprocess.run", fail_gh)

    session = GitHubSessionFactory().create_session(
        target_id="octo-org/example",
        target_type="repository",
        region_name="global",
        provider_options={},
    )

    assert session.auth_source == "default:netrc"
    assert isinstance(FakeGithubClient.instances[0].kwargs["auth"], FakeAuth.NetrcAuth)


def test_github_session_factory_rejects_missing_profile(monkeypatch, tmp_path):
    _install_fake_pygithub(monkeypatch)
    config_path = tmp_path / "github-config.toml"
    config_path.write_text('[default]\ntoken_env = "GITHUB_TOKEN"\n', encoding="utf-8")
    monkeypatch.setenv(GITHUB_CONFIG_ENV, str(config_path))

    with pytest.raises(RuntimeError, match="profile 'work'.*not found"):
        GitHubSessionFactory().create_session(
            target_id="octo-org/example",
            target_type="repository",
            region_name="global",
            provider_options={"profile": "work"},
        )


def test_github_session_factory_rejects_ambiguous_profile(monkeypatch, tmp_path):
    _install_fake_pygithub(monkeypatch)
    monkeypatch.setenv("GITHUB_PRIVATE_KEY", "private-key")
    config_path = tmp_path / "github-config.toml"
    config_path.write_text(
        '[bad]\ntoken_env = "GITHUB_TOKEN"\napp_id = "12345"\n'
        'private_key_env = "GITHUB_PRIVATE_KEY"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv(GITHUB_CONFIG_ENV, str(config_path))

    with pytest.raises(RuntimeError, match="mix token and app"):
        GitHubSessionFactory().create_session(
            target_id="octo-org/example",
            target_type="repository",
            region_name="global",
            provider_options={"profile": "bad"},
        )


def test_github_profile_config_loads_injected_path(monkeypatch, tmp_path):
    monkeypatch.delenv(GITHUB_CONFIG_ENV, raising=False)
    config_path = tmp_path / "config"
    config_path.write_text(
        '[enterprise]\napi_url = "https://github.example/api/v3"\n'
        'token_env = "GHE_TOKEN"\n',
        encoding="utf-8",
    )

    profiles = GitHubProfileConfig(path=Path(config_path)).load()

    assert profiles == {
        "enterprise": {
            "api_url": "https://github.example/api/v3",
            "token_env": "GHE_TOKEN",
        }
    }


def test_github_profile_config_rejects_invalid_toml(monkeypatch, tmp_path):
    monkeypatch.delenv(GITHUB_CONFIG_ENV, raising=False)
    config_path = tmp_path / "config"
    config_path.write_text("[default\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="invalid"):
        GitHubProfileConfig(path=config_path).load()


def test_github_session_factory_caches_profile_config_resolution(monkeypatch, tmp_path):
    monkeypatch.delenv(GITHUB_CONFIG_ENV, raising=False)
    monkeypatch.setenv("WORK_GITHUB_TOKEN", "profile-token")
    config_path = tmp_path / "config"
    config_path.write_text(
        '[work]\ntoken_env = "WORK_GITHUB_TOKEN"\n', encoding="utf-8"
    )
    profile_config = CountingProfileConfig(path=config_path)
    session_factory = GitHubSessionFactory(profile_config=profile_config)

    first_settings = session_factory.resolve_auth_settings(
        provider_options={"profile": "work"}
    )
    second_settings = session_factory.resolve_auth_settings(
        provider_options={"profile": "work"}
    )

    assert first_settings.source == "profile:work"
    assert second_settings.source == "profile:work"
    assert profile_config.load_calls == 1


def test_github_session_factory_uses_app_auth_private_key_env(monkeypatch):
    _install_fake_pygithub(monkeypatch)
    monkeypatch.setenv("GITHUB_PRIVATE_KEY", "private-key")
    FakeGithubClient.rest_responses = {"/orgs/octo-org/installation": {"id": 67890}}

    session = GitHubSessionFactory().create_session(
        target_id="octo-org/example",
        target_type="repository",
        region_name="global",
        provider_options={"app_id": "12345", "private_key_env": "GITHUB_PRIVATE_KEY"},
    )

    auth = FakeGithubClient.instances[1].kwargs["auth"]
    assert session.auth_source == "inline"
    assert FakeAppAuth.instances[0].app_id == 12345
    assert FakeAppAuth.instances[0].private_key == "private-key"
    assert auth.installation_id == 67890
    assert FakeGithubClient.instances[0].rest_calls == [
        ("GET", "/orgs/octo-org/installation")
    ]


def test_github_session_factory_uses_app_auth_private_key_path(monkeypatch, tmp_path):
    _install_fake_pygithub(monkeypatch)
    private_key_path = tmp_path / "github-app.pem"
    private_key_path.write_text("file-private-key", encoding="utf-8")
    FakeGithubClient.rest_responses = {"/orgs/octo-org/installation": {"id": "67890"}}

    GitHubSessionFactory().create_session(
        target_id="octo-org",
        target_type="organization",
        region_name="global",
        provider_options={"app_id": "12345", "private_key_path": str(private_key_path)},
    )

    auth = FakeGithubClient.instances[1].kwargs["auth"]
    assert FakeAppAuth.instances[0].private_key == "file-private-key"
    assert auth.installation_id == 67890


def test_github_session_factory_caches_installation_lookup(monkeypatch):
    _install_fake_pygithub(monkeypatch)
    monkeypatch.setenv("GITHUB_PRIVATE_KEY", "private-key")
    FakeGithubClient.rest_responses = {"/orgs/octo-org/installation": {"id": 67890}}
    session_factory = GitHubSessionFactory()

    for _index in range(2):
        session_factory.create_session(
            target_id="octo-org/example",
            target_type="repository",
            region_name="global",
            provider_options={
                "app_id": "12345",
                "private_key_env": "GITHUB_PRIVATE_KEY",
            },
        )

    lookup_clients = [
        client
        for client in FakeGithubClient.instances
        if isinstance(client.kwargs["auth"], FakeAppAuth)
    ]
    assert len(lookup_clients) == 1
    assert lookup_clients[0].rest_calls == [("GET", "/orgs/octo-org/installation")]


def test_github_session_factory_caches_installation_and_client_by_owner(monkeypatch):
    _install_fake_pygithub(monkeypatch)
    monkeypatch.setenv("GITHUB_PRIVATE_KEY", "private-key")
    FakeGithubClient.rest_responses = {"/orgs/octo-org/installation": {"id": 67890}}
    session_factory = GitHubSessionFactory()

    first_session = session_factory.create_session(
        target_id="octo-org/example",
        target_type="repository",
        region_name="global",
        provider_options={"app_id": "12345", "private_key_env": "GITHUB_PRIVATE_KEY"},
    )
    second_session = session_factory.create_session(
        target_id="octo-org/other",
        target_type="repository",
        region_name="global",
        provider_options={"app_id": "12345", "private_key_env": "GITHUB_PRIVATE_KEY"},
    )

    lookup_clients = [
        client
        for client in FakeGithubClient.instances
        if isinstance(client.kwargs["auth"], FakeAppAuth)
    ]
    installation_clients = [
        client
        for client in FakeGithubClient.instances
        if isinstance(client.kwargs["auth"], FakeInstallationAuth)
    ]
    assert len(lookup_clients) == 1
    assert len(installation_clients) == 1
    assert first_session.client is second_session.client


def test_github_session_factory_uses_installation_specific_rate_keys(monkeypatch):
    _install_fake_pygithub(monkeypatch)
    monkeypatch.setenv("GITHUB_PRIVATE_KEY", "private-key")
    FakeGithubClient.rest_responses = {
        "/orgs/first-org/installation": {"id": 111},
        "/orgs/second-org/installation": {"id": 222},
    }
    session_factory = GitHubSessionFactory()

    first_session = session_factory.create_session(
        target_id="first-org/example",
        target_type="repository",
        region_name="global",
        provider_options={"app_id": "12345", "private_key_env": "GITHUB_PRIVATE_KEY"},
    )
    second_session = session_factory.create_session(
        target_id="second-org/example",
        target_type="repository",
        region_name="global",
        provider_options={"app_id": "12345", "private_key_env": "GITHUB_PRIVATE_KEY"},
    )

    assert first_session.client._rate_key != second_session.client._rate_key
    assert first_session.client._rate_key[-2:] == ("installation", 111)
    assert second_session.client._rate_key[-2:] == ("installation", 222)


def test_github_session_factory_single_flights_installation_lookup(monkeypatch):
    _install_fake_pygithub(monkeypatch)
    monkeypatch.setenv("GITHUB_PRIVATE_KEY", "private-key")
    started = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def slow_resolve(self, *, client, lookup_paths, target_id):
        calls.append(target_id)
        started.set()
        assert release.wait(timeout=5)
        return 67890

    monkeypatch.setattr(GitHubSessionFactory, "_resolve_installation_id", slow_resolve)
    session_factory = GitHubSessionFactory()
    errors: list[BaseException] = []

    def create_session() -> None:
        try:
            session_factory.create_session(
                target_id="octo-org/example",
                target_type="repository",
                region_name="global",
                provider_options={
                    "app_id": "12345",
                    "private_key_env": "GITHUB_PRIVATE_KEY",
                },
            )
        except BaseException as error:
            errors.append(error)

    first = threading.Thread(target=create_session)
    second = threading.Thread(target=create_session)
    first.start()
    assert started.wait(timeout=5)
    second.start()
    release.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert calls == ["octo-org/example"]
    lookup_clients = [
        client
        for client in FakeGithubClient.instances
        if isinstance(client.kwargs["auth"], FakeAppAuth)
    ]
    assert len(lookup_clients) == 1


def test_github_session_factory_single_flights_installation_client_build(monkeypatch):
    _install_fake_pygithub(monkeypatch)
    monkeypatch.setenv("GITHUB_PRIVATE_KEY", "private-key")
    FakeGithubClient.rest_responses = {"/orgs/octo-org/installation": {"id": 67890}}
    started = threading.Event()
    release = threading.Event()
    calls: list[int] = []
    original_get_installation_auth = FakeAppAuth.get_installation_auth

    def slow_get_installation_auth(self, installation_id):
        calls.append(installation_id)
        started.set()
        assert release.wait(timeout=5)
        return original_get_installation_auth(self, installation_id)

    monkeypatch.setattr(
        FakeAppAuth, "get_installation_auth", slow_get_installation_auth
    )
    session_factory = GitHubSessionFactory()
    sessions = []
    errors: list[BaseException] = []

    def create_session(target_id: str) -> None:
        try:
            sessions.append(
                session_factory.create_session(
                    target_id=target_id,
                    target_type="repository",
                    region_name="global",
                    provider_options={
                        "app_id": "12345",
                        "private_key_env": "GITHUB_PRIVATE_KEY",
                    },
                )
            )
        except BaseException as error:
            errors.append(error)

    first = threading.Thread(target=create_session, args=("octo-org/example",))
    second = threading.Thread(target=create_session, args=("octo-org/other",))
    first.start()
    assert started.wait(timeout=5)
    second.start()
    release.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert calls == [67890]
    assert len(sessions) == 2
    assert sessions[0].client is sessions[1].client
    installation_clients = [
        client
        for client in FakeGithubClient.instances
        if isinstance(client.kwargs["auth"], FakeInstallationAuth)
    ]
    assert len(installation_clients) == 1


def test_github_session_factory_requires_app_private_key(monkeypatch):
    _install_fake_pygithub(monkeypatch)

    with pytest.raises(RuntimeError, match="private_key_env.*private_key_path"):
        GitHubSessionFactory().create_session(
            target_id="octo-org",
            target_type="organization",
            region_name="global",
            provider_options={"app_id": "12345"},
        )


def test_github_session_factory_requires_integer_app_options(monkeypatch):
    _install_fake_pygithub(monkeypatch)
    monkeypatch.setenv("GITHUB_PRIVATE_KEY", "private-key")

    with pytest.raises(RuntimeError, match="app_id must be an integer"):
        GitHubSessionFactory().create_session(
            target_id="octo-org",
            target_type="organization",
            region_name="global",
            provider_options={
                "app_id": "not-an-int",
                "private_key_env": "GITHUB_PRIVATE_KEY",
            },
        )


def test_cached_github_client_reuses_repo_and_organization_objects(monkeypatch):
    _install_fake_pygithub(monkeypatch)
    monkeypatch.setenv("GITHUB_TOKEN", "secret-token")
    session = GitHubSessionFactory().create_session(
        target_id="octo-org/example",
        target_type="repository",
        region_name="global",
        provider_options={},
    )

    raw_client = FakeGithubClient.instances[0]
    raw_client.get_organization = FakeOrganizationClient(raw_client).get_organization

    assert session.client.get_repo("octo-org/example") == {"repo": "octo-org/example"}
    assert session.client.get_repo("octo-org/example") == {"repo": "octo-org/example"}
    assert session.client.get_organization("octo-org") == {"organization": "octo-org"}
    assert session.client.get_organization("octo-org") == {"organization": "octo-org"}

    assert raw_client.repo_calls == ["octo-org/example"]
    assert raw_client.organization_calls == ["octo-org"]


def test_cached_github_client_paces_lazy_search_pages_by_rate_key():
    raw_client = FakeGithubClient()
    now = 100.0
    sleeps: list[float] = []

    def monotonic() -> float:
        return now

    def sleep(seconds: float) -> None:
        nonlocal now
        sleeps.append(seconds)
        now += seconds

    rate_gate = GitHubRateGate(
        min_interval_seconds=1.0, monotonic=monotonic, sleep=sleep
    )
    client = CachedGitHubClient(
        client=raw_client, rate_key=("github", "token"), rate_gate=rate_gate
    )

    raw_client.search_results = FakeLazySearchResults(list(range(101)))
    results = client.search_code("TargetDescriptor", highlight=False)

    assert sleeps == []
    assert list(results) == list(range(101))
    assert sleeps == [1.0]
    assert raw_client.search_calls == [("TargetDescriptor", False)]


def test_cached_github_client_extends_cooldown_after_lazy_secondary_rate_limit():
    raw_client = FakeGithubClient()
    now = 100.0
    sleeps: list[float] = []

    def monotonic() -> float:
        return now

    def sleep(seconds: float) -> None:
        nonlocal now
        sleeps.append(seconds)
        now += seconds

    rate_gate = GitHubRateGate(
        min_interval_seconds=1.0, monotonic=monotonic, sleep=sleep
    )
    client = CachedGitHubClient(
        client=raw_client, rate_key=("github", "token"), rate_gate=rate_gate
    )
    error = RuntimeError("403 forbidden")
    error.status = 403
    error.headers = {"Retry-After": "15"}
    raw_client.search_results = FakeLazySearchResults([], error=error)

    with pytest.raises(RuntimeError, match="403 forbidden"):
        list(client.search_code("TargetDescriptor"))

    raw_client.search_results = FakeLazySearchResults([])
    list(client.search_code("TargetDescriptor"))

    assert sleeps == [15.0]
    assert raw_client.search_calls == [
        ("TargetDescriptor", False),
        ("TargetDescriptor", False),
    ]


def test_github_session_factory_lists_org_and_user_repositories(monkeypatch):
    _install_fake_pygithub(monkeypatch)
    monkeypatch.setenv("GITHUB_TOKEN", "secret-token")

    repositories = GitHubSessionFactory().list_owner_repositories(
        owner_logins=["octo-org", "personal-user"], provider_options={}
    )

    assert [repository.full_name for repository in repositories] == [
        "octo-org/alpha",
        "octo-org/beta",
        "personal-user/alpha",
        "personal-user/beta",
    ]
    raw_client = FakeGithubClient.instances[0]
    assert raw_client.organization_calls == ["octo-org", "personal-user"]
    assert raw_client.user_calls == ["personal-user"]


def test_repository_discovery_cache_key_uses_resolved_token_value(monkeypatch):
    provider = GithubProvider()
    monkeypatch.setenv("GITHUB_TOKEN", "first-token")

    first_key = provider._repository_discovery_cache_key(
        owner_logins=["octo-org"], provider_options={}
    )
    monkeypatch.setenv("GITHUB_TOKEN", "second-token")
    second_key = provider._repository_discovery_cache_key(
        owner_logins=["octo-org"], provider_options={}
    )

    assert first_key != second_key
    assert "first-token" not in str(first_key)
    assert "second-token" not in str(second_key)
