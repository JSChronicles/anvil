from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from anvil.descriptors import TargetDescriptor
from anvil.execution_context import ExecutionContext
from anvil.providers.base import ProviderAuthResult, ProviderMetadata
from anvil.results import AuthResult, EngineState, ExecutionStatus, TargetResult
from anvil.runner import (
    PreparedTarget,
    TargetExecutionOutcome,
    run_auth_checks,
    run_multiple_targets,
)


def _target(*, name: str, profile: str, mode: str = "organization") -> TargetDescriptor:
    return TargetDescriptor(
        name=name,
        provider="test",
        mode=mode,
        provider_options={"profile": profile},
        include=["target-a"] if mode == "accounts" else None,
        tasks=[],
    )


class _AuthProvider:
    metadata = ProviderMetadata(
        name="test",
        display_name="Test",
        supported_task_scopes=frozenset({"region"}),
        default_regions=("global",),
    )

    def __init__(self, check) -> None:
        self._check = check

    def validate_target(self, target) -> None:
        return None

    def resolve_target_filters(self, *, target, include_override, exclude_override):
        return target.include, target.exclude

    def auth_cache_key(self, target):
        return ("test", target.provider_options["profile"])

    def auth_check(self, target):
        return self._check(target)


def _patch_provider(monkeypatch, check) -> None:
    provider = _AuthProvider(check)
    monkeypatch.setattr("anvil.runner._load_provider", lambda provider_name: provider)


def _success_auth(target: TargetDescriptor) -> ProviderAuthResult:
    return ProviderAuthResult(status=ExecutionStatus.SUCCESS, source="test")


def _prepared(
    *,
    index: int,
    target: TargetDescriptor,
    exclusive_execution_keys: tuple[object, ...] = (),
) -> PreparedTarget:
    return PreparedTarget(
        index=index,
        provider=SimpleNamespace(),
        effective_target=target,
        auth_result=AuthResult(
            target_name=target.name,
            status=ExecutionStatus.SUCCESS,
            source="test",
            started_at="start",
            ended_at="end",
            duration_seconds=0.0,
        ),
        context=ExecutionContext(
            regions=["global"], dry_run=False, tasks=[], metadata={}
        ),
        exclusive_execution_keys=exclusive_execution_keys,
    )


def _outcome(prepared_target: PreparedTarget) -> TargetExecutionOutcome:
    target = prepared_target.effective_target
    return TargetExecutionOutcome(
        index=prepared_target.index,
        target_result=TargetResult.create(
            target_name=target.name,
            provider=target.provider,
            dry_run=False,
            entities=[],
        ),
        cancelled=False,
    )


def test_run_auth_checks_uses_parallel_pool_and_preserves_input_order(monkeypatch):
    targets = [
        _target(name="target-a", profile="a"),
        _target(name="target-b", profile="b"),
        _target(name="target-c", profile="c"),
    ]
    active = 0
    max_active = 0
    lock = threading.Lock()
    release = threading.Event()

    def check(target):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
            if active == len(targets):
                release.set()
        assert release.wait(timeout=1.0)
        time.sleep({"target-a": 0.03, "target-b": 0.0, "target-c": 0.01}[target.name])
        with lock:
            active -= 1
        return _success_auth(target)

    _patch_provider(monkeypatch, check)
    result = run_auth_checks(targets=targets)

    assert max_active > 1
    assert [item.target_name for item in result.auth_results] == [
        "target-a",
        "target-b",
        "target-c",
    ]
    assert result.state is EngineState.COMPLETED_SUCCESS


def test_run_auth_checks_preserves_mixed_results_in_input_order(monkeypatch):
    targets = [
        _target(name="target-a", profile="a"),
        _target(name="target-b", profile="b"),
        _target(name="target-c", profile="c"),
    ]

    def check(target):
        status = (
            ExecutionStatus.ERROR
            if target.name == "target-b"
            else ExecutionStatus.SUCCESS
        )
        return ProviderAuthResult(status=status, source="test", message=target.name)

    _patch_provider(monkeypatch, check)
    result = run_auth_checks(targets=targets)

    assert [item.status for item in result.auth_results] == [
        ExecutionStatus.SUCCESS,
        ExecutionStatus.ERROR,
        ExecutionStatus.SUCCESS,
    ]
    assert result.state is EngineState.AUTH_FAILED


@pytest.mark.parametrize("status", [ExecutionStatus.SUCCESS, ExecutionStatus.ERROR])
def test_run_auth_checks_reuses_same_provider_cache_key(monkeypatch, status):
    targets = [
        _target(name="target-a", profile="shared"),
        _target(name="target-b", profile="shared"),
        _target(name="target-c", profile="shared"),
    ]
    calls: list[str] = []

    def check(target):
        calls.append(target.name)
        return ProviderAuthResult(status=status, source="test", message="cached")

    _patch_provider(monkeypatch, check)
    result = run_auth_checks(targets=targets)

    assert calls == ["target-a"]
    assert [item.target_name for item in result.auth_results] == [
        "target-a",
        "target-b",
        "target-c",
    ]
    assert all(item.status is status for item in result.auth_results)
    assert [item.duration_seconds for item in result.auth_results][1:] == [0.0, 0.0]


def test_run_auth_checks_keeps_distinct_cache_keys_separate(monkeypatch):
    targets = [
        _target(name="target-a", profile="a"),
        _target(name="target-b", profile="b"),
        _target(name="target-c", profile="a"),
    ]
    calls: list[str] = []

    def check(target):
        calls.append(target.name)
        return _success_auth(target)

    _patch_provider(monkeypatch, check)
    run_auth_checks(targets=targets)

    assert calls == ["target-a", "target-b"]


def test_run_auth_checks_single_flights_concurrent_same_key(monkeypatch):
    targets = [
        _target(name="target-a", profile="shared"),
        _target(name="target-b", profile="shared"),
    ]
    calls: list[str] = []
    started = threading.Event()
    release = threading.Event()

    def check(target):
        calls.append(target.name)
        started.set()
        assert release.wait(timeout=1.0)
        return _success_auth(target)

    _patch_provider(monkeypatch, check)
    holder = {}
    thread = threading.Thread(
        target=lambda: holder.setdefault("result", run_auth_checks(targets=targets))
    )
    thread.start()
    assert started.wait(timeout=1.0)
    release.set()
    thread.join(timeout=1.0)

    assert not thread.is_alive()
    assert calls == ["target-a"]
    assert len(holder["result"].auth_results) == 2


def test_run_multiple_targets_parallelizes_and_preserves_result_order(monkeypatch):
    targets = [
        _target(name="target-a", profile="a"),
        _target(name="target-b", profile="b"),
    ]
    active = 0
    max_active = 0
    lock = threading.Lock()
    release = threading.Event()

    monkeypatch.setattr(
        "anvil.runner.prepare_target",
        lambda index, target, **kwargs: _prepared(index=index, target=target),
    )

    def run(prepared_target):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
            if active == len(targets):
                release.set()
        assert release.wait(timeout=1.0)
        with lock:
            active -= 1
        return _outcome(prepared_target)

    monkeypatch.setattr("anvil.runner.run_prepared_target", run)
    result = run_multiple_targets(
        targets=targets,
        max_parallel_targets=2,
        cli_dry_run=None,
        cli_include=None,
        cli_exclude=None,
    )

    assert max_active == 2
    assert [item.target_name for item in result.target_results] == [
        "target-a",
        "target-b",
    ]


def test_run_multiple_targets_serializes_overlapping_provider_keys(monkeypatch):
    targets = [
        _target(name="target-a", profile="a"),
        _target(name="target-b", profile="b"),
        _target(name="target-c", profile="c"),
    ]
    keys = {
        "target-a": (("test", "shared"),),
        "target-b": (("test", "shared"),),
        "target-c": (("test", "other"),),
    }
    active_by_key: dict[object, int] = {}
    max_shared = 0
    max_total = 0
    total = 0
    lock = threading.Lock()

    monkeypatch.setattr(
        "anvil.runner.prepare_target",
        lambda index, target, **kwargs: _prepared(
            index=index, target=target, exclusive_execution_keys=keys[target.name]
        ),
    )

    def run(prepared_target):
        nonlocal max_shared, max_total, total
        key = prepared_target.exclusive_execution_keys[0]
        with lock:
            total += 1
            active_by_key[key] = active_by_key.get(key, 0) + 1
            max_shared = max(max_shared, active_by_key.get(("test", "shared"), 0))
            max_total = max(max_total, total)
        time.sleep(0.03)
        with lock:
            total -= 1
            active_by_key[key] -= 1
        return _outcome(prepared_target)

    monkeypatch.setattr("anvil.runner.run_prepared_target", run)
    run_multiple_targets(
        targets=targets,
        max_parallel_targets=2,
        cli_dry_run=None,
        cli_include=None,
        cli_exclude=None,
    )

    assert max_shared == 1
    assert max_total == 2


def test_run_multiple_targets_pipelines_preparation_and_execution(monkeypatch):
    targets = [
        _target(name="target-a", profile="a"),
        _target(name="target-b", profile="b"),
        _target(name="target-c", profile="c"),
    ]
    first_execution_started = threading.Event()
    last_preparation_finished = threading.Event()

    def prepare(index, target, **kwargs):
        if target.name == "target-c":
            assert first_execution_started.wait(timeout=1.0)
            last_preparation_finished.set()
        return _prepared(index=index, target=target)

    def run(prepared_target):
        if prepared_target.effective_target.name == "target-a":
            first_execution_started.set()
            assert not last_preparation_finished.is_set()
        time.sleep(0.01)
        return _outcome(prepared_target)

    monkeypatch.setattr("anvil.runner.prepare_target", prepare)
    monkeypatch.setattr("anvil.runner.run_prepared_target", run)

    result = run_multiple_targets(
        targets=targets,
        max_parallel_targets=2,
        cli_dry_run=None,
        cli_include=None,
        cli_exclude=None,
    )

    assert first_execution_started.is_set()
    assert last_preparation_finished.is_set()
    assert len(result.target_results) == 3
