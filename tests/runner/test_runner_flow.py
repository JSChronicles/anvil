import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from anvil.auth import AuthSource
from anvil.descriptors import ConfigBranch, TargetDescriptor
from anvil.execution_context import ExecutionContext
from anvil.providers.base import ProviderRegion
from anvil.providers.azure.provider import AzureSubscription
from anvil.providers.gcp.provider import GcpProject
from anvil.results import EntityResult, AuthResult, ExecutionStatus, TargetResult
from anvil.runner import (
    AuthCheckCache,
    OrganizationRunCache,
    OrganizationRunCacheEntry,
    PreparedTarget,
    prepare_target,
    run_auth_checks,
    run_multiple_targets,
    run_prepared_target,
)
from anvil.task_loader import ResolvedExecution, ResolvedTask
from anvil.validators import load_config_descriptors, validate_config_schema


def _empty_resolved_execution(**kwargs):
    return ResolvedExecution(ordered=[], adjacency={})


def _load_targets(config: dict) -> list[TargetDescriptor]:
    validate_config_schema(config=config)
    return load_config_descriptors(config=config).targets


def test_runner_auth_failure_short_circuits(monkeypatch):
    monkeypatch.setattr(
        "anvil.runner.auth_check",
        lambda **kwargs: AuthResult(
            target_name=kwargs["target_name"],
            status=ExecutionStatus.ERROR,
            source="test",
            started_at="start",
            ended_at="end",
            duration_seconds=0.0,
            message="fail",
        ),
    )

    target = TargetDescriptor(
        config_branch=ConfigBranch.TARGETS,
        name="org",
        tasks=[],
        regions=["us-east-1"],
        role_name="role",
        max_workers=1,
        dry_run=True,
        fail_fast=True,
    )

    engine_result = run_multiple_targets(
        targets=[target],
        max_parallel_targets=1,
        cli_dry_run=True,
        cli_include=None,
        cli_exclude=None,
    )

    assert engine_result.auth_results[0].status is ExecutionStatus.ERROR
    assert engine_result.target_results == []
    assert engine_result.has_auth_failures


def test_run_dispatches_non_aws_provider_without_aws_auth_or_preflight(monkeypatch):
    def fail_auth_check(**kwargs):
        raise AssertionError("AWS auth should not run for non-AWS providers")

    monkeypatch.setattr("anvil.runner.auth_check", fail_auth_check)
    monkeypatch.setattr(
        "anvil.runner.AwsProvider.preflight_execution",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("AWS organization preflight should not run")
        ),
    )
    monkeypatch.setattr(
        "anvil.providers.azure.provider.AzureSessionFactory.create_session",
        lambda self, **kwargs: type(
            "_AzureSession", (), {"region_name": kwargs["location"]}
        )(),
    )
    resolved_provider_names: list[str] = []

    def fake_resolve_tasks(**kwargs):
        resolved_provider_names.append(kwargs["provider_name"])
        return ResolvedExecution(ordered=[], adjacency={})

    monkeypatch.setattr("anvil.runner.resolve_tasks", fake_resolve_tasks)

    target = TargetDescriptor(
        config_branch=ConfigBranch.TARGETS,
        name="azure-subscriptions",
        provider="azure",
        mode="subscriptions",
        include=["11111111-2222-3333-4444-555555555555"],
        tasks=[],
    )

    engine_result = run_multiple_targets(
        targets=[target],
        max_parallel_targets=1,
        cli_dry_run=None,
        cli_include=None,
        cli_exclude=None,
    )

    assert resolved_provider_names == ["azure"]
    assert engine_result.auth_results[0].status is ExecutionStatus.SUCCESS
    assert engine_result.auth_results[0].source == "deferred"
    assert engine_result.target_results[0].entities[0].id == (
        "11111111-2222-3333-4444-555555555555"
    )


def test_auth_check_dispatches_non_aws_provider_without_aws_auth(monkeypatch):
    def fail_auth_check(**kwargs):
        raise AssertionError("AWS auth should not run for non-AWS providers")

    monkeypatch.setattr("anvil.runner.auth_check", fail_auth_check)

    target = TargetDescriptor(
        config_branch=ConfigBranch.TARGETS,
        name="gcp-projects",
        provider="gcp",
        mode="projects",
        include=["project-a"],
        tasks=[],
    )

    engine_result = run_auth_checks(targets=[target])

    assert engine_result.auth_results[0].status is ExecutionStatus.SUCCESS
    assert engine_result.auth_results[0].source == "deferred"


def test_non_aws_provider_session_failure_is_reported_without_aws_paths(monkeypatch):
    monkeypatch.setattr(
        "anvil.runner.auth_check",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("AWS auth should not run for non-AWS providers")
        ),
    )
    monkeypatch.setattr(
        "anvil.providers.azure.provider.AzureSessionFactory.create_session",
        lambda self, **kwargs: (_ for _ in ()).throw(
            RuntimeError("Azure provider requires optional dependency 'azure-identity'")
        ),
    )

    target = TargetDescriptor(
        config_branch=ConfigBranch.TARGETS,
        name="azure-subscriptions",
        provider="azure",
        mode="subscriptions",
        include=["11111111-2222-3333-4444-555555555555"],
        tasks=[{"name": "noop"}],
    )

    engine_result = run_multiple_targets(
        targets=[target],
        max_parallel_targets=1,
        cli_dry_run=None,
        cli_include=None,
        cli_exclude=None,
    )

    entity_result = engine_result.target_results[0].entities[0]
    assert entity_result.status is ExecutionStatus.ERROR
    assert "azure-identity" in entity_result.error


def test_non_aws_provider_options_reach_runtime_session_factory(monkeypatch):
    session_calls: list[dict[str, str | None]] = []

    monkeypatch.setattr(
        "anvil.providers.azure.provider.AzureSessionFactory.create_session",
        lambda self, **kwargs: (
            session_calls.append(kwargs)
            or type("_AzureSession", (), {"region_name": kwargs["location"]})()
        ),
    )

    target = TargetDescriptor(
        config_branch=ConfigBranch.TARGETS,
        name="azure-subscriptions",
        provider="azure",
        mode="subscriptions",
        regions=["eastus"],
        include=["sub-a"],
        provider_options={
            "tenant_id": "tenant-a",
            "client_id": "client-a",
            "client_secret": "secret-a",
            "subscription_id": "billing-sub",
        },
        tasks=[],
    )

    engine_result = run_multiple_targets(
        targets=[target],
        max_parallel_targets=1,
        cli_dry_run=None,
        cli_include=None,
        cli_exclude=None,
    )

    assert not engine_result.target_results[0].has_failures
    assert session_calls == [
        {
            "subscription_id": "sub-a",
            "location": "eastus",
            "tenant_id": "tenant-a",
            "client_id": "client-a",
            "client_secret": "secret-a",
            "configured_subscription_id": "billing-sub",
        }
    ]


def test_azure_subscription_discovery_runs_without_aws_paths(monkeypatch):
    subscription_calls: list[dict[str, str | None]] = []

    monkeypatch.setattr(
        "anvil.runner.auth_check",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("AWS auth should not run for Azure providers")
        ),
    )
    monkeypatch.setattr(
        "anvil.runner.AwsProvider.preflight_execution",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("AWS organization preflight should not run")
        ),
    )
    monkeypatch.setattr(
        "anvil.providers.azure.provider.AzureSessionFactory.list_subscriptions",
        lambda self, **kwargs: (
            subscription_calls.append(kwargs)
            or [
                AzureSubscription(subscription_id="sub-b"),
                AzureSubscription(subscription_id="sub-a"),
            ]
        ),
    )
    monkeypatch.setattr(
        "anvil.providers.azure.provider.AzureSessionFactory.create_session",
        lambda self, **kwargs: type(
            "_AzureSession", (), {"region_name": kwargs["location"]}
        )(),
    )

    target = TargetDescriptor(
        config_branch=ConfigBranch.TARGETS,
        name="azure-subscriptions",
        provider="azure",
        mode="tenant",
        include=None,
        tasks=[],
    )

    engine_result = run_multiple_targets(
        targets=[target],
        max_parallel_targets=1,
        cli_dry_run=None,
        cli_include=None,
        cli_exclude=None,
    )

    assert [result.id for result in engine_result.target_results[0].entities] == [
        "sub-a",
        "sub-b",
    ]
    assert subscription_calls == [
        {"tenant_id": None, "client_id": None, "client_secret": None}
    ]


def test_azure_subscription_discovery_errors_are_target_failures(monkeypatch):
    monkeypatch.setattr(
        "anvil.providers.azure.provider.AzureSessionFactory.list_subscriptions",
        lambda self, **kwargs: (_ for _ in ()).throw(
            RuntimeError("Azure provider could not discover subscriptions: denied")
        ),
    )

    target = TargetDescriptor(
        config_branch=ConfigBranch.TARGETS,
        name="azure-subscriptions",
        provider="azure",
        mode="tenant",
        include=None,
        provider_options={
            "tenant_id": "error-tenant",
            "client_id": "error-client",
            "client_secret": "error-secret",
        },
        tasks=[],
    )

    engine_result = run_multiple_targets(
        targets=[target],
        max_parallel_targets=1,
        cli_dry_run=None,
        cli_include=None,
        cli_exclude=None,
    )

    target_result = engine_result.target_results[0]
    assert (
        target_result.error == "Azure provider could not discover subscriptions: denied"
    )
    assert target_result.entities == []


def test_azure_subscription_discovery_rejects_cli_include_and_exclude(monkeypatch):
    def unexpected_list_subscriptions(self, **kwargs):
        raise AssertionError("subscription discovery should not run")

    monkeypatch.setattr(
        "anvil.providers.azure.provider.AzureSessionFactory.list_subscriptions",
        unexpected_list_subscriptions,
    )
    monkeypatch.setattr(
        "anvil.providers.azure.provider.AzureSessionFactory.create_session",
        lambda self, **kwargs: type(
            "_AzureSession", (), {"region_name": kwargs["location"]}
        )(),
    )

    target = TargetDescriptor(
        config_branch=ConfigBranch.TARGETS,
        name="azure-subscriptions",
        provider="azure",
        mode="tenant",
        include=None,
        provider_options={
            "tenant_id": "cli-filter-tenant",
            "client_id": "cli-filter-client",
            "client_secret": "cli-filter-secret",
        },
        tasks=[],
    )

    engine_result = run_multiple_targets(
        targets=[target],
        max_parallel_targets=1,
        cli_dry_run=None,
        cli_include=["sub-c", "sub-a"],
        cli_exclude=["sub-a"],
    )

    assert engine_result.target_results == []
    assert engine_result.auth_results[0].status is ExecutionStatus.ERROR
    assert engine_result.auth_results[0].source == "config"
    assert "include and exclude together" in engine_result.auth_results[0].message


def test_azure_subscription_discovery_plan_is_cached_across_targets(monkeypatch):
    subscription_calls = 0

    def fake_list_subscriptions(self, **kwargs):
        nonlocal subscription_calls
        subscription_calls += 1
        return [AzureSubscription(subscription_id="sub-a")]

    monkeypatch.setattr(
        "anvil.providers.azure.provider.AzureSessionFactory.list_subscriptions",
        fake_list_subscriptions,
    )
    monkeypatch.setattr(
        "anvil.providers.azure.provider.AzureSessionFactory.create_session",
        lambda self, **kwargs: type(
            "_AzureSession", (), {"region_name": kwargs["location"]}
        )(),
    )

    targets = [
        TargetDescriptor(
            config_branch=ConfigBranch.TARGETS,
            name="azure-subscriptions-a",
            provider="azure",
            mode="tenant",
            include=None,
            provider_options={
                "tenant_id": "cache-tenant",
                "client_id": "cache-client",
                "client_secret": "cache-secret",
            },
            tasks=[],
        ),
        TargetDescriptor(
            config_branch=ConfigBranch.TARGETS,
            name="azure-subscriptions-b",
            provider="azure",
            mode="tenant",
            include=None,
            provider_options={
                "tenant_id": "cache-tenant",
                "client_id": "cache-client",
                "client_secret": "cache-secret",
            },
            tasks=[],
        ),
    ]

    engine_result = run_multiple_targets(
        targets=targets,
        max_parallel_targets=2,
        cli_dry_run=None,
        cli_include=None,
        cli_exclude=None,
    )

    assert subscription_calls == 1
    assert [result.entities[0].id for result in engine_result.target_results] == [
        "sub-a",
        "sub-a",
    ]


def test_gcp_provider_options_reach_runtime_session_factory(monkeypatch):
    session_calls: list[dict[str, str | None]] = []

    monkeypatch.setattr(
        "anvil.providers.gcp.provider.GcpSessionFactory.create_session",
        lambda self, **kwargs: (
            session_calls.append(kwargs)
            or type("_GcpSession", (), {"region_name": kwargs["location"]})()
        ),
    )

    target = TargetDescriptor(
        config_branch=ConfigBranch.TARGETS,
        name="gcp-projects",
        provider="gcp",
        mode="projects",
        regions=["us-central1"],
        include=["project-a"],
        provider_options={
            "credentials_path": "credentials.json",
            "quota_project_id": "billing-project",
        },
        tasks=[],
    )

    engine_result = run_multiple_targets(
        targets=[target],
        max_parallel_targets=1,
        cli_dry_run=None,
        cli_include=None,
        cli_exclude=None,
    )

    assert not engine_result.target_results[0].has_failures
    assert session_calls == [
        {
            "project_id": "project-a",
            "location": "us-central1",
            "credentials_path": "credentials.json",
            "quota_project_id": "billing-project",
        }
    ]


def test_non_aws_universal_task_can_use_provider_neutral_kwargs(monkeypatch):
    seen: dict[str, object] = {}

    def neutral_task(
        *,
        provider,
        execution_target_id,
        execution_target_name,
        execution_target_type,
        region,
        task_context,
        session,
        dry_run,
        metadata,
        actions,
    ):
        seen.update(
            {
                "provider": provider,
                "execution_target_id": execution_target_id,
                "execution_target_name": execution_target_name,
                "execution_target_type": execution_target_type,
                "region": region,
                "context_provider": task_context.provider,
                "session_region": session.region_name,
                "dry_run": dry_run,
                "metadata": metadata,
                "actions": actions,
            }
        )
        actions.record("neutral task ran")
        return {"provider": provider, "target": execution_target_id}

    monkeypatch.setattr(
        "anvil.runner.resolve_tasks",
        lambda **kwargs: ResolvedExecution(
            ordered=[
                ResolvedTask("neutral", neutral_task, depends_on=[], optional=False)
            ],
            adjacency={},
        ),
    )
    monkeypatch.setattr(
        "anvil.providers.gcp.provider.GcpSessionFactory.create_session",
        lambda self, **kwargs: type(
            "_GcpSession", (), {"region_name": kwargs["location"]}
        )(),
    )

    target = TargetDescriptor(
        config_branch=ConfigBranch.TARGETS,
        name="gcp-projects",
        provider="gcp",
        mode="projects",
        regions=["us-central1"],
        include=["project-a"],
        metadata={"source": "neutral"},
        tasks=[{"name": "neutral"}],
    )

    engine_result = run_multiple_targets(
        targets=[target],
        max_parallel_targets=1,
        cli_dry_run=None,
        cli_include=None,
        cli_exclude=None,
    )

    entity_result = engine_result.target_results[0].entities[0]
    assert entity_result.status is ExecutionStatus.SUCCESS
    assert entity_result.tasks[0].result == {"provider": "gcp", "target": "project-a"}
    assert seen["provider"] == "gcp"
    assert seen["execution_target_id"] == "project-a"
    assert seen["execution_target_name"] == "project-a"
    assert seen["execution_target_type"] == "project"
    assert seen["region"] == "us-central1"
    assert seen["context_provider"] == "gcp"
    assert seen["session_region"] == "us-central1"
    assert seen["dry_run"] is False
    assert seen["metadata"] == {"source": "neutral"}
    assert seen["actions"].actions == ["neutral task ran"]


def test_non_aws_execution_uses_provider_resolved_target_locations(monkeypatch):
    seen: list[tuple[str, str]] = []

    def neutral_task(*, execution_target_id, region, **kwargs):
        seen.append((execution_target_id, region))
        return {"target": execution_target_id, "region": region}

    monkeypatch.setattr(
        "anvil.runner.resolve_tasks",
        lambda **kwargs: ResolvedExecution(
            ordered=[
                ResolvedTask("neutral", neutral_task, depends_on=[], optional=False)
            ],
            adjacency={},
        ),
    )
    monkeypatch.setattr(
        "anvil.providers.gcp.provider.GcpSessionFactory.create_session",
        lambda self, **kwargs: type(
            "_GcpSession", (), {"region_name": kwargs["location"]}
        )(),
    )
    monkeypatch.setattr(
        "anvil.providers.gcp.provider.GcpSessionFactory.list_regions",
        lambda self, *, project_id, **kwargs: (
            [
                ProviderRegion(name="us-east1", status="UP"),
                ProviderRegion(name="us-west1", status="UP"),
            ]
            if project_id == "project-a"
            else [ProviderRegion(name="europe-west1", status="UP")]
        ),
    )

    target = TargetDescriptor(
        config_branch=ConfigBranch.TARGETS,
        name="gcp-projects",
        provider="gcp",
        mode="projects",
        regions=["all"],
        include=["project-a", "project-b"],
        tasks=[{"name": "neutral"}],
    )

    engine_result = run_multiple_targets(
        targets=[target],
        max_parallel_targets=1,
        cli_dry_run=None,
        cli_include=None,
        cli_exclude=None,
    )

    assert not engine_result.target_results[0].has_failures
    assert sorted(seen) == [
        ("project-a", "us-east1"),
        ("project-a", "us-west1"),
        ("project-b", "europe-west1"),
    ]


def test_gcp_project_discovery_runs_without_aws_paths(monkeypatch):
    project_calls: list[dict[str, str | None]] = []

    monkeypatch.setattr(
        "anvil.runner.auth_check",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("AWS auth should not run for GCP providers")
        ),
    )
    monkeypatch.setattr(
        "anvil.runner.AwsProvider.preflight_execution",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("AWS organization preflight should not run")
        ),
    )
    monkeypatch.setattr(
        "anvil.providers.gcp.provider.GcpSessionFactory.list_projects",
        lambda self, **kwargs: (
            project_calls.append(kwargs)
            or [GcpProject(project_id="project-b"), GcpProject(project_id="project-a")]
        ),
    )
    monkeypatch.setattr(
        "anvil.providers.gcp.provider.GcpSessionFactory.create_session",
        lambda self, **kwargs: type(
            "_GcpSession", (), {"region_name": kwargs["location"]}
        )(),
    )

    target = TargetDescriptor(
        config_branch=ConfigBranch.TARGETS,
        name="gcp-projects",
        provider="gcp",
        mode="projects",
        include=None,
        tasks=[],
    )

    engine_result = run_multiple_targets(
        targets=[target],
        max_parallel_targets=1,
        cli_dry_run=None,
        cli_include=None,
        cli_exclude=None,
    )

    assert [result.id for result in engine_result.target_results[0].entities] == [
        "project-a",
        "project-b",
    ]
    assert project_calls == [{"credentials_path": None, "quota_project_id": None}]


def test_gcp_project_discovery_errors_are_target_failures(monkeypatch):
    monkeypatch.setattr(
        "anvil.providers.gcp.provider.GcpSessionFactory.list_projects",
        lambda self, **kwargs: (_ for _ in ()).throw(
            RuntimeError("GCP provider could not discover projects: denied")
        ),
    )

    target = TargetDescriptor(
        config_branch=ConfigBranch.TARGETS,
        name="gcp-projects",
        provider="gcp",
        mode="projects",
        include=None,
        provider_options={
            "credentials_path": "error-credentials.json",
            "quota_project_id": "error-billing-project",
        },
        tasks=[],
    )

    engine_result = run_multiple_targets(
        targets=[target],
        max_parallel_targets=1,
        cli_dry_run=None,
        cli_include=None,
        cli_exclude=None,
    )

    target_result = engine_result.target_results[0]
    assert target_result.error == "GCP provider could not discover projects: denied"
    assert target_result.entities == []


def test_gcp_project_discovery_rejects_cli_include_and_exclude(monkeypatch):
    def unexpected_list_projects(self, **kwargs):
        raise AssertionError("project discovery should not run")

    monkeypatch.setattr(
        "anvil.providers.gcp.provider.GcpSessionFactory.list_projects",
        unexpected_list_projects,
    )
    monkeypatch.setattr(
        "anvil.providers.gcp.provider.GcpSessionFactory.create_session",
        lambda self, **kwargs: type(
            "_GcpSession", (), {"region_name": kwargs["location"]}
        )(),
    )

    target = TargetDescriptor(
        config_branch=ConfigBranch.TARGETS,
        name="gcp-projects",
        provider="gcp",
        mode="projects",
        include=None,
        tasks=[],
    )

    engine_result = run_multiple_targets(
        targets=[target],
        max_parallel_targets=1,
        cli_dry_run=None,
        cli_include=["project-c", "project-a"],
        cli_exclude=["project-a"],
    )

    assert engine_result.target_results == []
    assert engine_result.auth_results[0].status is ExecutionStatus.ERROR
    assert engine_result.auth_results[0].source == "config"
    assert "include and exclude together" in engine_result.auth_results[0].message


def test_gcp_project_discovery_plan_is_cached_across_targets(monkeypatch):
    project_calls = 0

    def fake_list_projects(self, **kwargs):
        nonlocal project_calls
        project_calls += 1
        return [GcpProject(project_id="project-a")]

    monkeypatch.setattr(
        "anvil.providers.gcp.provider.GcpSessionFactory.list_projects",
        fake_list_projects,
    )
    monkeypatch.setattr(
        "anvil.providers.gcp.provider.GcpSessionFactory.create_session",
        lambda self, **kwargs: type(
            "_GcpSession", (), {"region_name": kwargs["location"]}
        )(),
    )

    targets = [
        TargetDescriptor(
            config_branch=ConfigBranch.TARGETS,
            name="gcp-projects-a",
            provider="gcp",
            mode="projects",
            include=None,
            provider_options={
                "credentials_path": "cache-credentials.json",
                "quota_project_id": "cache-billing-project",
            },
            tasks=[],
        ),
        TargetDescriptor(
            config_branch=ConfigBranch.TARGETS,
            name="gcp-projects-b",
            provider="gcp",
            mode="projects",
            include=None,
            provider_options={
                "credentials_path": "cache-credentials.json",
                "quota_project_id": "cache-billing-project",
            },
            tasks=[],
        ),
    ]

    engine_result = run_multiple_targets(
        targets=targets,
        max_parallel_targets=2,
        cli_dry_run=None,
        cli_include=None,
        cli_exclude=None,
    )

    assert project_calls == 1
    assert [result.entities[0].id for result in engine_result.target_results] == [
        "project-a",
        "project-a",
    ]


def test_non_aws_fail_fast_cancels_pending_execution_targets(monkeypatch):
    execution_started = threading.Event()
    calls: list[str] = []

    def fake_execute_provider_execution_target(**kwargs):
        execution_target = kwargs["execution_target"]
        calls.append(execution_target.id)
        if execution_target.id == "first":
            execution_started.set()
            return EntityResult(
                id="first",
                name="first",
                type="account",
                status=ExecutionStatus.ERROR,
                started_at="start",
                ended_at="end",
                duration_seconds=0.0,
                tasks=[],
                error="failed",
            )
        raise AssertionError("pending provider targets should be cancelled")

    monkeypatch.setattr(
        "anvil.runner._execute_provider_execution_target",
        fake_execute_provider_execution_target,
    )

    target = TargetDescriptor(
        config_branch=ConfigBranch.TARGETS,
        name="azure-subscriptions",
        provider="azure",
        mode="subscriptions",
        include=["first", "second"],
        tasks=[],
        fail_fast=True,
        max_workers=1,
    )

    engine_result = run_multiple_targets(
        targets=[target],
        max_parallel_targets=1,
        cli_dry_run=None,
        cli_include=None,
        cli_exclude=None,
    )

    assert execution_started.is_set()
    assert calls == ["first"]
    assert engine_result.target_results[0].entities[0].status.is_error


@pytest.mark.parametrize(
    ("provider", "mode", "include"),
    [
        ("aws", "accounts", "111111111111"),
        ("azure", "subscriptions", "00000000-0000-0000-0000-000000000000"),
        ("gcp", "projects", "project-a"),
    ],
)
def test_explicit_modes_reject_cli_exclude_before_execution(
    monkeypatch, provider, mode, include
):
    executed = False

    def fail_execute_provider_execution_target(**kwargs):
        nonlocal executed
        executed = True
        raise AssertionError("explicit-mode exclude should stop before execution")

    monkeypatch.setattr(
        "anvil.runner._run_cached_auth_check_for_target",
        lambda target, auth_cache: AuthResult(
            target_name=target.name,
            status=ExecutionStatus.SUCCESS,
            source="test",
            started_at="start",
            ended_at="end",
            duration_seconds=0.0,
            message="ok",
        ),
    )
    monkeypatch.setattr(
        "anvil.runner._execute_provider_execution_target",
        fail_execute_provider_execution_target,
    )

    target = _load_targets(
        {
            "schema_version": 2,
            "targets": [
                {
                    "name": f"{provider}-{mode}",
                    "provider": {"name": provider, "mode": mode, "options": {}},
                    "regions": [
                        "us-east-1"
                        if provider == "aws"
                        else "eastus"
                        if provider == "azure"
                        else "global"
                    ],
                    "include": [include],
                    "tasks": [{"name": "noop"}],
                }
            ],
        }
    )[0]

    engine_result = run_multiple_targets(
        targets=[target],
        max_parallel_targets=1,
        cli_dry_run=None,
        cli_include=None,
        cli_exclude=[include],
    )

    assert not executed
    assert engine_result.auth_results[0].status is ExecutionStatus.ERROR
    assert "does not allow exclude" in (engine_result.auth_results[0].message or "")


def test_discovery_modes_reject_effective_include_and_exclude(monkeypatch):
    monkeypatch.setattr(
        "anvil.runner._run_cached_auth_check_for_target",
        lambda target, auth_cache: AuthResult(
            target_name=target.name,
            status=ExecutionStatus.SUCCESS,
            source="test",
            started_at="start",
            ended_at="end",
            duration_seconds=0.0,
            message="ok",
        ),
    )

    target = _load_targets(
        {
            "schema_version": 2,
            "targets": [
                {
                    "name": "aws-org",
                    "provider": {
                        "name": "aws",
                        "mode": "organization",
                        "options": {"profile": "shared"},
                    },
                    "regions": ["us-east-1"],
                    "include": ["111111111111"],
                    "tasks": [{"name": "noop"}],
                }
            ],
        }
    )[0]

    engine_result = run_multiple_targets(
        targets=[target],
        max_parallel_targets=1,
        cli_dry_run=None,
        cli_include=None,
        cli_exclude=["222222222222"],
    )

    assert engine_result.auth_results[0].status is ExecutionStatus.ERROR
    assert "include and exclude together" in (
        engine_result.auth_results[0].message or ""
    )


def test_run_multiple_targets_reuses_same_profile_auth_during_preparation(monkeypatch):
    auth_check_calls: list[str] = []

    monkeypatch.setattr(
        "anvil.runner.infer_auth_source", lambda profile: AuthSource.PROFILE_STATIC
    )

    def fake_auth_check(**kwargs):
        auth_check_calls.append(kwargs["target_name"])
        return AuthResult(
            target_name=kwargs["target_name"],
            status=ExecutionStatus.SUCCESS,
            source=kwargs["auth_source"].value,
            started_at="start",
            ended_at="end",
            duration_seconds=0.0,
            message="ok",
        )

    monkeypatch.setattr("anvil.runner.auth_check", fake_auth_check)
    monkeypatch.setattr("anvil.runner.resolve_tasks", _empty_resolved_execution)

    def fake_preflight_execution(self, **kwargs):
        data = SimpleNamespace(
            session_factory=kwargs["session_factory"],
            base_session=object(),
            organization_id="o-shared",
            management_account_id="999999999999",
            base_session_account_id="999999999999",
            discovered_accounts={
                "999999999999": {
                    "account_number": "999999999999",
                    "account_alias": "management",
                }
            },
            region_statuses={"us-east-1": "ENABLED_BY_DEFAULT"},
        )
        return SimpleNamespace(data=data, exclusive_execution_key="o-shared")

    monkeypatch.setattr(
        "anvil.runner.AwsProvider.preflight_execution",
        fake_preflight_execution,
    )
    monkeypatch.setattr(
        "anvil.runner._execute_provider_targets",
        lambda **kwargs: TargetResult.create(
            config_branch=kwargs["target"].config_branch,
            target_name=kwargs["target"].name,
            dry_run=kwargs["context"].dry_run,
            entities=[],
        ),
    )

    targets = [
        TargetDescriptor(
            config_branch=ConfigBranch.TARGETS, name="org-a", profile="shared", tasks=[]
        ),
        TargetDescriptor(
            config_branch=ConfigBranch.TARGETS, name="org-b", profile="shared", tasks=[]
        ),
    ]

    engine_result = run_multiple_targets(
        targets=targets,
        max_parallel_targets=1,
        cli_dry_run=None,
        cli_include=None,
        cli_exclude=None,
    )

    assert auth_check_calls == ["org-a"]
    assert [result.target_name for result in engine_result.auth_results] == [
        "org-a",
        "org-b",
    ]


def test_prepare_target_reuses_same_org_discovery_cache(monkeypatch):
    discovered_accounts = {
        "111111111111": {"account_number": "111111111111", "account_alias": "acct-a"}
    }
    region_statuses = {"us-east-1": "ENABLED_BY_DEFAULT", "us-west-2": "ENABLED"}
    call_counts = {"accounts": 0, "regions": 0}

    monkeypatch.setattr(
        "anvil.runner._run_cached_auth_check_for_target",
        lambda target, auth_cache: AuthResult(
            target_name=target.name,
            status=ExecutionStatus.SUCCESS,
            source="test",
            started_at="start",
            ended_at="end",
            duration_seconds=0.0,
            message="ok",
        ),
    )
    monkeypatch.setattr("anvil.runner.resolve_tasks", _empty_resolved_execution)

    class FakeSessionFactory:
        def create_base_session(self, **kwargs):
            return type("_BaseSession", (), {"profile_name": kwargs["profile_name"]})()

    monkeypatch.setattr("anvil.runner.SessionFactory", FakeSessionFactory)
    monkeypatch.setattr(
        "anvil.runner.OrganizationResolver.describe_organization",
        staticmethod(lambda session: ("o-shared", "999999999999")),
    )
    monkeypatch.setattr(
        "anvil.runner.OrganizationResolver.describe_base_session_account",
        staticmethod(lambda session: "999999999999"),
    )

    def fake_discover_accounts(session):
        call_counts["accounts"] += 1
        return discovered_accounts

    def fake_discover_regions(session):
        call_counts["regions"] += 1
        return region_statuses

    monkeypatch.setattr(
        "anvil.runner.OrganizationResolver.discover_accounts",
        staticmethod(fake_discover_accounts),
    )
    monkeypatch.setattr(
        "anvil.runner.OrganizationResolver.discover_region_statuses",
        staticmethod(fake_discover_regions),
    )

    organization_cache = OrganizationRunCache()
    auth_cache = AuthCheckCache()
    target_a = TargetDescriptor(
        config_branch=ConfigBranch.TARGETS,
        name="org-a",
        profile="shared",
        regions=["us-east-1"],
        tasks=[],
    )
    target_b = TargetDescriptor(
        config_branch=ConfigBranch.TARGETS,
        name="org-b",
        profile="shared",
        regions=["us-west-2"],
        tasks=[],
    )

    prepared_a = prepare_target(
        index=0,
        target=target_a,
        cli_dry_run=None,
        cli_include=None,
        cli_exclude=None,
        organization_cache=organization_cache,
        auth_cache=auth_cache,
    )
    prepared_b = prepare_target(
        index=1,
        target=target_b,
        cli_dry_run=None,
        cli_include=None,
        cli_exclude=None,
        organization_cache=organization_cache,
        auth_cache=auth_cache,
    )

    assert call_counts == {"accounts": 1, "regions": 1}
    assert prepared_a.base_session is not None
    assert prepared_b.base_session is not None
    assert prepared_a.discovered_accounts == discovered_accounts
    assert prepared_b.discovered_accounts == discovered_accounts
    assert prepared_a.region_statuses == region_statuses
    assert prepared_b.region_statuses == region_statuses


def test_run_multiple_targets_reuses_same_org_discovery_cache(monkeypatch):
    discovered_accounts = {
        "111111111111": {"account_number": "111111111111", "account_alias": "acct-a"}
    }
    region_statuses = {"us-east-1": "ENABLED_BY_DEFAULT", "us-west-2": "ENABLED"}
    call_counts = {"accounts": 0, "regions": 0, "execute": 0}

    monkeypatch.setattr(
        "anvil.runner._run_cached_auth_check_for_target",
        lambda target, auth_cache: AuthResult(
            target_name=target.name,
            status=ExecutionStatus.SUCCESS,
            source="test",
            started_at="start",
            ended_at="end",
            duration_seconds=0.0,
            message="ok",
        ),
    )
    monkeypatch.setattr("anvil.runner.resolve_tasks", _empty_resolved_execution)

    class FakeSessionFactory:
        def create_base_session(self, **kwargs):
            return type("_BaseSession", (), {"profile_name": kwargs["profile_name"]})()

    monkeypatch.setattr("anvil.runner.SessionFactory", FakeSessionFactory)
    monkeypatch.setattr(
        "anvil.runner.OrganizationResolver.describe_organization",
        staticmethod(lambda session: ("o-shared", "999999999999")),
    )
    monkeypatch.setattr(
        "anvil.runner.OrganizationResolver.describe_base_session_account",
        staticmethod(lambda session: "999999999999"),
    )

    def fake_discover_accounts(session):
        call_counts["accounts"] += 1
        return discovered_accounts

    def fake_discover_regions(session):
        call_counts["regions"] += 1
        return region_statuses

    def fake_execute_provider_targets(**kwargs):
        call_counts["execute"] += 1
        return TargetResult.create(
            config_branch=kwargs["target"].config_branch,
            target_name=kwargs["target"].name,
            dry_run=kwargs["context"].dry_run,
            entities=[],
        )

    monkeypatch.setattr(
        "anvil.runner.OrganizationResolver.discover_accounts",
        staticmethod(fake_discover_accounts),
    )
    monkeypatch.setattr(
        "anvil.runner.OrganizationResolver.discover_region_statuses",
        staticmethod(fake_discover_regions),
    )
    monkeypatch.setattr(
        "anvil.runner._execute_provider_targets", fake_execute_provider_targets
    )

    targets = _load_targets(
        {
            "schema_version": 2,
            "max_parallel_targets": 2,
            "targets": [
                {
                    "name": "org-a",
                    "provider": {
                        "name": "aws",
                        "mode": "organization",
                        "options": {"profile": "shared"},
                    },
                    "regions": ["us-east-1"],
                    "tasks": [{"name": "noop"}],
                },
                {
                    "name": "org-b",
                    "provider": {
                        "name": "aws",
                        "mode": "organization",
                        "options": {"profile": "shared"},
                    },
                    "regions": ["us-west-2"],
                    "tasks": [{"name": "noop"}],
                },
            ],
        }
    )

    engine_result = run_multiple_targets(
        targets=targets,
        max_parallel_targets=2,
        cli_dry_run=None,
        cli_include=None,
        cli_exclude=None,
    )

    assert call_counts == {"accounts": 1, "regions": 1, "execute": 2}
    assert [result.target_name for result in engine_result.target_results] == [
        "org-a",
        "org-b",
    ]


def test_run_multiple_targets_preserves_aws_account_access_strategies(monkeypatch):
    observed_accounts: dict[str, list[tuple[str, str]]] = {}

    monkeypatch.setattr(
        "anvil.runner._run_cached_auth_check_for_target",
        lambda target, auth_cache: AuthResult(
            target_name=target.name,
            status=ExecutionStatus.SUCCESS,
            source="test",
            started_at="start",
            ended_at="end",
            duration_seconds=0.0,
            message="ok",
        ),
    )
    monkeypatch.setattr("anvil.runner.resolve_tasks", _empty_resolved_execution)

    class FakeSessionFactory:
        def create_base_session(self, **kwargs):
            return object()

    def fake_execute_provider_targets(**kwargs):
        observed_accounts[kwargs["target"].name] = [
            (execution_target.id, execution_target.provider_data.access_strategy.value)
            for execution_target in kwargs["execution_targets"]
        ]
        return TargetResult.create(
            config_branch=kwargs["target"].config_branch,
            target_name=kwargs["target"].name,
            dry_run=kwargs["context"].dry_run,
            entities=[],
        )

    monkeypatch.setattr("anvil.runner.SessionFactory", FakeSessionFactory)
    monkeypatch.setattr(
        "anvil.runner._execute_provider_targets", fake_execute_provider_targets
    )

    targets = _load_targets(
        {
            "schema_version": 2,
            "targets": [
                {
                    "name": "direct-account",
                    "provider": {"name": "aws", "mode": "accounts", "options": {}},
                    "regions": ["us-east-1"],
                    "include": ["111111111111"],
                    "tasks": [{"name": "noop"}],
                },
                {
                    "name": "assume-role-accounts",
                    "provider": {
                        "name": "aws",
                        "mode": "accounts",
                        "options": {"role_name": "AuditRole"},
                    },
                    "regions": ["us-east-1"],
                    "include": ["222222222222", "333333333333"],
                    "tasks": [{"name": "noop"}],
                },
            ],
        }
    )

    run_multiple_targets(
        targets=targets,
        max_parallel_targets=1,
        cli_dry_run=None,
        cli_include=None,
        cli_exclude=None,
    )

    assert observed_accounts["direct-account"] == [("111111111111", "direct_profile")]
    assert observed_accounts["assume-role-accounts"] == [
        ("222222222222", "assume_role"),
        ("333333333333", "assume_role"),
    ]


def test_prepare_target_keeps_base_session_account_out_of_org_cache(monkeypatch):
    discovered_accounts = {
        "111111111111": {"account_number": "111111111111", "account_alias": "payer"},
        "222222222222": {
            "account_number": "222222222222",
            "account_alias": "delegated-admin",
        },
    }
    region_statuses = {"us-east-1": "ENABLED_BY_DEFAULT"}
    call_counts = {"accounts": 0, "regions": 0}
    base_account_ids = {
        "management-profile": "111111111111",
        "delegated-profile": "222222222222",
    }

    monkeypatch.setattr(
        "anvil.runner._run_cached_auth_check_for_target",
        lambda target, auth_cache: AuthResult(
            target_name=target.name,
            status=ExecutionStatus.SUCCESS,
            source="test",
            started_at="start",
            ended_at="end",
            duration_seconds=0.0,
            message="ok",
        ),
    )
    monkeypatch.setattr("anvil.runner.resolve_tasks", _empty_resolved_execution)

    class FakeSessionFactory:
        def create_base_session(self, **kwargs):
            return type("_BaseSession", (), {"profile_name": kwargs["profile_name"]})()

    monkeypatch.setattr("anvil.runner.SessionFactory", FakeSessionFactory)
    monkeypatch.setattr(
        "anvil.runner.OrganizationResolver.describe_organization",
        staticmethod(lambda session: ("o-shared", "111111111111")),
    )
    monkeypatch.setattr(
        "anvil.runner.OrganizationResolver.describe_base_session_account",
        staticmethod(lambda session: base_account_ids[session.profile_name]),
    )

    def fake_discover_accounts(session):
        call_counts["accounts"] += 1
        return discovered_accounts

    def fake_discover_regions(session):
        call_counts["regions"] += 1
        return region_statuses

    monkeypatch.setattr(
        "anvil.runner.OrganizationResolver.discover_accounts",
        staticmethod(fake_discover_accounts),
    )
    monkeypatch.setattr(
        "anvil.runner.OrganizationResolver.discover_region_statuses",
        staticmethod(fake_discover_regions),
    )

    organization_cache = OrganizationRunCache()
    auth_cache = AuthCheckCache()

    prepared_management = prepare_target(
        index=0,
        target=TargetDescriptor(
            config_branch=ConfigBranch.TARGETS,
            name="management-auth",
            profile="management-profile",
            tasks=[],
        ),
        cli_dry_run=None,
        cli_include=None,
        cli_exclude=None,
        organization_cache=organization_cache,
        auth_cache=auth_cache,
    )
    prepared_delegated = prepare_target(
        index=1,
        target=TargetDescriptor(
            config_branch=ConfigBranch.TARGETS,
            name="delegated-auth",
            profile="delegated-profile",
            tasks=[],
        ),
        cli_dry_run=None,
        cli_include=None,
        cli_exclude=None,
        organization_cache=organization_cache,
        auth_cache=auth_cache,
    )

    assert call_counts == {"accounts": 1, "regions": 1}
    assert prepared_management.management_account_id == "111111111111"
    assert prepared_management.base_session_account_id == "111111111111"
    assert prepared_delegated.management_account_id == "111111111111"
    assert prepared_delegated.base_session_account_id == "222222222222"
    assert prepared_management.discovered_accounts == discovered_accounts
    assert prepared_delegated.discovered_accounts == discovered_accounts


def test_prepare_target_uses_bootstrap_region_for_region_selector(monkeypatch):
    created_session_regions: list[str] = []

    monkeypatch.setattr(
        "anvil.runner._run_cached_auth_check_for_target",
        lambda target, auth_cache: AuthResult(
            target_name=target.name,
            status=ExecutionStatus.SUCCESS,
            source="test",
            started_at="start",
            ended_at="end",
            duration_seconds=0.0,
            message="ok",
        ),
    )
    monkeypatch.setattr("anvil.runner.resolve_tasks", _empty_resolved_execution)

    class FakeSessionFactory:
        def create_base_session(self, **kwargs):
            created_session_regions.append(kwargs["region_name"])
            return type("_BaseSession", (), {"profile_name": kwargs["profile_name"]})()

    monkeypatch.setattr("anvil.runner.SessionFactory", FakeSessionFactory)
    monkeypatch.setattr(
        "anvil.runner.OrganizationResolver.describe_organization",
        staticmethod(lambda session: ("o-shared", "999999999999")),
    )
    monkeypatch.setattr(
        "anvil.runner.OrganizationResolver.describe_base_session_account",
        staticmethod(lambda session: "999999999999"),
    )
    monkeypatch.setattr(
        "anvil.runner.OrganizationResolver.discover_accounts",
        staticmethod(lambda session: {}),
    )
    monkeypatch.setattr(
        "anvil.runner.OrganizationResolver.discover_region_statuses",
        staticmethod(lambda session: {"us-east-1": "ENABLED_BY_DEFAULT"}),
    )

    prepare_target(
        index=0,
        target=TargetDescriptor(
            config_branch=ConfigBranch.TARGETS,
            name="org-a",
            profile="shared",
            regions=["all"],
            tasks=[],
        ),
        cli_dry_run=None,
        cli_include=None,
        cli_exclude=None,
        organization_cache=OrganizationRunCache(),
        auth_cache=AuthCheckCache(),
    )

    assert created_session_regions == ["us-east-1"]


def test_organization_run_cache_single_flights_concurrent_discovery():
    entry = OrganizationRunCacheEntry(
        management_account_id="999999999999",
        discovered_accounts={
            "111111111111": {
                "account_number": "111111111111",
                "account_alias": "acct-a",
            }
        },
        region_statuses={"us-east-1": "ENABLED_BY_DEFAULT"},
    )
    cache = OrganizationRunCache()
    discovery_started = threading.Event()
    waiter_started = threading.Event()
    release_discovery = threading.Event()
    discover_calls = 0
    discover_lock = threading.Lock()

    def discover():
        nonlocal discover_calls
        with discover_lock:
            discover_calls += 1

        discovery_started.set()
        assert waiter_started.wait(timeout=1)
        assert release_discovery.wait(timeout=1)
        return entry

    def owner_lookup():
        return cache.get_or_discover(organization_id="o-shared", discover=discover)

    def waiter_lookup():
        waiter_started.set()
        return cache.get_or_discover(organization_id="o-shared", discover=discover)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(owner_lookup)
        assert discovery_started.wait(timeout=1)

        second = executor.submit(waiter_lookup)
        assert waiter_started.wait(timeout=1)
        release_discovery.set()

        first_lookup = first.result(timeout=1)
        second_lookup = second.result(timeout=1)

    assert discover_calls == 1
    assert first_lookup.entry is entry
    assert first_lookup.hit is False
    assert first_lookup.waited is False
    assert second_lookup.entry is entry
    assert second_lookup.hit is True
    assert second_lookup.waited is True


def test_organization_run_cache_releases_waiters_after_discovery_error():
    cache = OrganizationRunCache()
    discovery_started = threading.Event()
    waiter_started = threading.Event()
    release_discovery = threading.Event()

    def fail_discovery():
        discovery_started.set()
        assert waiter_started.wait(timeout=1)
        assert release_discovery.wait(timeout=1)
        raise RuntimeError("discovery failed")

    def owner_lookup():
        return cache.get_or_discover(
            organization_id="o-shared", discover=fail_discovery
        )

    def waiter_lookup():
        waiter_started.set()
        return cache.get_or_discover(
            organization_id="o-shared", discover=fail_discovery
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(owner_lookup)
        assert discovery_started.wait(timeout=1)

        second = executor.submit(waiter_lookup)
        assert waiter_started.wait(timeout=1)
        release_discovery.set()

        with pytest.raises(RuntimeError, match="discovery failed"):
            second.result(timeout=1)
        with pytest.raises(RuntimeError, match="discovery failed"):
            first.result(timeout=1)

    entry = OrganizationRunCacheEntry(
        management_account_id="999999999999",
        discovered_accounts={},
        region_statuses={"us-east-1": "ENABLED_BY_DEFAULT"},
    )
    retry_lookup = cache.get_or_discover(
        organization_id="o-shared", discover=lambda: entry
    )

    assert retry_lookup.entry is entry
    assert retry_lookup.hit is False
    assert retry_lookup.waited is False


def test_run_prepared_target_uses_cached_org_preflight(monkeypatch):
    target = TargetDescriptor(
        config_branch=ConfigBranch.TARGETS,
        name="org-a",
        profile="shared",
        regions=["us-east-1"],
        tasks=[],
        role_name="TestRole",
    )
    context = ExecutionContext(
        regions=["us-east-1"],
        role_name="TestRole",
        dry_run=False,
        tasks=[],
        metadata={},
    )
    base_session = type("_BaseSession", (), {"profile_name": "shared"})()
    discovered_accounts = {
        "111111111111": {"account_number": "111111111111", "account_alias": "acct-a"}
    }
    region_statuses = {"us-east-1": "ENABLED_BY_DEFAULT"}

    class FakeSessionFactory:
        def create_base_session(self, **kwargs):
            raise AssertionError("execution should reuse the preflight base session")

    monkeypatch.setattr(
        "anvil.organization.OrganizationResolver.describe_organization",
        staticmethod(
            lambda session: (_ for _ in ()).throw(
                AssertionError("execution should not rediscover organization identity")
            )
        ),
    )
    monkeypatch.setattr(
        "anvil.organization.OrganizationResolver.discover_accounts",
        staticmethod(
            lambda session: (_ for _ in ()).throw(
                AssertionError("execution should not rediscover accounts")
            )
        ),
    )
    monkeypatch.setattr(
        "anvil.organization.OrganizationResolver.discover_region_statuses",
        staticmethod(
            lambda session: (_ for _ in ()).throw(
                AssertionError("execution should not rediscover region statuses")
            )
        ),
    )

    monkeypatch.setattr(
        "anvil.runner._execute_provider_targets",
        lambda **kwargs: TargetResult.create(
            config_branch=kwargs["target"].config_branch,
            target_name=kwargs["target"].name,
            dry_run=kwargs["context"].dry_run,
            entities=[],
        ),
    )

    prepared_target = PreparedTarget(
        index=0,
        effective_target=target,
        auth_result=AuthResult(
            target_name=target.name,
            status=ExecutionStatus.SUCCESS,
            source="test",
            started_at="start",
            ended_at="end",
            duration_seconds=0.0,
            message="ok",
        ),
        context=context,
        provider_preflight=SimpleNamespace(
            session_factory=FakeSessionFactory(),
            base_session=base_session,
            organization_id="o-shared",
            management_account_id="999999999999",
            base_session_account_id="999999999999",
            discovered_accounts=discovered_accounts,
            region_statuses=region_statuses,
        ),
        exclusive_execution_key="o-shared",
        session_factory=FakeSessionFactory(),
        base_session=base_session,
        organization_id="o-shared",
        management_account_id="999999999999",
        base_session_account_id="999999999999",
        discovered_accounts=discovered_accounts,
        region_statuses=region_statuses,
    )

    outcome = run_prepared_target(prepared_target=prepared_target)

    assert outcome.target_result.target_name == "org-a"
    assert outcome.cancelled is False


def test_run_prepared_target_converts_aws_value_error(monkeypatch):
    target = TargetDescriptor(
        config_branch=ConfigBranch.TARGETS, name="group-a", include=["111111111111"]
    )
    context = ExecutionContext(
        regions=["us-east-1"], role_name=None, dry_run=False, tasks=[], metadata={}
    )

    monkeypatch.setattr(
        "anvil.runner.AwsProvider.resolve_execution_targets",
        lambda self, **kwargs: (_ for _ in ()).throw(ValueError("bad aws config")),
    )

    prepared_target = PreparedTarget(
        index=0,
        effective_target=target,
        auth_result=AuthResult(
            target_name=target.name,
            status=ExecutionStatus.SUCCESS,
            source="test",
            started_at="start",
            ended_at="end",
            duration_seconds=0.0,
            message="ok",
        ),
        context=context,
    )

    outcome = run_prepared_target(prepared_target=prepared_target)

    assert outcome.target_result.error == "bad aws config"
    assert outcome.target_result.entities == []


def test_run_prepared_target_does_not_swallow_unexpected_aws_exception(monkeypatch):
    target = TargetDescriptor(
        config_branch=ConfigBranch.TARGETS, name="group-a", include=["111111111111"]
    )
    context = ExecutionContext(
        regions=["us-east-1"], role_name=None, dry_run=False, tasks=[], metadata={}
    )

    monkeypatch.setattr(
        "anvil.runner.AwsProvider.resolve_execution_targets",
        lambda self, **kwargs: (_ for _ in ()).throw(
            RuntimeError("unexpected aws failure")
        ),
    )

    prepared_target = PreparedTarget(
        index=0,
        effective_target=target,
        auth_result=AuthResult(
            target_name=target.name,
            status=ExecutionStatus.SUCCESS,
            source="test",
            started_at="start",
            ended_at="end",
            duration_seconds=0.0,
            message="ok",
        ),
        context=context,
    )

    with pytest.raises(RuntimeError, match="unexpected aws failure"):
        run_prepared_target(prepared_target=prepared_target)


def test_prepare_target_carries_max_parallel_regions_into_context(monkeypatch):
    monkeypatch.setattr(
        "anvil.runner._run_cached_auth_check_for_target",
        lambda target, auth_cache: AuthResult(
            target_name=target.name,
            status=ExecutionStatus.SUCCESS,
            source="test",
            started_at="start",
            ended_at="end",
            duration_seconds=0.0,
            message="ok",
        ),
    )
    monkeypatch.setattr("anvil.runner.resolve_tasks", _empty_resolved_execution)

    target = TargetDescriptor(
        config_branch=ConfigBranch.TARGETS,
        name="group-a",
        include=["111111111111"],
        max_parallel_regions=3,
    )

    prepared = prepare_target(
        index=0,
        target=target,
        cli_dry_run=None,
        cli_include=None,
        cli_exclude=None,
        organization_cache=OrganizationRunCache(),
        auth_cache=AuthCheckCache(),
    )

    assert prepared.context is not None
    assert prepared.context.max_parallel_regions == 3
