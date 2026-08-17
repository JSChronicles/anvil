from __future__ import annotations

from collections.abc import Iterable, Sequence
from types import SimpleNamespace

import pytest

from anvil.descriptors import TargetDescriptor
from anvil.providers.gitlab.auth import resolve_auth_settings
from anvil.providers.gitlab.target_resolver import GitLabTargetResolver


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
    def __init__(
        self, resources: Sequence[object], *, list_error: Exception | None = None
    ) -> None:
        self.resources = resources
        self.list_error = list_error
        self.get_calls: list[object] = []
        self.list_calls: list[dict[str, object]] = []

    def get(self, selector: object) -> object:
        self.get_calls.append(selector)
        for resource in self.resources:
            if selector in {
                getattr(resource, "id", None),
                getattr(resource, "full_path", None),
                getattr(resource, "path_with_namespace", None),
            }:
                return resource
        raise LookupError(f"unknown selector {selector}")

    def list(self, **kwargs: object) -> Iterable[object]:
        self.list_calls.append(kwargs)
        if self.list_error is not None:
            raise self.list_error
        return list(self.resources)


class FakeClient:
    def __init__(
        self,
        *,
        groups: list[object] | None = None,
        projects: list[object] | None = None,
        list_error: Exception | None = None,
    ) -> None:
        self.groups = FakeManager(groups or [], list_error=list_error)
        self.projects = FakeManager(projects or [], list_error=list_error)


class CountingSessionFactory:
    def __init__(self, client: FakeClient) -> None:
        self.client = client
        self.create_calls = 0
        self.closed: list[object] = []

    def create_client(self, *, settings) -> object:
        self.create_calls += 1
        return self.client

    def close_client(self, client: object) -> None:
        self.closed.append(client)


def _target(*, mode: str = "projects") -> TargetDescriptor:
    return TargetDescriptor(
        name="gitlab-test",
        provider="gitlab",
        mode=mode,
        regions=["global"],
        provider_options={"token_env": "ANVIL_TEST_GITLAB_TOKEN"},
        tasks=[],
    )


def _settings(monkeypatch, *, mode: str = "projects"):
    monkeypatch.setenv("ANVIL_TEST_GITLAB_TOKEN", "secret-token")
    return resolve_auth_settings(target=_target(mode=mode), require_token=True)


def test_group_discovery_uses_complete_offset_iterator_and_deterministic_order(
    monkeypatch,
) -> None:
    groups = [
        SimpleNamespace(id=20, full_path="root/nested", parent_id=10),
        SimpleNamespace(id=10, full_path="root", parent_id=None),
    ]
    session_factory = CountingSessionFactory(FakeClient(groups=groups))

    resources = GitLabTargetResolver(session_factory=session_factory).resolve(
        mode="groups",
        include=None,
        exclude=None,
        settings=_settings(monkeypatch, mode="groups"),
        cache=FakePreparationCache(),
    )

    assert [resource.id for resource in resources] == [10, 20]
    assert session_factory.client.groups.list_calls == [
        {"iterator": True, "order_by": "id", "sort": "asc", "all_available": False}
    ]
    assert session_factory.closed == [session_factory.client]


def test_discovery_cache_avoids_duplicate_clients_and_api_calls(monkeypatch) -> None:
    client = FakeClient(
        projects=[SimpleNamespace(id=1, path_with_namespace="root/project")]
    )
    session_factory = CountingSessionFactory(client)
    resolver = GitLabTargetResolver(session_factory=session_factory)
    cache = FakePreparationCache()
    settings = _settings(monkeypatch)

    first = resolver.resolve(
        mode="projects", include=None, exclude=None, settings=settings, cache=cache
    )
    second = resolver.resolve(
        mode="projects", include=None, exclude=None, settings=settings, cache=cache
    )

    assert first == second
    assert first is not second
    assert session_factory.create_calls == 1
    assert len(client.projects.list_calls) == 1
    assert session_factory.closed == [client]


def test_discovery_cache_is_partitioned_by_credential_identity(monkeypatch) -> None:
    client = FakeClient(
        projects=[SimpleNamespace(id=1, path_with_namespace="root/project")]
    )
    session_factory = CountingSessionFactory(client)
    resolver = GitLabTargetResolver(session_factory=session_factory)
    cache = FakePreparationCache()

    first_settings = _settings(monkeypatch)
    resolver.resolve(
        mode="projects",
        include=None,
        exclude=None,
        settings=first_settings,
        cache=cache,
    )
    monkeypatch.setenv("ANVIL_TEST_GITLAB_TOKEN", "replacement-token")
    second_settings = resolve_auth_settings(target=_target(), require_token=True)
    resolver.resolve(
        mode="projects",
        include=None,
        exclude=None,
        settings=second_settings,
        cache=cache,
    )

    assert session_factory.create_calls == 2
    assert len(client.projects.list_calls) == 2


def test_discovery_excludes_resources_by_id_and_nested_path(monkeypatch) -> None:
    client = FakeClient(
        groups=[
            SimpleNamespace(id=1, full_path="root", parent_id=None),
            SimpleNamespace(id=2, full_path="root/one", parent_id=1),
            SimpleNamespace(id=3, full_path="root/two", parent_id=1),
        ]
    )
    resolver = GitLabTargetResolver(session_factory=CountingSessionFactory(client))

    resources = resolver.resolve(
        mode="groups",
        include=None,
        exclude=["1", "root/two"],
        settings=_settings(monkeypatch, mode="groups"),
        cache=FakePreparationCache(),
    )

    assert [(resource.id, resource.full_path) for resource in resources] == [
        (2, "root/one")
    ]


def test_discovery_rejects_unknown_excludes(monkeypatch) -> None:
    resolver = GitLabTargetResolver(
        session_factory=CountingSessionFactory(
            FakeClient(
                projects=[SimpleNamespace(id=1, path_with_namespace="root/project")]
            )
        )
    )

    with pytest.raises(ValueError, match="unknown project selectors: missing"):
        resolver.resolve(
            mode="projects",
            include=None,
            exclude=["missing"],
            settings=_settings(monkeypatch),
            cache=FakePreparationCache(),
        )


def test_explicit_selection_rejects_duplicate_canonical_ids(monkeypatch) -> None:
    resolver = GitLabTargetResolver(
        session_factory=CountingSessionFactory(
            FakeClient(
                projects=[SimpleNamespace(id=7, path_with_namespace="root/project")]
            )
        )
    )

    with pytest.raises(ValueError, match="duplicate canonical IDs: 7"):
        resolver.resolve(
            mode="projects",
            include=["7", "root/project"],
            exclude=None,
            settings=_settings(monkeypatch),
            cache=FakePreparationCache(),
        )


@pytest.mark.parametrize(
    "resource",
    [
        SimpleNamespace(id=None, path_with_namespace="root/project"),
        SimpleNamespace(id=True, path_with_namespace="root/project"),
        SimpleNamespace(id=1, path_with_namespace=""),
    ],
)
def test_discovery_rejects_malformed_project_identity(monkeypatch, resource) -> None:
    resolver = GitLabTargetResolver(
        session_factory=CountingSessionFactory(FakeClient(projects=[resource]))
    )

    with pytest.raises(RuntimeError, match="GitLab project discovery failed"):
        resolver.resolve(
            mode="projects",
            include=None,
            exclude=None,
            settings=_settings(monkeypatch),
            cache=FakePreparationCache(),
        )


def test_discovery_failure_closes_client_and_redacts_token(monkeypatch) -> None:
    client = FakeClient(list_error=RuntimeError("failure for secret-token"))
    session_factory = CountingSessionFactory(client)
    resolver = GitLabTargetResolver(session_factory=session_factory)

    with pytest.raises(RuntimeError, match="failure for <redacted>") as error:
        resolver.resolve(
            mode="projects",
            include=None,
            exclude=None,
            settings=_settings(monkeypatch),
            cache=FakePreparationCache(),
        )

    assert "secret-token" not in str(error.value)
    assert session_factory.closed == [client]
