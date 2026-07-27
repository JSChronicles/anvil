from __future__ import annotations

import threading
from collections import deque
from types import SimpleNamespace

from anvil.descriptors import TargetDescriptor
from anvil.execution_context import ExecutionContext
from anvil.providers.base import (
    ExecutionTarget,
    ProviderAuthResult,
    ProviderExecutionPlan,
    ProviderMetadata,
    ProviderPreparation,
)
from anvil.results import EntityResult, EngineState, ExecutionStatus
from anvil.runner import (
    AuthCheckCache,
    PreparedTarget,
    _SingleFlightCache,
    _execute_provider_targets,
    _next_eligible_target,
    prepare_target,
    run_multiple_targets,
    run_prepared_target,
)
from anvil.task_loader import ResolvedExecution, ResolvedTask


def _target(**overrides) -> TargetDescriptor:
    values = {
        "name": "target-a",
        "provider": "test",
        "mode": "fleet",
        "regions": ["global"],
        "tasks": [],
    }
    values.update(overrides)
    return TargetDescriptor(**values)


class _Runtime:
    def __init__(self, *, fail_session: bool = False) -> None:
        self.fail_session = fail_session

    def build_session(self, *, region: str):
        if self.fail_session:
            raise RuntimeError("session failed")
        return SimpleNamespace(region_name=region)

    def record_region_outcome(self, **kwargs) -> None:
        return None

    def close(self) -> None:
        return None


class _Provider:
    metadata = ProviderMetadata(
        name="test",
        display_name="Test",
        supported_task_scopes=frozenset({"region", "target"}),
        default_regions=("global",),
    )

    def __init__(
        self,
        *,
        auth_status: ExecutionStatus = ExecutionStatus.SUCCESS,
        fail_session: bool = False,
        fail_resolution: bool = False,
    ) -> None:
        self.auth_status = auth_status
        self.fail_session = fail_session
        self.fail_resolution = fail_resolution
        self.preparation = object()
        self.seen_preparation = None

    def validate_target(self, target) -> None:
        return None

    def resolve_target_filters(self, *, target, include_override, exclude_override):
        include = include_override if include_override is not None else target.include
        exclude = exclude_override if exclude_override is not None else target.exclude
        if include is not None and exclude is not None:
            raise ValueError("test filters are mutually exclusive")
        return include, exclude

    def auth_cache_key(self, target):
        return ("test", target.name)

    def auth_check(self, target):
        return ProviderAuthResult(
            status=self.auth_status,
            source="test",
            message="auth failed" if self.auth_status.is_error else "ok",
        )

    def prepare_target(self, **kwargs):
        return ProviderPreparation(
            data=self.preparation,
            exclusive_execution_keys=(("test", kwargs["target"].name),),
        )

    def resolve_execution_targets(self, **kwargs):
        if self.fail_resolution:
            raise RuntimeError("resolution failed")
        self.seen_preparation = kwargs["preparation"]
        target = kwargs["target"]
        return ProviderExecutionPlan(
            execution_targets=[
                ExecutionTarget(
                    id="entity-a",
                    name="Entity A",
                    type="resource",
                    provider="test",
                    regions=list(kwargs["regions"]),
                    metadata={"target": target.name},
                )
            ]
        )

    def prepare_execution_runtime(self, **kwargs):
        return _Runtime(fail_session=self.fail_session)


def _patch_provider(monkeypatch, provider: _Provider) -> None:
    monkeypatch.setattr("anvil.runner._load_provider", lambda provider_name: provider)


def test_auth_failure_short_circuits_target_execution(monkeypatch):
    provider = _Provider(auth_status=ExecutionStatus.ERROR)
    _patch_provider(monkeypatch, provider)

    result = run_multiple_targets(
        targets=[_target()],
        max_parallel_targets=1,
        cli_dry_run=None,
        cli_include=None,
        cli_exclude=None,
    )

    assert result.state is EngineState.AUTH_FAILED
    assert result.target_results == []
    assert result.auth_results[0].message == "auth failed"


def test_provider_dispatch_executes_resolved_target_without_aws_paths(monkeypatch):
    provider = _Provider()
    _patch_provider(monkeypatch, provider)

    result = run_multiple_targets(
        targets=[_target()],
        max_parallel_targets=1,
        cli_dry_run=None,
        cli_include=None,
        cli_exclude=None,
    )

    assert result.state is EngineState.COMPLETED_SUCCESS
    assert [entity.id for entity in result.target_results[0].entities] == ["entity-a"]
    assert provider.seen_preparation is provider.preparation


def test_provider_session_failure_becomes_entity_error(monkeypatch):
    provider = _Provider(fail_session=True)
    _patch_provider(monkeypatch, provider)

    result = run_multiple_targets(
        targets=[_target()],
        max_parallel_targets=1,
        cli_dry_run=None,
        cli_include=None,
        cli_exclude=None,
    )

    entity = result.target_results[0].entities[0]
    assert entity.status is ExecutionStatus.ERROR
    assert entity.error == "session failed"


def test_universal_task_receives_provider_neutral_kwargs_and_records_actions(
    monkeypatch,
):
    provider = _Provider()
    _patch_provider(monkeypatch, provider)
    seen = {}

    def task(
        *,
        provider,
        execution_target_id,
        execution_target_name,
        execution_target_type,
        region,
        session,
        dry_run,
        metadata,
        actions,
    ):
        seen.update(locals())
        actions.record("task ran")
        return {"target": execution_target_id}

    monkeypatch.setattr(
        "anvil.runner.resolve_tasks",
        lambda **kwargs: ResolvedExecution(
            ordered=[ResolvedTask("neutral", task, depends_on=[], optional=False)],
            adjacency={},
        ),
    )

    result = run_multiple_targets(
        targets=[_target(tasks=[{"name": "neutral"}], metadata={"team": "security"})],
        max_parallel_targets=1,
        cli_dry_run=None,
        cli_include=None,
        cli_exclude=None,
    )

    task_result = result.target_results[0].entities[0].tasks[0]
    assert task_result.status is ExecutionStatus.SUCCESS
    assert task_result.actions == ["task ran"]
    assert seen["provider"] == "test"
    assert seen["execution_target_id"] == "entity-a"
    assert seen["execution_target_name"] == "Entity A"
    assert seen["execution_target_type"] == "resource"
    assert seen["region"] == "global"
    assert seen["session"].region_name == "global"
    assert seen["dry_run"] is False
    assert seen["metadata"] == {"team": "security"}


def test_prepare_target_carries_provider_preflight_and_execution_controls(monkeypatch):
    provider = _Provider()
    _patch_provider(monkeypatch, provider)
    target = _target(max_parallel_regions=3)

    prepared = prepare_target(
        index=0,
        target=target,
        cli_dry_run=True,
        cli_include=None,
        cli_exclude=None,
        preparation_cache=_SingleFlightCache(),
        auth_cache=AuthCheckCache(),
    )

    assert prepared.provider is provider
    assert prepared.provider_preflight is provider.preparation
    assert prepared.exclusive_execution_keys == (("test", "target-a"),)
    assert prepared.context is not None
    assert prepared.context.max_parallel_regions == 3
    assert prepared.context.dry_run is True


def test_prepare_target_reports_provider_filter_errors_as_config_auth_results(
    monkeypatch,
):
    provider = _Provider()
    _patch_provider(monkeypatch, provider)

    prepared = prepare_target(
        index=0,
        target=_target(include=["a"]),
        cli_dry_run=None,
        cli_include=None,
        cli_exclude=["b"],
        preparation_cache=_SingleFlightCache(),
        auth_cache=AuthCheckCache(),
    )

    assert prepared.context is None
    assert prepared.auth_result.status is ExecutionStatus.ERROR
    assert prepared.auth_result.source == "config"
    assert "mutually exclusive" in str(prepared.auth_result.message)


def test_run_prepared_target_passes_opaque_preflight_to_provider(monkeypatch):
    provider = _Provider()
    _patch_provider(monkeypatch, provider)
    prepared = prepare_target(
        index=0,
        target=_target(),
        cli_dry_run=None,
        cli_include=None,
        cli_exclude=None,
        preparation_cache=_SingleFlightCache(),
        auth_cache=AuthCheckCache(),
    )

    outcome = run_prepared_target(prepared_target=prepared)

    assert outcome.target_result.error is None
    assert provider.seen_preparation is provider.preparation


def test_run_prepared_target_converts_provider_resolution_errors():
    provider = _Provider(fail_resolution=True)
    target = _target()
    prepared = PreparedTarget(
        index=0,
        provider=provider,
        effective_target=target,
        auth_result=SimpleNamespace(status=ExecutionStatus.SUCCESS),
        context=ExecutionContext(
            regions=["global"], dry_run=False, tasks=[], metadata={}
        ),
    )

    outcome = run_prepared_target(prepared_target=prepared)

    assert outcome.target_result.error == "resolution failed"
    assert outcome.target_result.entities == []


def test_scheduler_skips_targets_with_overlapping_provider_keys():
    target = _target()
    context = ExecutionContext(regions=["global"], dry_run=False, tasks=[], metadata={})
    blocked = PreparedTarget(
        index=0,
        provider=_Provider(),
        effective_target=target,
        auth_result=SimpleNamespace(),
        context=context,
        exclusive_execution_keys=(("test", "shared"),),
    )
    eligible = PreparedTarget(
        index=1,
        provider=_Provider(),
        effective_target=_target(name="target-b"),
        auth_result=SimpleNamespace(),
        context=context,
        exclusive_execution_keys=(("test", "other"),),
    )
    pending = deque([blocked, eligible])

    selected = _next_eligible_target(
        pending=pending, active_execution_keys={("test", "shared")}
    )

    assert selected is eligible
    assert list(pending) == [blocked]


def test_single_flight_cache_shares_concurrent_work():
    cache = _SingleFlightCache()
    started = threading.Event()
    release = threading.Event()
    calls = 0
    results = []

    def create():
        nonlocal calls
        calls += 1
        started.set()
        assert release.wait(timeout=1.0)
        return "value"

    def lookup():
        results.append(cache.get_or_create(key="shared", create=create))

    threads = [threading.Thread(target=lookup) for _ in range(2)]
    for thread in threads:
        thread.start()
    assert started.wait(timeout=1.0)
    release.set()
    for thread in threads:
        thread.join(timeout=1.0)

    assert calls == 1
    assert sorted(results) == [("value", False, False), ("value", True, True)]


def test_single_flight_cache_releases_waiters_after_error():
    cache = _SingleFlightCache()
    started = threading.Event()
    release = threading.Event()
    errors = []

    def create():
        started.set()
        assert release.wait(timeout=1.0)
        raise ValueError("discovery failed")

    def lookup():
        try:
            cache.get_or_create(key="shared", create=create)
        except ValueError as error:
            errors.append(str(error))

    threads = [threading.Thread(target=lookup) for _ in range(2)]
    for thread in threads:
        thread.start()
    assert started.wait(timeout=1.0)
    release.set()
    for thread in threads:
        thread.join(timeout=1.0)

    assert errors == ["discovery failed", "discovery failed"]


def test_fail_fast_does_not_start_pending_execution_targets(monkeypatch):
    calls = []

    def execute(**kwargs):
        execution_target = kwargs["execution_target"]
        calls.append(execution_target.id)
        if execution_target.id != "first":
            raise AssertionError("pending target started after fail-fast")
        return EntityResult(
            id="first",
            name="first",
            type="resource",
            provider="test",
            metadata={},
            status=ExecutionStatus.ERROR,
            started_at="2026-01-01T00:00:00+00:00",
            ended_at="2026-01-01T00:00:01+00:00",
            duration_seconds=1.0,
            tasks=[],
            error="failed",
        )

    monkeypatch.setattr("anvil.runner._execute_provider_execution_target", execute)
    target = _target(fail_fast=True, max_workers=1)
    context = ExecutionContext(
        regions=["global"], dry_run=False, tasks=[], metadata={}, fail_fast=True
    )

    result = _execute_provider_targets(
        provider=_Provider(),
        target=target,
        context=context,
        execution_targets=[
            ExecutionTarget(
                id="first",
                name="first",
                type="resource",
                provider="test",
                regions=["global"],
            ),
            ExecutionTarget(
                id="second",
                name="second",
                type="resource",
                provider="test",
                regions=["global"],
            ),
        ],
        benchmark_data=None,
    )

    assert calls == ["first"]
    assert result.entities[0].status is ExecutionStatus.ERROR
