from anvil.descriptors import ConfigBranch, TargetDescriptor
from anvil.execution_context import ExecutionContext
from anvil.results import AuthResult, ExecutionStatus
from anvil.runner import (
    OrganizationRunCache,
    PreparedTarget,
    prepare_target,
    run_multiple_targets,
    run_prepared_target,
)
from anvil.task_loader import ResolvedExecution


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
        config_branch=ConfigBranch.ORGANIZATIONS,
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


def test_prepare_target_reuses_same_org_discovery_cache(monkeypatch):
    discovered_accounts = {
        "111111111111": {"account_number": "111111111111", "account_alias": "acct-a"}
    }
    enabled_regions = ["us-east-1", "us-west-2"]
    call_counts = {"accounts": 0, "regions": 0}

    monkeypatch.setattr(
        "anvil.runner._run_auth_check_for_target",
        lambda target: AuthResult(
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
        "anvil.runner.resolve_tasks",
        lambda task_specs: ResolvedExecution(ordered=[], adjacency={}),
    )

    class FakeSessionFactory:
        def create_base_session(self, **kwargs):
            return type("_BaseSession", (), {"profile_name": kwargs["profile_name"]})()

    monkeypatch.setattr("anvil.runner.SessionFactory", FakeSessionFactory)
    monkeypatch.setattr(
        "anvil.runner.OrganizationResolver.describe_organization",
        staticmethod(lambda session: ("o-shared", "999999999999")),
    )

    def fake_discover_accounts(session):
        call_counts["accounts"] += 1
        return discovered_accounts

    def fake_discover_regions(session):
        call_counts["regions"] += 1
        return enabled_regions

    monkeypatch.setattr(
        "anvil.runner.OrganizationResolver.discover_accounts",
        staticmethod(fake_discover_accounts),
    )
    monkeypatch.setattr(
        "anvil.runner.OrganizationResolver.discover_enabled_regions",
        staticmethod(fake_discover_regions),
    )

    organization_cache = OrganizationRunCache()
    target_a = TargetDescriptor(
        config_branch=ConfigBranch.ORGANIZATIONS,
        name="org-a",
        profile="shared",
        regions=["us-east-1"],
        tasks=[],
    )
    target_b = TargetDescriptor(
        config_branch=ConfigBranch.ORGANIZATIONS,
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
    )
    prepared_b = prepare_target(
        index=1,
        target=target_b,
        cli_dry_run=None,
        cli_include=None,
        cli_exclude=None,
        organization_cache=organization_cache,
    )

    assert call_counts == {"accounts": 1, "regions": 1}
    assert prepared_a.base_session is not None
    assert prepared_b.base_session is not None
    assert prepared_a.discovered_accounts == discovered_accounts
    assert prepared_b.discovered_accounts == discovered_accounts
    assert prepared_a.enabled_regions == enabled_regions
    assert prepared_b.enabled_regions == enabled_regions


def test_run_prepared_target_uses_cached_org_preflight(monkeypatch):
    target = TargetDescriptor(
        config_branch=ConfigBranch.ORGANIZATIONS,
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
    enabled_regions = ["us-east-1"]

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
        "anvil.organization.OrganizationResolver.discover_enabled_regions",
        staticmethod(
            lambda session: (_ for _ in ()).throw(
                AssertionError("execution should not rediscover enabled regions")
            )
        ),
    )

    monkeypatch.setattr(
        "anvil.runner.execute_accounts",
        lambda **kwargs: __import__(
            "anvil.results", fromlist=["TargetResult"]
        ).TargetResult.create(
            config_branch=kwargs["config_branch"],
            target_name=kwargs["name"],
            dry_run=kwargs["context"].dry_run,
            account_results=[],
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
        session_factory=FakeSessionFactory(),
        base_session=base_session,
        organization_id="o-shared",
        management_account_id="999999999999",
        discovered_accounts=discovered_accounts,
        enabled_regions=enabled_regions,
    )

    outcome = run_prepared_target(prepared_target=prepared_target)

    assert outcome.target_result.target_name == "org-a"
    assert outcome.cancelled is False


def test_prepare_target_carries_max_parallel_regions_into_context(monkeypatch):
    monkeypatch.setattr(
        "anvil.runner._run_auth_check_for_target",
        lambda target: AuthResult(
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
        "anvil.runner.resolve_tasks",
        lambda task_specs: ResolvedExecution(ordered=[], adjacency={}),
    )

    target = TargetDescriptor(
        config_branch=ConfigBranch.ACCOUNTS,
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
    )

    assert prepared.context is not None
    assert prepared.context.max_parallel_regions == 3
