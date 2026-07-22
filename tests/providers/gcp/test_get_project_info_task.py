from __future__ import annotations

import sys
from dataclasses import dataclass
from types import ModuleType

import pytest

from anvil.actions import ActionRecorder
from anvil.providers.gcp.tasks import get_project_info
from anvil.providers.gcp.tasks.get_project_info import run


@dataclass(frozen=True)
class FakeGcpProject:
    project_id: str
    name: str
    display_name: str
    state: object
    parent: str


@dataclass(frozen=True)
class FakeGcpSession:
    project_id: str = "project-a"
    location: str = "us-central1"
    credentials: object = object()


class FakeProjectsClient:
    calls: list[dict[str, object]] = []
    project: object = FakeGcpProject(
        project_id="project-a",
        name="projects/project-a",
        display_name="Project A",
        state="ACTIVE",
        parent="folders/123",
    )

    def __init__(self, *, credentials: object) -> None:
        self.calls.append({"credentials": credentials})

    def get_project(self, *, name: str) -> object:
        self.calls.append({"name": name})
        return type(self).project


@pytest.fixture
def fake_gcp_resource_manager_sdk(monkeypatch):
    google_module = ModuleType("google")
    google_cloud_module = ModuleType("google.cloud")
    resource_manager_module = ModuleType("google.cloud.resourcemanager_v3")
    resource_manager_module.ProjectsClient = FakeProjectsClient

    monkeypatch.setitem(sys.modules, "google", google_module)
    monkeypatch.setitem(sys.modules, "google.cloud", google_cloud_module)
    monkeypatch.setitem(
        sys.modules, "google.cloud.resourcemanager_v3", resource_manager_module
    )

    FakeProjectsClient.calls = []
    FakeProjectsClient.project = FakeGcpProject(
        project_id="project-a",
        name="projects/project-a",
        display_name="Project A",
        state="ACTIVE",
        parent="folders/123",
    )
    return FakeProjectsClient


def _run_task(
    *,
    session: FakeGcpSession,
    dry_run: bool = False,
    execution_target_type: str = "project",
) -> tuple[dict[str, object], list[str]]:
    actions = ActionRecorder(actions=[])
    result = run(
        provider="gcp",
        execution_target_id=session.project_id,
        execution_target_name=session.project_id,
        execution_target_type=execution_target_type,
        region=session.location,
        session=session,
        dry_run=dry_run,
        metadata={},
        actions=actions,
    )
    return result, actions.actions


def test_get_project_info_reads_project_metadata(fake_gcp_resource_manager_sdk):
    session = FakeGcpSession()

    result, actions = _run_task(session=session)

    assert fake_gcp_resource_manager_sdk.calls == [
        {"credentials": session.credentials},
        {"name": "projects/project-a"},
    ]
    assert result == {
        "project_id": "project-a",
        "region": "us-central1",
        "project_name": "projects/project-a",
        "display_name": "Project A",
        "state": "ACTIVE",
        "parent": "folders/123",
    }
    assert actions == [
        "Read GCP project metadata for project project-a region us-central1"
    ]


def test_get_project_info_supports_mapping_project_response(
    fake_gcp_resource_manager_sdk,
):
    fake_gcp_resource_manager_sdk.project = {
        "project_id": "project-b",
        "name": "projects/project-b",
        "display_name": "Project B",
        "lifecycle_state": "DELETE_REQUESTED",
    }
    session = FakeGcpSession(project_id="project-b", location="global")

    result, actions = _run_task(session=session, dry_run=True)

    assert result == {
        "project_id": "project-b",
        "region": "global",
        "project_name": "projects/project-b",
        "display_name": "Project B",
        "state": "DELETE_REQUESTED",
    }
    assert actions == ["Read GCP project metadata for project project-b region global"]


def test_get_project_info_supports_enum_state(fake_gcp_resource_manager_sdk):
    class State:
        name = "ACTIVE"

    fake_gcp_resource_manager_sdk.project = FakeGcpProject(
        project_id="project-a",
        name="projects/project-a",
        display_name="Project A",
        state=State(),
        parent="organizations/123",
    )

    result, _actions = _run_task(session=FakeGcpSession())

    assert result["state"] == "ACTIVE"
    assert result["parent"] == "organizations/123"


def test_get_project_info_imports_google_sdk_lazily(monkeypatch):
    real_import = __import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "google.cloud":
            raise ImportError("missing google cloud")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr("builtins.__import__", fake_import)

    with pytest.raises(
        RuntimeError, match=r"google-cloud-resource-manager.*anvil\[gcp\]"
    ):
        _run_task(session=FakeGcpSession())


def test_get_project_info_module_import_does_not_require_google_sdk():
    assert "resourcemanager_v3" not in get_project_info.__dict__
    assert callable(get_project_info.run)


def test_get_project_info_requires_gcp_project_target(fake_gcp_resource_manager_sdk):
    with pytest.raises(RuntimeError, match="gcp provider"):
        run(
            provider="aws",
            execution_target_id="123456789012",
            execution_target_name="test",
            execution_target_type="account",
            region="us-east-1",
            session=FakeGcpSession(),
            dry_run=False,
            metadata={},
            actions=ActionRecorder(actions=[]),
        )

    with pytest.raises(RuntimeError, match="GCP project execution target"):
        _run_task(session=FakeGcpSession(), execution_target_type="folder")


def test_get_project_info_requires_session_credentials(fake_gcp_resource_manager_sdk):
    session = FakeGcpSession(credentials=None)

    with pytest.raises(RuntimeError, match="GCP session credentials"):
        _run_task(session=session)
