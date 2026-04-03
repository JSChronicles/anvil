from __future__ import annotations

import importlib
import threading
import time
from types import SimpleNamespace


def _org_target(descriptors, *, name: str, profile: str):
    return descriptors.TargetDescriptor(
        config_branch=descriptors.ConfigBranch.ORGANIZATIONS, name=name, profile=profile
    )


def _accounts_target(descriptors, *, name: str, profile: str, include: list[str]):
    return descriptors.TargetDescriptor(
        config_branch=descriptors.ConfigBranch.ACCOUNTS,
        name=name,
        profile=profile,
        include=include,
        role_name="TestRole",
    )


def test_run_auth_checks_uses_parallel_pool_and_preserves_input_order(monkeypatch):
    runner = importlib.import_module("anvil.runner")
    descriptors = importlib.import_module("anvil.descriptors")
    results = importlib.import_module("anvil.results")

    started_count = 0
    max_in_flight = 0
    completed_order: list[str] = []
    lock = threading.Lock()
    release_event = threading.Event()

    targets = [
        _org_target(descriptors, name="org-a", profile="a"),
        _org_target(descriptors, name="org-b", profile="b"),
        _org_target(descriptors, name="org-c", profile="c"),
    ]

    monkeypatch.setattr(
        runner,
        "infer_auth_source",
        lambda profile: SimpleNamespace(value=f"source-{profile}"),
    )

    def fake_auth_check(*, target_name: str, profile: str | None, auth_source):
        nonlocal started_count, max_in_flight

        with lock:
            started_count += 1
            max_in_flight = max(max_in_flight, started_count)
            if started_count == len(targets):
                release_event.set()

        assert release_event.wait(timeout=1.0)

        time.sleep({"org-a": 0.03, "org-b": 0.0, "org-c": 0.01}[target_name])

        with lock:
            completed_order.append(target_name)
            started_count -= 1

        return results.AuthResult(
            target_name=target_name,
            status=results.ExecutionStatus.SUCCESS,
            source=auth_source.value,
            started_at="start",
            ended_at="end",
            duration_seconds=0.0,
            message="ok",
        )

    monkeypatch.setattr(runner, "auth_check", fake_auth_check)

    engine_result = runner.run_auth_checks(targets=targets)

    assert max_in_flight > 1
    assert completed_order != ["org-a", "org-b", "org-c"]
    assert [result.target_name for result in engine_result.auth_results] == [
        "org-a",
        "org-b",
        "org-c",
    ]
    assert engine_result.state is results.EngineState.COMPLETED_SUCCESS


def test_run_auth_checks_handles_mixed_success_and_failure_in_input_order(monkeypatch):
    runner = importlib.import_module("anvil.runner")
    descriptors = importlib.import_module("anvil.descriptors")
    results = importlib.import_module("anvil.results")

    targets = [
        _org_target(descriptors, name="org-a", profile="a"),
        _org_target(descriptors, name="org-b", profile="b"),
        _org_target(descriptors, name="org-c", profile="c"),
    ]

    monkeypatch.setattr(
        runner,
        "infer_auth_source",
        lambda profile: SimpleNamespace(value=f"source-{profile}"),
    )

    delays = {"org-a": 0.10, "org-b": 0.01, "org-c": 0.05}
    statuses = {
        "org-a": results.ExecutionStatus.SUCCESS,
        "org-b": results.ExecutionStatus.ERROR,
        "org-c": results.ExecutionStatus.SUCCESS,
    }

    def fake_auth_check(*, target_name: str, profile: str | None, auth_source):
        time.sleep(delays[target_name])
        return results.AuthResult(
            target_name=target_name,
            status=statuses[target_name],
            source=auth_source.value,
            started_at="start",
            ended_at="end",
            duration_seconds=delays[target_name],
            message="ok" if statuses[target_name].is_success else "bad",
        )

    monkeypatch.setattr(runner, "auth_check", fake_auth_check)

    engine_result = runner.run_auth_checks(targets=targets)

    assert [result.target_name for result in engine_result.auth_results] == [
        "org-a",
        "org-b",
        "org-c",
    ]
    assert [result.status for result in engine_result.auth_results] == [
        results.ExecutionStatus.SUCCESS,
        results.ExecutionStatus.ERROR,
        results.ExecutionStatus.SUCCESS,
    ]
    assert engine_result.state is results.EngineState.AUTH_FAILED


def test_run_multiple_targets_executes_targets_in_parallel_and_preserves_input_order(
    monkeypatch,
):
    runner = importlib.import_module("anvil.runner")
    descriptors = importlib.import_module("anvil.descriptors")
    results = importlib.import_module("anvil.results")

    started_count = 0
    max_in_flight = 0
    completed_order: list[str] = []
    lock = threading.Lock()
    release_event = threading.Event()

    targets = [
        _org_target(descriptors, name="org-a", profile="a"),
        _org_target(descriptors, name="org-b", profile="b"),
    ]

    def fake_prepare_target(*, index, target, **kwargs):
        return runner.PreparedTarget(
            index=index,
            effective_target=target,
            auth_result=results.AuthResult(
                target_name=target.name,
                status=results.ExecutionStatus.SUCCESS,
                source=f"source-{target.profile}",
                started_at="start",
                ended_at="end",
                duration_seconds=0.0,
                message="ok",
            ),
            context=SimpleNamespace(cancel_event=threading.Event(), dry_run=False),
            organization_id=target.name,
            management_account_id="123456789012",
        )

    def fake_run_prepared_target(*, prepared_target):
        nonlocal started_count, max_in_flight

        with lock:
            started_count += 1
            max_in_flight = max(max_in_flight, started_count)
            if started_count == len(targets):
                release_event.set()

        assert release_event.wait(timeout=1.0)
        time.sleep({"org-a": 0.03, "org-b": 0.0}[prepared_target.effective_target.name])

        with lock:
            completed_order.append(prepared_target.effective_target.name)
            started_count -= 1

        return runner.TargetExecutionOutcome(
            index=prepared_target.index,
            target_result=results.TargetResult.create(
                config_branch=prepared_target.effective_target.config_branch,
                target_name=prepared_target.effective_target.name,
                dry_run=False,
                account_results=[],
            ),
            cancelled=False,
        )

    monkeypatch.setattr(runner, "prepare_target", fake_prepare_target)
    monkeypatch.setattr(runner, "run_prepared_target", fake_run_prepared_target)

    engine_result = runner.run_multiple_targets(
        targets=targets,
        max_parallel_targets=2,
        cli_dry_run=None,
        cli_include=None,
        cli_exclude=None,
    )

    assert max_in_flight > 1
    assert completed_order != ["org-a", "org-b"]
    assert [result.target_name for result in engine_result.auth_results] == [
        "org-a",
        "org-b",
    ]
    assert [result.target_name for result in engine_result.target_results] == [
        "org-a",
        "org-b",
    ]
    assert engine_result.state is results.EngineState.COMPLETED_SUCCESS


def test_run_multiple_targets_serializes_same_org_targets(monkeypatch):
    runner = importlib.import_module("anvil.runner")
    descriptors = importlib.import_module("anvil.descriptors")
    results = importlib.import_module("anvil.results")

    active_org_counts: dict[str, int] = {}
    max_same_org = 0
    max_total_in_flight = 0
    total_in_flight = 0
    lock = threading.Lock()

    targets = [
        _org_target(descriptors, name="org-a", profile="a"),
        _org_target(descriptors, name="org-b", profile="b"),
        _org_target(descriptors, name="org-c", profile="c"),
    ]
    org_ids = {"org-a": "shared-org", "org-b": "shared-org", "org-c": "other-org"}

    def fake_prepare_target(*, index, target, **kwargs):
        return runner.PreparedTarget(
            index=index,
            effective_target=target,
            auth_result=results.AuthResult(
                target_name=target.name,
                status=results.ExecutionStatus.SUCCESS,
                source=f"source-{target.profile}",
                started_at="start",
                ended_at="end",
                duration_seconds=0.0,
                message="ok",
            ),
            context=SimpleNamespace(cancel_event=threading.Event(), dry_run=False),
            organization_id=org_ids[target.name],
            management_account_id="123456789012",
        )

    def fake_run_prepared_target(*, prepared_target):
        nonlocal max_same_org, max_total_in_flight, total_in_flight
        organization_id = prepared_target.organization_id or ""

        with lock:
            total_in_flight += 1
            active_org_counts[organization_id] = (
                active_org_counts.get(organization_id, 0) + 1
            )
            max_same_org = max(max_same_org, active_org_counts[organization_id])
            max_total_in_flight = max(max_total_in_flight, total_in_flight)

        time.sleep(0.03)

        with lock:
            total_in_flight -= 1
            active_org_counts[organization_id] -= 1

        return runner.TargetExecutionOutcome(
            index=prepared_target.index,
            target_result=results.TargetResult.create(
                config_branch=prepared_target.effective_target.config_branch,
                target_name=prepared_target.effective_target.name,
                dry_run=False,
                account_results=[],
            ),
            cancelled=False,
        )

    monkeypatch.setattr(runner, "prepare_target", fake_prepare_target)
    monkeypatch.setattr(runner, "run_prepared_target", fake_run_prepared_target)

    engine_result = runner.run_multiple_targets(
        targets=targets,
        max_parallel_targets=2,
        cli_dry_run=None,
        cli_include=None,
        cli_exclude=None,
    )

    assert max_same_org == 1
    assert max_total_in_flight == 2
    assert [result.target_name for result in engine_result.target_results] == [
        "org-a",
        "org-b",
        "org-c",
    ]


def test_run_multiple_targets_parallelizes_accounts_branch(monkeypatch):
    runner = importlib.import_module("anvil.runner")
    descriptors = importlib.import_module("anvil.descriptors")
    results = importlib.import_module("anvil.results")

    started_count = 0
    max_in_flight = 0
    lock = threading.Lock()
    release_event = threading.Event()

    targets = [
        _accounts_target(
            descriptors, name="group-a", profile="a", include=["111111111111"]
        ),
        _accounts_target(
            descriptors, name="group-b", profile="b", include=["222222222222"]
        ),
    ]

    def fake_prepare_target(*, index, target, **kwargs):
        return runner.PreparedTarget(
            index=index,
            effective_target=target,
            auth_result=results.AuthResult(
                target_name=target.name,
                status=results.ExecutionStatus.SUCCESS,
                source=f"source-{target.profile}",
                started_at="start",
                ended_at="end",
                duration_seconds=0.0,
                message="ok",
            ),
            context=SimpleNamespace(cancel_event=threading.Event(), dry_run=False),
        )

    def fake_run_prepared_target(*, prepared_target):
        nonlocal started_count, max_in_flight

        with lock:
            started_count += 1
            max_in_flight = max(max_in_flight, started_count)
            if started_count == len(targets):
                release_event.set()

        assert release_event.wait(timeout=1.0)
        time.sleep(0.02)

        with lock:
            started_count -= 1

        return runner.TargetExecutionOutcome(
            index=prepared_target.index,
            target_result=results.TargetResult.create(
                config_branch=prepared_target.effective_target.config_branch,
                target_name=prepared_target.effective_target.name,
                dry_run=False,
                account_results=[],
            ),
            cancelled=False,
        )

    monkeypatch.setattr(runner, "prepare_target", fake_prepare_target)
    monkeypatch.setattr(runner, "run_prepared_target", fake_run_prepared_target)

    engine_result = runner.run_multiple_targets(
        targets=targets,
        max_parallel_targets=2,
        cli_dry_run=None,
        cli_include=None,
        cli_exclude=None,
    )

    assert max_in_flight > 1
    assert [result.target_name for result in engine_result.target_results] == [
        "group-a",
        "group-b",
    ]


def test_run_multiple_targets_pipelines_preparation_into_execution(monkeypatch):
    runner = importlib.import_module("anvil.runner")
    descriptors = importlib.import_module("anvil.descriptors")
    results = importlib.import_module("anvil.results")

    prep_done = threading.Event()
    first_execution_started = threading.Event()

    targets = [
        _org_target(descriptors, name="org-a", profile="a"),
        _org_target(descriptors, name="org-b", profile="b"),
        _org_target(descriptors, name="org-c", profile="c"),
    ]

    def fake_prepare_target(*, index, target, **kwargs):
        if target.name == "org-c":
            assert first_execution_started.wait(timeout=1.0)
            time.sleep(0.03)

        prepared = runner.PreparedTarget(
            index=index,
            effective_target=target,
            auth_result=results.AuthResult(
                target_name=target.name,
                status=results.ExecutionStatus.SUCCESS,
                source=f"source-{target.profile}",
                started_at="start",
                ended_at="end",
                duration_seconds=0.0,
                message="ok",
            ),
            context=SimpleNamespace(cancel_event=threading.Event(), dry_run=False),
            organization_id=target.name,
            management_account_id="123456789012",
        )

        if target.name == "org-c":
            prep_done.set()

        return prepared

    def fake_run_prepared_target(*, prepared_target):
        if prepared_target.effective_target.name == "org-a":
            first_execution_started.set()
            assert not prep_done.is_set()

        time.sleep(0.01)
        return runner.TargetExecutionOutcome(
            index=prepared_target.index,
            target_result=results.TargetResult.create(
                config_branch=prepared_target.effective_target.config_branch,
                target_name=prepared_target.effective_target.name,
                dry_run=False,
                account_results=[],
            ),
            cancelled=False,
        )

    monkeypatch.setattr(runner, "prepare_target", fake_prepare_target)
    monkeypatch.setattr(runner, "run_prepared_target", fake_run_prepared_target)

    engine_result = runner.run_multiple_targets(
        targets=targets,
        max_parallel_targets=2,
        cli_dry_run=None,
        cli_include=None,
        cli_exclude=None,
    )

    assert first_execution_started.is_set()
    assert prep_done.is_set()
    assert [result.target_name for result in engine_result.target_results] == [
        "org-a",
        "org-b",
        "org-c",
    ]
