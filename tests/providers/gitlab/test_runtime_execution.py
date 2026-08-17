from __future__ import annotations

from collections.abc import Iterable
from types import SimpleNamespace

import pytest

from anvil.actions import ActionRecorder
from anvil.descriptors import TargetDescriptor
from anvil.execution_context import ExecutionContext
from anvil.providers.base import ExecutionTarget
from anvil.providers.gitlab.auth import resolve_auth_settings
from anvil.providers.gitlab.provider import GitLabExecutionTargetData, GitLabProvider
from anvil.providers.gitlab.session import GitLabSession
from anvil.results import ExecutionStatus
from anvil.runner import _execute_provider_execution_target
from anvil.task_loader import ResolvedTask


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
    def __init__(self, resource: object) -> None:
        self.resource = resource
        self.get_calls: list[object] = []

    def get(self, selector: object) -> object:
        self.get_calls.append(selector)
        valid_selectors = {
            getattr(self.resource, "id"),
            getattr(self.resource, "full_path", None),
            getattr(self.resource, "path_with_namespace", None),
        }
        if selector not in valid_selectors:
            raise LookupError(f"unknown selector {selector}")
        return self.resource

    def list(self, **kwargs: object) -> Iterable[object]:
        return [self.resource]


class FakeClient:
    def __init__(self, *, resource: object, resource_type: str) -> None:
        self.groups = FakeManager(resource)
        self.projects = FakeManager(resource)
        self.resource_type = resource_type


class FakeSessionFactory:
    def __init__(self, *, resource: object, resource_type: str) -> None:
        self.resource = resource
        self.resource_type = resource_type
        self.clients: list[FakeClient] = []
        self.closed: list[object] = []

    def create_client(self, *, settings) -> object:
        client = FakeClient(resource=self.resource, resource_type=self.resource_type)
        self.clients.append(client)
        return client

    def create_session(
        self, *, target_id, target_type, region_name, settings
    ) -> GitLabSession:
        return GitLabSession(
            target_id=target_id,
            target_type=target_type,
            region_name=region_name,
            client=self.create_client(settings=settings),
            url=settings.url,
            auth_source=settings.source,
        )

    def close_client(self, client: object) -> None:
        self.closed.append(client)


def _target(*, mode: str, selector: str) -> TargetDescriptor:
    return TargetDescriptor(
        name=f"gitlab-{mode}",
        provider="gitlab",
        mode=mode,
        regions=["global"],
        include=[selector],
        provider_options={"token_env": "ANVIL_TEST_GITLAB_TOKEN"},
        tasks=[{"name": "inspect_gitlab_target"}],
    )


@pytest.mark.parametrize(
    ("mode", "resource_type", "selector", "resource"),
    [
        (
            "projects",
            "project",
            "root/nested/project",
            SimpleNamespace(
                id=42,
                path_with_namespace="root/nested/project",
                namespace={"id": 7, "full_path": "root/nested"},
                manageable=True,
            ),
        ),
        (
            "groups",
            "group",
            "root/nested",
            SimpleNamespace(
                id=7, full_path="root/nested", parent_id=3, manageable=True
            ),
        ),
    ],
)
def test_gitlab_target_runs_normal_task_with_complete_task_call_context(
    monkeypatch, mode: str, resource_type: str, selector: str, resource: object
) -> None:
    monkeypatch.setenv("ANVIL_TEST_GITLAB_TOKEN", "secret-token")
    session_factory = FakeSessionFactory(resource=resource, resource_type=resource_type)
    provider = GitLabProvider(session_factory=session_factory)
    target = _target(mode=mode, selector=selector)
    captured: dict[str, object] = {}

    def inspect_gitlab_target(
        *,
        provider: str,
        execution_target_id: str,
        execution_target_name: str,
        execution_target_type: str,
        region: str,
        session: GitLabSession,
        dry_run: bool,
        metadata: dict[str, object],
        dependency_data: dict[str, object],
        actions: ActionRecorder,
    ) -> dict[str, object]:
        manager = getattr(session.client, f"{session.target_type}s")
        manageable = manager.get(session.target_id)
        captured.update(
            {
                "provider": provider,
                "execution_target_id": execution_target_id,
                "execution_target_name": execution_target_name,
                "execution_target_type": execution_target_type,
                "region": region,
                "session": session,
                "dry_run": dry_run,
                "metadata": metadata,
                "dependency_data": dependency_data,
                "actions": actions,
            }
        )
        return {
            "manageable": getattr(manageable, "manageable"),
            "resource_id": getattr(manageable, "id"),
        }

    context = ExecutionContext(
        regions=["global"],
        dry_run=True,
        tasks=[
            ResolvedTask(
                name="inspect_gitlab_target",
                run=inspect_gitlab_target,
                depends_on=[],
                metadata={"task_metadata": True},
            )
        ],
        metadata={"configured_metadata": True},
    )
    preparation = provider.prepare_target(
        target=target,
        context=context,
        include=target.include,
        exclude=None,
        cache=FakePreparationCache(),
        benchmark={},
    )
    execution_target = provider.resolve_execution_targets(
        target=target,
        regions=["global"],
        include=target.include,
        exclude=None,
        preparation=preparation.data,
    ).execution_targets[0]

    result = _execute_provider_execution_target(
        provider=provider,
        target=target,
        execution_target=execution_target,
        context=context,
    )

    assert result.status is ExecutionStatus.SUCCESS
    assert result.id == str(getattr(resource, "id"))
    assert result.name == selector
    assert result.type == resource_type
    assert result.provider == "gitlab"
    assert result.metadata["gitlab_instance_url"] == "https://gitlab.com"
    assert result.tasks[0].status is ExecutionStatus.SUCCESS
    assert result.tasks[0].result == {
        "manageable": True,
        "resource_id": getattr(resource, "id"),
    }
    assert captured["provider"] == "gitlab"
    assert captured["execution_target_id"] == str(getattr(resource, "id"))
    assert captured["execution_target_name"] == selector
    assert captured["execution_target_type"] == resource_type
    assert captured["region"] == "global"
    assert captured["dry_run"] is True
    assert captured["metadata"] == {"configured_metadata": True, "task_metadata": True}
    assert captured["dependency_data"] == {}
    assert isinstance(captured["actions"], ActionRecorder)

    runtime_session = captured["session"]
    assert isinstance(runtime_session, GitLabSession)
    runtime_client = runtime_session.client
    runtime_manager = getattr(runtime_client, f"{resource_type}s")
    assert runtime_manager.get_calls == [getattr(resource, "id")]
    assert len(session_factory.clients) == 2
    assert session_factory.closed == session_factory.clients

    payload = result.to_dict()
    assert payload["id"] == str(getattr(resource, "id"))
    assert payload["provider"] == "gitlab"
    assert payload["status"] == "success"
    task_payloads = payload["tasks"]
    assert isinstance(task_payloads, list)
    assert task_payloads[0]["result"] == {
        "manageable": True,
        "resource_id": getattr(resource, "id"),
    }


def test_gitlab_runtime_rejects_invalid_target_data_and_region(monkeypatch) -> None:
    monkeypatch.setenv("ANVIL_TEST_GITLAB_TOKEN", "secret-token")
    resource = SimpleNamespace(id=42, path_with_namespace="root/project")
    session_factory = FakeSessionFactory(resource=resource, resource_type="project")
    provider = GitLabProvider(session_factory=session_factory)
    target = _target(mode="projects", selector="root/project")

    wrong_provider = ExecutionTarget(
        id="42",
        name="root/project",
        type="project",
        provider="github",
        regions=["global"],
        provider_data=object(),
    )
    with pytest.raises(ValueError, match="not gitlab"):
        provider.prepare_execution_runtime(
            target=target,
            execution_target=wrong_provider,
            context=ExecutionContext(
                regions=["global"], dry_run=False, tasks=[], metadata={}
            ),
        )

    wrong_data = ExecutionTarget(
        id="42",
        name="root/project",
        type="project",
        provider="gitlab",
        regions=["global"],
        provider_data=object(),
    )
    with pytest.raises(TypeError, match="GitLabExecutionTargetData"):
        provider.prepare_execution_runtime(
            target=target,
            execution_target=wrong_data,
            context=ExecutionContext(
                regions=["global"], dry_run=False, tasks=[], metadata={}
            ),
        )

    data = GitLabExecutionTargetData(
        resource_id=42,
        resource_type="project",
        settings=resolve_auth_settings(target=target, require_token=True),
        session_factory=session_factory,
    )
    valid_target = ExecutionTarget(
        id="42",
        name="root/project",
        type="project",
        provider="gitlab",
        regions=["global"],
        provider_data=data,
    )
    runtime = provider.prepare_execution_runtime(
        target=target,
        execution_target=valid_target,
        context=ExecutionContext(
            regions=["global"], dry_run=False, tasks=[], metadata={}
        ),
    )
    with pytest.raises(ValueError, match="requires region 'global'"):
        runtime.build_session(region="us-east-1")
