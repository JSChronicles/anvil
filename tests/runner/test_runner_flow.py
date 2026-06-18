import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from anvil.auth import AuthSource
from anvil.descriptors import ConfigBranch, TargetDescriptor
from anvil.execution_context import ExecutionContext
from anvil.results import AuthResult, ExecutionStatus
from anvil.runner import (
    AuthCheckCache,
    OrganizationRunCache,
    OrganizationRunCacheEntry,
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
    monkeypatch.setattr(
        "anvil.runner.resolve_tasks",
        lambda task_specs: ResolvedExecution(ordered=[], adjacency={}),
    )
    monkeypatch.setattr(
        "anvil.runner._preflight_organization",
        lambda **kwargs: (
            object(),
            "o-shared",
            "999999999999",
            "999999999999",
            {
                "999999999999": {
                    "account_number": "999999999999",
                    "account_alias": "management",
                }
            },
            {"us-east-1": "ENABLED_BY_DEFAULT"},
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

    targets = [
        TargetDescriptor(
            config_branch=ConfigBranch.ORGANIZATIONS,
            name="org-a",
            profile="shared",
            tasks=[],
        ),
        TargetDescriptor(
            config_branch=ConfigBranch.ORGANIZATIONS,
            name="org-b",
            profile="shared",
            tasks=[],
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
            config_branch=ConfigBranch.ORGANIZATIONS,
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
            config_branch=ConfigBranch.ORGANIZATIONS,
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
    monkeypatch.setattr(
        "anvil.runner.resolve_tasks",
        lambda task_specs: ResolvedExecution(ordered=[], adjacency={}),
    )

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
            config_branch=ConfigBranch.ORGANIZATIONS,
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
        base_session_account_id="999999999999",
        discovered_accounts=discovered_accounts,
        region_statuses=region_statuses,
    )

    outcome = run_prepared_target(prepared_target=prepared_target)

    assert outcome.target_result.target_name == "org-a"
    assert outcome.cancelled is False


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
        auth_cache=AuthCheckCache(),
    )

    assert prepared.context is not None
    assert prepared.context.max_parallel_regions == 3
