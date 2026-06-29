from __future__ import annotations

import builtins
import sys
from dataclasses import dataclass
from types import ModuleType
from types import SimpleNamespace

import pytest

from anvil.descriptors import ConfigBranch, TargetDescriptor
from anvil.execution_context import ExecutionContext
from anvil.providers.gcp.provider import (
    GcpExecutionTargetData,
    GcpProject,
    GcpProvider,
    GcpSessionFactory,
)


@dataclass(frozen=True)
class FakeSession:
    project_id: str
    location: str


class FakeSessionFactory:
    def __init__(self, *, projects: list[GcpProject] | None = None) -> None:
        self.calls: list[dict[str, str | None]] = []
        self.list_calls: list[dict[str, str | None]] = []
        self.projects = projects or [
            GcpProject(project_id="project-a"),
            GcpProject(project_id="project-b"),
        ]

    def create_session(
        self,
        *,
        project_id: str,
        location: str,
        credentials_path: str | None = None,
        quota_project_id: str | None = None,
    ) -> FakeSession:
        self.calls.append(
            {
                "project_id": project_id,
                "location": location,
                "credentials_path": credentials_path,
                "quota_project_id": quota_project_id,
            }
        )
        return FakeSession(project_id=project_id, location=location)

    def list_projects(
        self,
        *,
        credentials_path: str | None = None,
        quota_project_id: str | None = None,
    ) -> list[GcpProject]:
        self.list_calls.append(
            {
                "credentials_path": credentials_path,
                "quota_project_id": quota_project_id,
            }
        )
        return list(self.projects)


def _target(**overrides) -> TargetDescriptor:
    values = {
        "config_branch": ConfigBranch.ACCOUNTS,
        "name": "gcp-projects",
        "provider": "gcp",
        "mode": "projects",
        "include": ["project-a"],
    }
    values.update(overrides)
    return TargetDescriptor(**values)


def _context() -> ExecutionContext:
    return ExecutionContext(
        regions=["us-central1"], role_name=None, dry_run=False, tasks=[], metadata={}
    )


def test_gcp_provider_metadata_and_default_locations():
    provider = GcpProvider()

    assert provider.metadata.name == "gcp"
    assert provider.default_regions(_target()) == ["us-central1"]
    assert provider.default_regions(_target(regions=["europe-west1"])) == [
        "europe-west1"
    ]
    assert [region.name for region in provider.discover_regions(_target())] == [
        "us-central1"
    ]


def test_gcp_provider_rejects_organization_targets():
    provider = GcpProvider()
    target = TargetDescriptor(config_branch=ConfigBranch.ORGANIZATIONS, name="folder")

    with pytest.raises(ValueError, match="supports projects"):
        provider.validate_target(target)


def test_gcp_resolves_explicit_project_targets_deterministically():
    session_factory = FakeSessionFactory()
    provider = GcpProvider(session_factory=session_factory)
    target = _target(include=["project-b"])

    plan = provider.resolve_execution_targets(
        target=target,
        regions=["us-central1", "europe-west1"],
        include=["project-a", "project-b"],
        exclude=None,
    )

    assert plan.exclusive_execution_key is None
    assert [execution_target.id for execution_target in plan.execution_targets] == [
        "project-a",
        "project-b",
    ]
    assert [execution_target.type for execution_target in plan.execution_targets] == [
        "project",
        "project",
    ]
    assert all(
        isinstance(execution_target.provider_data, GcpExecutionTargetData)
        for execution_target in plan.execution_targets
    )
    assert session_factory.list_calls == []


def test_gcp_project_discovery_resolves_listed_projects():
    session_factory = FakeSessionFactory(
        projects=[
            GcpProject(project_id="project-b"),
            GcpProject(project_id="project-a"),
        ]
    )
    provider = GcpProvider(session_factory=session_factory)
    target = _target(include=None)

    plan = provider.resolve_execution_targets(
        target=target,
        regions=["us-central1"],
        include=None,
        exclude=None,
    )

    assert [execution_target.id for execution_target in plan.execution_targets] == [
        "project-a",
        "project-b",
    ]
    assert session_factory.list_calls == [
        {"credentials_path": None, "quota_project_id": None}
    ]


def test_gcp_project_discovery_applies_include_and_exclude_filters():
    session_factory = FakeSessionFactory(
        projects=[
            GcpProject(project_id="project-a"),
            GcpProject(project_id="project-b"),
            GcpProject(project_id="project-c"),
        ]
    )
    provider = GcpProvider(session_factory=session_factory)
    target = _target(include=None)

    included_plan = provider.resolve_execution_targets(
        target=target,
        regions=["us-central1"],
        include=["project-c", "project-a"],
        exclude=None,
    )
    excluded_plan = provider.resolve_execution_targets(
        target=target,
        regions=["us-central1"],
        include=None,
        exclude=["project-b"],
    )

    assert [execution_target.id for execution_target in included_plan.execution_targets] == [
        "project-c",
        "project-a",
    ]
    assert [execution_target.id for execution_target in excluded_plan.execution_targets] == [
        "project-a",
        "project-c",
    ]


def test_gcp_project_discovery_reports_unknown_filters():
    provider = GcpProvider(session_factory=FakeSessionFactory())
    target = _target(include=None)

    with pytest.raises(ValueError, match="unknown project IDs: missing-project"):
        provider.resolve_execution_targets(
            target=target,
            regions=["us-central1"],
            include=["missing-project"],
            exclude=None,
        )


def test_gcp_project_discovery_errors_are_actionable():
    class FailingSessionFactory(FakeSessionFactory):
        def list_projects(self, **kwargs):
            raise RuntimeError("GCP provider could not discover projects: boom")

    provider = GcpProvider(session_factory=FailingSessionFactory())
    target = _target(include=None)

    with pytest.raises(RuntimeError, match="could not discover projects: boom"):
        provider.resolve_execution_targets(
            target=target,
            regions=["us-central1"],
            include=None,
            exclude=None,
        )


def test_gcp_runtime_uses_injected_session_factory():
    session_factory = FakeSessionFactory()
    provider = GcpProvider(session_factory=session_factory)
    target = _target(
        provider_options={
            "credentials_path": "credentials.json",
            "quota_project_id": "billing-project",
        }
    )
    execution_target = provider.resolve_execution_targets(
        target=target, regions=["us-central1"], include=target.include, exclude=None
    ).execution_targets[0]

    runtime = provider.prepare_execution_runtime(
        target=target, execution_target=execution_target, context=_context()
    )

    assert runtime.build_session(region="us-central1") == FakeSession(
        project_id="project-a", location="us-central1"
    )
    assert session_factory.calls == [
        {
            "project_id": "project-a",
            "location": "us-central1",
            "credentials_path": "credentials.json",
            "quota_project_id": "billing-project",
        }
    ]


def test_gcp_session_factory_imports_sdk_only_when_session_is_built(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "google.auth":
            raise ImportError("missing google auth")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(RuntimeError, match="google-auth"):
        GcpSessionFactory().create_session(
            project_id="project-a", location="us-central1"
        )


def test_gcp_session_factory_uses_credentials_file_and_quota_project(monkeypatch):
    calls: list[dict[str, object]] = []

    def fake_load_credentials_from_file(
        credentials_path, *, scopes, quota_project_id
    ):
        calls.append(
            {
                "credentials_path": credentials_path,
                "scopes": scopes,
                "quota_project_id": quota_project_id,
            }
        )
        return object(), "loaded-project"

    fake_auth = SimpleNamespace(
        load_credentials_from_file=fake_load_credentials_from_file,
        default=lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("google.auth.default should not be used")
        ),
    )
    monkeypatch.setitem(sys.modules, "google", SimpleNamespace(auth=fake_auth))
    monkeypatch.setitem(sys.modules, "google.auth", fake_auth)

    session = GcpSessionFactory().create_session(
        project_id="project-a",
        location="us-central1",
        credentials_path="credentials.json",
        quota_project_id="billing-project",
    )

    assert session.quota_project_id == "billing-project"
    assert calls == [
        {
            "credentials_path": "credentials.json",
            "scopes": ["https://www.googleapis.com/auth/cloud-platform"],
            "quota_project_id": "billing-project",
        }
    ]


def test_gcp_session_factory_list_projects_uses_credentials_options(monkeypatch):
    credential = object()
    credential_calls: list[dict[str, object]] = []
    client_credentials: list[object] = []

    def fake_load_credentials_from_file(
        credentials_path, *, scopes, quota_project_id
    ):
        credential_calls.append(
            {
                "credentials_path": credentials_path,
                "scopes": scopes,
                "quota_project_id": quota_project_id,
            }
        )
        return credential, "loaded-project"

    class FakeProjectsClient:
        def __init__(self, *, credentials):
            client_credentials.append(credentials)

        def search_projects(self):
            return [
                SimpleNamespace(project_id="project-b", display_name="Project B"),
                SimpleNamespace(project_id="project-a", display_name="Project A"),
            ]

    google_module = ModuleType("google")
    auth_module = ModuleType("google.auth")
    auth_module.load_credentials_from_file = fake_load_credentials_from_file
    auth_module.default = lambda **kwargs: (_ for _ in ()).throw(
        AssertionError("google.auth.default should not be used")
    )
    cloud_module = ModuleType("google.cloud")
    resource_manager_module = ModuleType("google.cloud.resourcemanager_v3")
    resource_manager_module.ProjectsClient = FakeProjectsClient
    google_module.auth = auth_module
    google_module.cloud = cloud_module
    cloud_module.resourcemanager_v3 = resource_manager_module
    monkeypatch.setitem(sys.modules, "google", google_module)
    monkeypatch.setitem(sys.modules, "google.auth", auth_module)
    monkeypatch.setitem(sys.modules, "google.cloud", cloud_module)
    monkeypatch.setitem(
        sys.modules, "google.cloud.resourcemanager_v3", resource_manager_module
    )

    projects = GcpSessionFactory().list_projects(
        credentials_path="credentials.json",
        quota_project_id="billing-project",
    )

    assert [project.project_id for project in projects] == ["project-a", "project-b"]
    assert [project.display_name for project in projects] == ["Project A", "Project B"]
    assert client_credentials == [credential]
    assert credential_calls == [
        {
            "credentials_path": "credentials.json",
            "scopes": ["https://www.googleapis.com/auth/cloud-platform"],
            "quota_project_id": "billing-project",
        }
    ]


def test_gcp_session_factory_list_projects_missing_sdk_is_lazy_error(monkeypatch):
    fake_auth = SimpleNamespace(
        load_credentials_from_file=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("load_credentials_from_file should not be used")
        ),
        default=lambda **kwargs: (object(), "default-project"),
    )
    monkeypatch.setitem(sys.modules, "google", SimpleNamespace(auth=fake_auth))
    monkeypatch.setitem(sys.modules, "google.auth", fake_auth)
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "google.cloud" and "resourcemanager_v3" in fromlist:
            raise ImportError("missing resource manager")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(RuntimeError, match="google-cloud-resource-manager"):
        GcpSessionFactory().list_projects()


def test_gcp_session_factory_uses_default_credentials_with_quota_project(monkeypatch):
    calls: list[dict[str, object]] = []

    def fake_default(*, scopes, quota_project_id):
        calls.append({"scopes": scopes, "quota_project_id": quota_project_id})
        return object(), "default-project"

    fake_auth = SimpleNamespace(
        load_credentials_from_file=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("load_credentials_from_file should not be used")
        ),
        default=fake_default,
    )
    monkeypatch.setitem(sys.modules, "google", SimpleNamespace(auth=fake_auth))
    monkeypatch.setitem(sys.modules, "google.auth", fake_auth)

    session = GcpSessionFactory().create_session(
        project_id="project-a",
        location="us-central1",
        quota_project_id="billing-project",
    )

    assert session.quota_project_id == "billing-project"
    assert calls == [
        {
            "scopes": ["https://www.googleapis.com/auth/cloud-platform"],
            "quota_project_id": "billing-project",
        }
    ]
