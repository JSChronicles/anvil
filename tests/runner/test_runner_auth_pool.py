from __future__ import annotations

import importlib
import threading
import time
from types import SimpleNamespace


def test_run_auth_checks_uses_parallel_pool_and_preserves_input_order(monkeypatch):
    runner = importlib.import_module("anvil.runner")
    descriptors = importlib.import_module("anvil.descriptors")
    results = importlib.import_module("anvil.results")

    started_count = 0
    max_in_flight = 0
    completed_order: list[str] = []
    lock = threading.Lock()
    release_event = threading.Event()

    orgs = [
        descriptors.OrgDescriptor(name="org-a", profile="a"),
        descriptors.OrgDescriptor(name="org-b", profile="b"),
        descriptors.OrgDescriptor(name="org-c", profile="c"),
    ]

    monkeypatch.setattr(
        runner,
        "infer_auth_source",
        lambda profile: SimpleNamespace(value=f"source-{profile}"),
    )

    def fake_auth_check(*, org_name: str, profile: str | None, auth_source):
        nonlocal started_count, max_in_flight

        with lock:
            started_count += 1
            max_in_flight = max(max_in_flight, started_count)
            if started_count == len(orgs):
                release_event.set()

        assert release_event.wait(timeout=1.0)

        time.sleep({"org-a": 0.03, "org-b": 0.0, "org-c": 0.01}[org_name])

        with lock:
            completed_order.append(org_name)
            started_count -= 1

        return results.AuthResult(
            org_name=org_name,
            status=results.ExecutionStatus.SUCCESS,
            source=auth_source.value,
            started_at="start",
            ended_at="end",
            duration_seconds=0.0,
            message="ok",
        )

    monkeypatch.setattr(runner, "auth_check", fake_auth_check)

    engine_result = runner.run_auth_checks(orgs=orgs)

    assert max_in_flight > 1
    assert completed_order != ["org-a", "org-b", "org-c"]
    assert [result.org_name for result in engine_result.auth_results] == [
        "org-a",
        "org-b",
        "org-c",
    ]
    assert engine_result.state is results.EngineState.COMPLETED_SUCCESS


def test_run_auth_checks_handles_mixed_success_and_failure_in_input_order(monkeypatch):
    runner = importlib.import_module("anvil.runner")
    descriptors = importlib.import_module("anvil.descriptors")
    results = importlib.import_module("anvil.results")

    orgs = [
        descriptors.OrgDescriptor(name="org-a", profile="a"),
        descriptors.OrgDescriptor(name="org-b", profile="b"),
        descriptors.OrgDescriptor(name="org-c", profile="c"),
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

    def fake_auth_check(*, org_name: str, profile: str | None, auth_source):
        time.sleep(delays[org_name])
        return results.AuthResult(
            org_name=org_name,
            status=statuses[org_name],
            source=auth_source.value,
            started_at="start",
            ended_at="end",
            duration_seconds=delays[org_name],
            message="ok" if statuses[org_name].is_success else "bad",
        )

    monkeypatch.setattr(runner, "auth_check", fake_auth_check)

    engine_result = runner.run_auth_checks(orgs=orgs)

    assert [result.org_name for result in engine_result.auth_results] == [
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


def test_run_multiple_orgs_behavior_is_unchanged(monkeypatch):
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

    def fake_auth_check(*, org_name: str, profile: str | None, auth_source):
        auth_calls.append(org_name)
        return results.AuthResult(
            org_name=org_name,
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

    class FakeOrganization:
        def __init__(self, *, name: str, context, **kwargs):
            self.name = name
            self.context = context

        def execute(self):
            execute_calls.append(self.name)
            return results.OrgResult.create(
                org_name=self.name, dry_run=self.context.dry_run, account_results=[]
            )

    monkeypatch.setattr(runner, "Organization", FakeOrganization)

    orgs = [
        descriptors.OrgDescriptor(name="org-a", profile="a"),
        descriptors.OrgDescriptor(name="org-b", profile="b"),
    ]

    engine_result = runner.run_multiple_orgs(
        orgs=orgs, cli_dry_run=None, cli_include=None, cli_exclude=None
    )

    assert auth_calls == ["org-a", "org-b"]
    assert execute_calls == ["org-a", "org-b"]
    assert [result.org_name for result in engine_result.organization_results] == [
        "org-a",
        "org-b",
    ]
    assert engine_result.state is results.EngineState.COMPLETED_SUCCESS
