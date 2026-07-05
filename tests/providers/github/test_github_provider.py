from __future__ import annotations

import builtins
import sys
from dataclasses import dataclass
from types import ModuleType

import pytest

from anvil.descriptors import ConfigBranch, TargetDescriptor
from anvil.execution_context import ExecutionContext
from anvil.providers.base import ExecutionTarget
from anvil.providers.github import create_provider
from anvil.providers.github.provider import (
    DEFAULT_GITHUB_API_VERSION,
    GitHubSessionFactory,
    GithubRepository,
    GithubExecutionTargetData,
    GithubProvider,
)
from anvil.results import ExecutionStatus


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


class FakeGithubClient:
    instances: list["FakeGithubClient"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.repo_calls: list[str] = []
        self.organization_calls: list[str] = []
        self.user_calls: list[str] = []
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


class FakeLegacyOrganizationClient:
    def __init__(self, raw_client):
        self.raw_client = raw_client

    def get_organization(self, login):
        self.raw_client.organization_calls.append(login)
        return {"organization": login}


def _target(**overrides) -> TargetDescriptor:
    values = {
        "config_branch": ConfigBranch.TARGETS,
        "name": "github-repositories",
        "provider": "github",
        "mode": "repositories",
        "include": ["octo-org/example"],
        "provider_options": {"auth_type": "token", "token_env": "GITHUB_TOKEN"},
    }
    values.update(overrides)
    return TargetDescriptor(**values)


def _context() -> ExecutionContext:
    return ExecutionContext(
        regions=["global"], role_name=None, dry_run=False, tasks=[], metadata={}
    )


def _install_fake_pygithub(monkeypatch) -> ModuleType:
    FakeGithubClient.instances = []
    FakeAppAuth.instances = []
    github_module = ModuleType("github")
    github_module.Auth = FakeAuth
    github_module.Github = FakeGithubClient
    monkeypatch.setitem(sys.modules, "github", github_module)
    return github_module


def test_github_provider_metadata_and_default_location():
    provider = create_provider()
    target = _target()

    assert provider.metadata.name == "github"
    assert provider.metadata.display_name == "GitHub"
    assert provider.default_regions(target) == ["global"]
    assert provider.discover_regions(target)[0].name == "global"


def test_github_provider_requires_explicit_include():
    with pytest.raises(ValueError, match="requires include"):
        _target(include=None)


def test_github_provider_rejects_exclude():
    with pytest.raises(ValueError, match="does not allow exclude|include and exclude"):
        _target(exclude=["octo-org/skip"])


def test_github_provider_rejects_unknown_auth_type():
    with pytest.raises(ValueError, match="auth_type"):
        _target(provider_options={"auth_type": "basic"})


def test_github_provider_rejects_repository_include_without_owner():
    provider = create_provider()
    target = _target(include=["example"])

    with pytest.raises(ValueError, match="owner/repo"):
        provider.validate_target(target)


def test_github_provider_rejects_organization_include_with_repo_path():
    provider = create_provider()
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


def test_github_provider_resolves_repository_targets_offline():
    provider = create_provider()
    target = _target(include=["octo-org/example", "octo-org/other"])

    plan = provider.resolve_execution_targets(
        target=target, regions=["global"], include=target.include, exclude=None
    )

    assert [(item.id, item.type) for item in plan.execution_targets] == [
        ("octo-org/example", "repository"),
        ("octo-org/other", "repository"),
    ]


def test_github_auth_check_is_deferred_offline():
    provider = create_provider()
    result = provider.auth_check(_target())

    assert result.status is ExecutionStatus.SUCCESS
    assert result.source == "deferred"


def test_github_runtime_uses_injected_session_factory():
    session_factory = FakeSessionFactory()
    provider = GithubProvider(session_factory=session_factory)
    target = _target(
        provider_options={
            "auth_type": "token",
            "token_env": "ANVIL_GITHUB_TOKEN",
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
                "auth_type": "token",
                "token_env": "ANVIL_GITHUB_TOKEN",
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
            provider_options={"auth_type": "token"},
        )


def test_github_session_factory_uses_token_env_api_url_and_default_version(monkeypatch):
    _install_fake_pygithub(monkeypatch)
    monkeypatch.setenv("ANVIL_GITHUB_TOKEN", "secret-token")

    session = GitHubSessionFactory().create_session(
        target_id="octo-org/example",
        target_type="repository",
        region_name="global",
        provider_options={
            "auth_type": "token",
            "token_env": "ANVIL_GITHUB_TOKEN",
            "api_url": "https://github.example/api/v3",
        },
    )

    assert session.target_id == "octo-org/example"
    assert session.region_name == "global"
    assert session.auth_type == "token"
    assert session.api_url == "https://github.example/api/v3"
    assert session.api_version == DEFAULT_GITHUB_API_VERSION
    assert FakeGithubClient.instances[0].kwargs == {
        "auth": FakeGithubClient.instances[0].kwargs["auth"],
        "base_url": "https://github.example/api/v3",
        "api_version": DEFAULT_GITHUB_API_VERSION,
    }
    assert FakeGithubClient.instances[0].kwargs["auth"].token == "secret-token"


def test_github_session_factory_uses_custom_api_version(monkeypatch):
    _install_fake_pygithub(monkeypatch)
    monkeypatch.setenv("GITHUB_TOKEN", "secret-token")

    session = GitHubSessionFactory().create_session(
        target_id="octo-org/example",
        target_type="repository",
        region_name="global",
        provider_options={"auth_type": "token", "api_version": "2023-01-01"},
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
            provider_options={
                "auth_type": "token",
                "token_env": "MISSING_GITHUB_TOKEN",
            },
        )


def test_github_session_factory_uses_app_auth_private_key_env(monkeypatch):
    _install_fake_pygithub(monkeypatch)
    monkeypatch.setenv("GITHUB_PRIVATE_KEY", "private-key")

    session = GitHubSessionFactory().create_session(
        target_id="octo-org",
        target_type="organization",
        region_name="global",
        provider_options={
            "auth_type": "app",
            "app_id": "12345",
            "installation_id": "67890",
            "private_key_env": "GITHUB_PRIVATE_KEY",
        },
    )

    auth = FakeGithubClient.instances[0].kwargs["auth"]
    assert session.auth_type == "app"
    assert FakeAppAuth.instances[0].app_id == 12345
    assert FakeAppAuth.instances[0].private_key == "private-key"
    assert auth.installation_id == 67890


def test_github_session_factory_uses_app_auth_private_key_path(monkeypatch, tmp_path):
    _install_fake_pygithub(monkeypatch)
    private_key_path = tmp_path / "github-app.pem"
    private_key_path.write_text("file-private-key", encoding="utf-8")

    GitHubSessionFactory().create_session(
        target_id="octo-org",
        target_type="organization",
        region_name="global",
        provider_options={
            "auth_type": "app",
            "app_id": "12345",
            "installation_id": "67890",
            "private_key_path": str(private_key_path),
        },
    )

    auth = FakeGithubClient.instances[0].kwargs["auth"]
    assert FakeAppAuth.instances[0].private_key == "file-private-key"
    assert auth.installation_id == 67890


def test_github_session_factory_requires_app_private_key(monkeypatch):
    _install_fake_pygithub(monkeypatch)

    with pytest.raises(RuntimeError, match="private_key_env.*private_key_path"):
        GitHubSessionFactory().create_session(
            target_id="octo-org",
            target_type="organization",
            region_name="global",
            provider_options={
                "auth_type": "app",
                "app_id": "12345",
                "installation_id": "67890",
            },
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
                "auth_type": "app",
                "app_id": "not-an-int",
                "installation_id": "67890",
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
        provider_options={"auth_type": "token"},
    )

    raw_client = FakeGithubClient.instances[0]
    raw_client.get_organization = FakeLegacyOrganizationClient(
        raw_client
    ).get_organization

    assert session.client.get_repo("octo-org/example") == {"repo": "octo-org/example"}
    assert session.client.get_repo("octo-org/example") == {"repo": "octo-org/example"}
    assert session.client.get_organization("octo-org") == {"organization": "octo-org"}
    assert session.client.get_organization("octo-org") == {"organization": "octo-org"}

    assert raw_client.repo_calls == ["octo-org/example"]
    assert raw_client.organization_calls == ["octo-org"]


def test_github_session_factory_lists_org_and_user_repositories(monkeypatch):
    _install_fake_pygithub(monkeypatch)
    monkeypatch.setenv("GITHUB_TOKEN", "secret-token")

    repositories = GitHubSessionFactory().list_owner_repositories(
        owner_logins=["octo-org", "personal-user"],
        provider_options={"auth_type": "token"},
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
