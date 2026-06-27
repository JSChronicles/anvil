from __future__ import annotations

import builtins
from dataclasses import dataclass

import pytest

from anvil.descriptors import ConfigBranch, TargetDescriptor
from anvil.execution_context import ExecutionContext
from anvil.providers.gcp.provider import (
    GcpExecutionTargetData,
    GcpProvider,
    GcpSessionFactory,
)


@dataclass(frozen=True)
class FakeSession:
    project_id: str
    location: str


class FakeSessionFactory:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def create_session(self, *, project_id: str, location: str) -> FakeSession:
        self.calls.append({"project_id": project_id, "location": location})
        return FakeSession(project_id=project_id, location=location)


def _target(**overrides) -> TargetDescriptor:
    values = {
        "config_branch": ConfigBranch.ACCOUNTS,
        "name": "gcp-projects",
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

    with pytest.raises(ValueError, match="explicit projects"):
        provider.validate_target(target)


def test_gcp_resolves_explicit_project_targets_deterministically():
    session_factory = FakeSessionFactory()
    provider = GcpProvider(session_factory=session_factory)
    target = _target(include=["project-b"], role_name="descriptor-compat")

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


def test_gcp_runtime_uses_injected_session_factory():
    session_factory = FakeSessionFactory()
    provider = GcpProvider(session_factory=session_factory)
    target = _target()
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
        {"project_id": "project-a", "location": "us-central1"}
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
