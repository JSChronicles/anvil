from __future__ import annotations

import builtins
from collections.abc import Iterable, Sequence
from types import SimpleNamespace

import pytest

from anvil.descriptors import TargetDescriptor
from anvil.execution_context import ExecutionContext
from anvil.providers.gitlab.auth import resolve_auth_settings
from anvil.providers.gitlab.provider import (
    GitLabExecutionTargetData,
    GitLabPreflightData,
    GitLabProvider,
)
from anvil.providers.gitlab.session import GitLabSession, GitLabSessionFactory
from anvil.providers.gitlab.target_resolver import GitLabResource, GitLabTargetResolver
from anvil.results import ExecutionStatus


class FakePreparationCache:
    def __init__(self) -> None:
        self.values: dict[object, object] = {}

    def get_or_create(self, *, key, create):
        if key in self.values:
            return self.values[key], True, False
        value = create()
        self.values[key] = value
        return value, False, False


class FakeManager:
    def __init__(self, resources: Sequence[object]) -> None:
        self.resources = resources
        self.get_calls: list[object] = []
        self.list_calls: list[dict[str, object]] = []

    def get(self, selector: object) -> object:
        self.get_calls.append(selector)
        for resource in self.resources:
            if selector in {
                getattr(resource, "id"),
                getattr(resource, "full_path", None),
                getattr(resource, "path_with_namespace", None),
            }:
                return resource
        raise RuntimeError("not found")

    def list(self, **kwargs: object) -> Iterable[object]:
        self.list_calls.append(kwargs)
        return list(self.resources)


class FakeClient:
    def __init__(
        self,
        *,
        groups: list[object] | None = None,
        projects: list[object] | None = None,
    ) -> None:
        self.groups = FakeManager(groups or [])
        self.projects = FakeManager(projects or [])


class FakeSessionFactory:
    def __init__(self, *, client: FakeClient | None = None) -> None:
        self.client = client or FakeClient()
        self.validated = []
        self.closed: list[object] = []

    def validate_auth(self, *, settings) -> None:
        self.validated.append(settings)

    def create_client(self, *, settings) -> object:
        return self.client

    def create_session(
        self, *, target_id, target_type, region_name, settings
    ) -> GitLabSession:
        return GitLabSession(
            target_id=target_id,
            target_type=target_type,
            region_name=region_name,
            client=self.client,
            url=settings.url,
            auth_source=settings.source,
        )

    def close_client(self, client: object) -> None:
        self.closed.append(client)


class FakeTargetResolver:
    def __init__(self, resources: list[GitLabResource]) -> None:
        self.resources = resources
        self.calls: list[dict[str, object]] = []

    def resolve(self, **kwargs: object) -> list[GitLabResource]:
        self.calls.append(kwargs)
        return list(self.resources)


def _target(
    *,
    mode: str = "projects",
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    options: dict[str, object] | None = None,
) -> TargetDescriptor:
    return TargetDescriptor(
        name="gitlab-test",
        provider="gitlab",
        mode=mode,
        regions=["global"],
        include=include,
        exclude=exclude,
        provider_options=options or {"token_env": "ANVIL_TEST_GITLAB_TOKEN"},
        tasks=[{"name": "noop"}],
    )


def _context() -> ExecutionContext:
    return ExecutionContext(regions=["global"], dry_run=True, tasks=[], metadata={})


def test_gitlab_provider_metadata_and_validation() -> None:
    provider = GitLabProvider()

    assert provider.metadata.default_regions == ("global",)
    assert provider.metadata.supported_task_scopes == frozenset({"region", "target"})
    provider.validate_target(_target())

    with pytest.raises(ValueError, match="Unsupported GitLab target mode"):
        provider.validate_target(_target(mode="instance"))
    with pytest.raises(ValueError, match="auth_type.*oauth, private"):
        provider.validate_target(
            _target(options={"token_env": "TOKEN", "auth_type": "job"})
        )
    with pytest.raises(ValueError, match="only region 'global'"):
        provider.validate_target(
            TargetDescriptor(
                name="bad-region",
                provider="gitlab",
                mode="projects",
                regions=["us-east-1"],
                provider_options={"token_env": "TOKEN"},
            )
        )


def test_gitlab_auth_check_is_actionable_for_missing_token(monkeypatch) -> None:
    monkeypatch.delenv("ANVIL_TEST_GITLAB_TOKEN", raising=False)

    result = GitLabProvider().auth_check(_target())

    assert result.status is ExecutionStatus.ERROR
    assert "ANVIL_TEST_GITLAB_TOKEN" in (result.message or "")
    assert "read_api" in (result.remediation or "")


def test_gitlab_auth_check_uses_normalized_instance_settings(monkeypatch) -> None:
    monkeypatch.setenv("ANVIL_TEST_GITLAB_TOKEN", "secret")
    session_factory = FakeSessionFactory()
    provider = GitLabProvider(session_factory=session_factory)

    result = provider.auth_check(
        _target(
            options={
                "url": "HTTPS://GitLab.Example.COM:443/root/",
                "auth_type": "oauth",
                "token_env": "ANVIL_TEST_GITLAB_TOKEN",
            }
        )
    )

    assert result.status is ExecutionStatus.SUCCESS
    assert result.source == "oauth:ANVIL_TEST_GITLAB_TOKEN"
    assert session_factory.validated[0].url == "https://gitlab.example.com/root"


def test_gitlab_preparation_and_execution_plan_are_canonical(monkeypatch) -> None:
    monkeypatch.setenv("ANVIL_TEST_GITLAB_TOKEN", "secret")
    resources = [
        GitLabResource(id=20, full_path="platform/api", type="project"),
        GitLabResource(
            id=10,
            full_path="platform/web",
            type="project",
            metadata={"namespace_id": 3},
        ),
    ]
    session_factory = FakeSessionFactory()
    target_resolver = FakeTargetResolver(resources)
    provider = GitLabProvider(
        session_factory=session_factory, target_resolver=target_resolver
    )
    target = _target(include=["platform/api", "platform/web"])

    preparation = provider.prepare_target(
        target=target,
        context=_context(),
        include=target.include,
        exclude=None,
        cache=FakePreparationCache(),
        benchmark=None,
    )
    plan = provider.resolve_execution_targets(
        target=target,
        regions=["global"],
        include=target.include,
        exclude=None,
        preparation=preparation.data,
    )

    assert preparation.exclusive_execution_keys == (
        ("gitlab", "https://gitlab.com", "project", 10),
        ("gitlab", "https://gitlab.com", "project", 20),
    )
    assert [execution_target.id for execution_target in plan.execution_targets] == [
        "10",
        "20",
    ]
    assert plan.execution_targets[0].name == "platform/web"
    assert plan.execution_targets[0].type == "project"
    assert plan.execution_targets[0].metadata["namespace_id"] == 3
    assert isinstance(
        plan.execution_targets[0].provider_data, GitLabExecutionTargetData
    )


def test_gitlab_runtime_builds_and_closes_target_session(monkeypatch) -> None:
    monkeypatch.setenv("ANVIL_TEST_GITLAB_TOKEN", "secret")
    session_factory = FakeSessionFactory()
    provider = GitLabProvider(session_factory=session_factory)
    target = _target(include=["42"])
    preflight = GitLabPreflightData(
        settings=resolve_auth_settings(target=target, require_token=True),
        resources=[GitLabResource(id=42, full_path="platform/api", type="project")],
    )
    execution_target = provider.resolve_execution_targets(
        target=target,
        regions=["global"],
        include=target.include,
        exclude=None,
        preparation=preflight,
    ).execution_targets[0]

    runtime = provider.prepare_execution_runtime(
        target=target, execution_target=execution_target, context=_context()
    )
    session = runtime.build_session(region="global")
    runtime.close()

    assert session.target_id == 42
    assert session.target_type == "project"
    assert session.region_name == "global"
    assert session_factory.closed == [session_factory.client]


@pytest.mark.parametrize(
    ("auth_type", "token_keyword"),
    [("private", "private_token"), ("oauth", "oauth_token")],
)
def test_session_factory_builds_supported_python_gitlab_clients(
    monkeypatch, auth_type: str, token_keyword: str
) -> None:
    monkeypatch.setenv("ANVIL_TEST_GITLAB_TOKEN", "secret")
    constructor_calls: list[tuple[str, dict[str, object]]] = []

    def client_constructor(url: str, **kwargs: object) -> object:
        constructor_calls.append((url, kwargs))
        return SimpleNamespace()

    monkeypatch.setattr(
        GitLabSessionFactory,
        "_load_python_gitlab",
        staticmethod(lambda: SimpleNamespace(Gitlab=client_constructor)),
    )
    settings = resolve_auth_settings(
        target=_target(
            options={
                "auth_type": auth_type,
                "token_env": "ANVIL_TEST_GITLAB_TOKEN",
                "ca_cert_path": "company-ca.pem",
            }
        ),
        require_token=True,
    )

    client = GitLabSessionFactory().create_client(settings=settings)

    assert client is not None
    assert constructor_calls == [
        (
            "https://gitlab.com",
            {
                token_keyword: "secret",
                "ssl_verify": "company-ca.pem",
                "per_page": 100,
                "retry_transient_errors": True,
                "keep_base_url": True,
            },
        )
    ]


def test_session_factory_reports_missing_optional_dependency(monkeypatch) -> None:
    original_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "gitlab":
            raise ImportError("missing gitlab")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    with pytest.raises(RuntimeError, match="python-gitlab.*anvil\\[gitlab\\]"):
        GitLabSessionFactory._load_python_gitlab()


def test_target_resolver_uses_pagination_options_and_numeric_order(monkeypatch) -> None:
    monkeypatch.setenv("ANVIL_TEST_GITLAB_TOKEN", "secret")
    projects = [
        SimpleNamespace(id=9, path_with_namespace="root/nine", namespace={"id": 1}),
        SimpleNamespace(id=2, path_with_namespace="root/two", namespace={"id": 1}),
    ]
    session_factory = FakeSessionFactory(client=FakeClient(projects=projects))
    resolver = GitLabTargetResolver(session_factory=session_factory)

    resources = resolver.resolve(
        mode="projects",
        include=None,
        exclude=None,
        settings=resolve_auth_settings(target=_target(), require_token=True),
        cache=FakePreparationCache(),
    )

    assert [resource.id for resource in resources] == [2, 9]
    assert session_factory.client.projects.list_calls == [
        {
            "iterator": True,
            "order_by": "id",
            "sort": "asc",
            "membership": True,
            "pagination": "keyset",
        }
    ]
    assert session_factory.closed == [session_factory.client]


def test_target_resolver_resolves_explicit_group_paths(monkeypatch) -> None:
    monkeypatch.setenv("ANVIL_TEST_GITLAB_TOKEN", "secret")
    groups = [
        SimpleNamespace(id=7, full_path="root/nested", parent_id=3),
        SimpleNamespace(id=3, full_path="root", parent_id=None),
    ]
    session_factory = FakeSessionFactory(client=FakeClient(groups=groups))
    resolver = GitLabTargetResolver(session_factory=session_factory)

    resources = resolver.resolve(
        mode="groups",
        include=["root/nested", "3"],
        exclude=None,
        settings=resolve_auth_settings(
            target=_target(mode="groups"), require_token=True
        ),
        cache=FakePreparationCache(),
    )

    assert [(resource.id, resource.full_path) for resource in resources] == [
        (3, "root"),
        (7, "root/nested"),
    ]
    assert resources[1].metadata == {"parent_id": 3}
    assert session_factory.client.groups.get_calls == ["root/nested", 3]
