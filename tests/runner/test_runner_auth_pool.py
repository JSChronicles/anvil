from __future__ import annotations

import importlib
import threading
import time
from types import SimpleNamespace


def _org_target(descriptors, *, name: str, profile: str):
    return descriptors.TargetDescriptor(
        config_branch=descriptors.ConfigBranch.ORGANIZATIONS, name=name, profile=profile
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
    assert engine_result.state is results.EngineState.COMPLETED_WITH_FAILURES


def test_run_multiple_targets_behavior_is_unchanged(monkeypatch):
    runner = importlib.import_module("anvil.runner")
    descriptors = importlib.import_module("anvil.descriptors")
    results = importlib.import_module("anvil.results")

    auth_calls: list[str] = []
    execute_calls: list[str] = []

    monkeypatch.setattr(
        runner,
        "infer_auth_source",
        lambda profile: SimpleNamespace(value=f"source-{profile}"),
    )

    def fake_auth_check(*, target_name: str, profile: str | None, auth_source):
        auth_calls.append(target_name)
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
    monkeypatch.setattr(
        runner,
        "resolve_tasks",
        lambda task_specs: SimpleNamespace(ordered=[], adjacency={}),
    )

    class FakeResolver:
        def __init__(self, *, descriptor, context, **kwargs):
            self.descriptor = descriptor
            self.context = context

        def resolve_accounts(self):
            return []

    def fake_execute_accounts(*, name, config_branch, max_workers, context, accounts):
        execute_calls.append(name)
        return results.TargetResult.create(
            config_branch=config_branch,
            target_name=name,
            dry_run=context.dry_run,
            account_results=[],
        )

    monkeypatch.setattr(runner, "OrganizationResolver", FakeResolver)
    monkeypatch.setattr(runner, "execute_accounts", fake_execute_accounts)

    targets = [
        _org_target(descriptors, name="org-a", profile="a"),
        _org_target(descriptors, name="org-b", profile="b"),
    ]

    engine_result = runner.run_multiple_targets(
        targets=targets, cli_dry_run=None, cli_include=None, cli_exclude=None
    )

    assert auth_calls == ["org-a", "org-b"]
    assert execute_calls == ["org-a", "org-b"]
    assert [result.target_name for result in engine_result.target_results] == [
        "org-a",
        "org-b",
    ]
    assert engine_result.state is results.EngineState.COMPLETED_SUCCESS
