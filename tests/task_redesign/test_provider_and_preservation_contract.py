from __future__ import annotations

import inspect
import importlib
import threading
import time
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from anvil.descriptors import TargetDescriptor
from anvil.execution_context import ExecutionContext
from anvil.providers.aws.provider import AwsPreflightData, AwsProvider
from anvil.providers.base import (
    ExecutionTarget,
    ProviderAuthResult,
    ProviderExecutionPlan,
    ProviderMetadata,
    ProviderPreparation,
)
from anvil.results import AuthResult, ExecutionStatus
from anvil.runner import (
    AuthCheckCache,
    PreparedTarget,
    _SingleFlightCache,
    _execute_provider_execution_target,
    _execute_provider_targets,
    prepare_target,
    run_prepared_target,
)
from anvil.task_loader import ResolvedTask, TaskScope, discover_tasks


@dataclass
class _BaseSession:
    profile_name: str | None = "management"


class _SessionFactory:
    pass


def _aws_preflight() -> AwsPreflightData:
    return AwsPreflightData(
        session_factory=_SessionFactory(),
        base_session=_BaseSession(),
        organization_id="o-contract",
        management_account_id="111111111111",
        base_session_account_id="111111111111",
        discovered_accounts={
            "111111111111": {
                "account_number": "111111111111",
                "account_alias": "management",
            },
            "222222222222": {
                "account_number": "222222222222",
                "account_alias": "member",
            },
        },
        region_statuses={"us-east-1": "ENABLED_BY_DEFAULT"},
    )


def test_aws_configured_target_uses_management_identity_when_excluded() -> None:
    provider = AwsProvider()
    target = TargetDescriptor(
        name="organization",
        provider="aws",
        mode="organization",
        exclude=["111111111111"],
        regions=["us-east-1"],
    )

    plan = provider.resolve_execution_targets(
        target=target,
        regions=["us-east-1"],
        include=target.include,
        exclude=target.exclude,
        preparation=_aws_preflight(),
    )

    assert [entity.id for entity in plan.execution_targets] == ["222222222222"]
    assert plan.configured_target is not None
    assert plan.configured_target.id == "111111111111"
    assert plan.configured_target.name == "management"
    assert plan.configured_target.type == "configured_target"
    assert plan.configured_target.regions == ["us-east-1"]


def test_aws_rejects_ambiguous_configured_target_before_runtime() -> None:
    provider = AwsProvider()
    target = TargetDescriptor(
        name="ambiguous",
        provider="aws",
        mode="accounts",
        provider_options={"role_name": "OrganizationAccountAccessRole"},
        include=["111111111111", "222222222222"],
        tasks=[{"name": "organization_task"}],
    )

    with pytest.raises(ValueError, match=r"ambiguous.*organization_task|no single"):
        provider.validate_task_configuration(
            target=target, task_scopes={"organization_task": "configured_target"}
        )


def test_aws_declares_configured_target_capability() -> None:
    assert AwsProvider.metadata.supported_task_scopes == frozenset(
        {"configured_target", "region"}
    )


def test_aws_ambiguous_configured_target_stops_before_auth_and_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    provider = AwsProvider()
    monkeypatch.setattr("anvil.runner._load_provider", lambda provider_name: provider)
    monkeypatch.setattr(
        "anvil.runner.resolve_tasks",
        lambda **kwargs: SimpleNamespace(
            ordered=[
                SimpleNamespace(
                    id="organization_task",
                    name="organization_task",
                    scope=TaskScope.CONFIGURED_TARGET,
                )
            ]
        ),
    )
    monkeypatch.setattr(provider, "auth_check", lambda target: events.append("auth"))
    monkeypatch.setattr(
        provider, "prepare_target", lambda **kwargs: events.append("prepare")
    )
    target = TargetDescriptor(
        name="ambiguous",
        provider="aws",
        mode="accounts",
        provider_options={"role_name": "OrganizationAccountAccessRole"},
        include=["111111111111", "222222222222"],
        regions=["us-east-1"],
        tasks=[{"name": "organization_task"}],
    )

    prepared = prepare_target(
        index=0,
        target=target,
        cli_dry_run=None,
        cli_include=None,
        cli_exclude=None,
        preparation_cache=_SingleFlightCache(),
        auth_cache=AuthCheckCache(),
    )

    assert events == []
    assert prepared.context is None
    assert prepared.auth_result.status is ExecutionStatus.ERROR
    assert "ambiguous" in (prepared.auth_result.message or "")
    assert "organization_task" in (prepared.auth_result.message or "")


class _OfflineValidationProvider:
    metadata = ProviderMetadata(
        name="fake",
        display_name="Fake",
        supported_task_scopes=frozenset({"configured_target", "region"}),
        default_regions=("region-a",),
    )

    def __init__(self, events: list[str]) -> None:
        self.events = events

    def validate_target(self, target) -> None:
        self.events.append("validate_target")

    def validate_task_configuration(self, *, target, task_scopes) -> None:
        self.events.append("validate_task_configuration")
        raise ValueError("configured-target identity is ambiguous")

    def resolve_target_filters(self, *, target, include_override, exclude_override):
        return target.include, target.exclude

    def auth_cache_key(self, target):
        return None

    def auth_check(self, target) -> ProviderAuthResult:
        self.events.append("auth")
        return ProviderAuthResult(status=ExecutionStatus.SUCCESS, source="fake")

    def prepare_target(self, **kwargs) -> ProviderPreparation:
        self.events.append("prepare")
        return ProviderPreparation()


def test_configured_target_ambiguity_is_rejected_before_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    provider = _OfflineValidationProvider(events)
    monkeypatch.setattr("anvil.runner._load_provider", lambda provider_name: provider)
    monkeypatch.setattr(
        "anvil.runner.resolve_tasks",
        lambda **kwargs: SimpleNamespace(
            ordered=[
                SimpleNamespace(
                    id="configured_task",
                    name="configured_task",
                    scope=TaskScope.CONFIGURED_TARGET,
                )
            ]
        ),
    )
    target = TargetDescriptor(
        name="ambiguous",
        provider="fake",
        mode="resources",
        regions=["region-a"],
        tasks=[{"name": "configured_task"}],
    )

    prepared = prepare_target(
        index=0,
        target=target,
        cli_dry_run=None,
        cli_include=None,
        cli_exclude=None,
        preparation_cache=_SingleFlightCache(),
        auth_cache=AuthCheckCache(),
    )

    assert events == ["validate_target", "validate_task_configuration"]
    assert prepared.context is None
    assert prepared.auth_result.status is ExecutionStatus.ERROR
    assert "ambiguous" in (prepared.auth_result.message or "")


def test_unsupported_scope_resolution_fails_before_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    provider = _OfflineValidationProvider(events)
    monkeypatch.setattr("anvil.runner._load_provider", lambda provider_name: provider)

    def reject_unsupported_scope(**kwargs):
        from anvil.task_loader import TaskConfigError

        raise TaskConfigError(
            "Provider 'fake' does not support task scope 'configured_target'"
        )

    monkeypatch.setattr("anvil.runner.resolve_tasks", reject_unsupported_scope)
    target = TargetDescriptor(
        name="unsupported",
        provider="fake",
        mode="resources",
        regions=["region-a"],
        tasks=[{"name": "configured_task"}],
    )

    prepared = prepare_target(
        index=0,
        target=target,
        cli_dry_run=None,
        cli_include=None,
        cli_exclude=None,
        preparation_cache=_SingleFlightCache(),
        auth_cache=AuthCheckCache(),
    )

    assert events == ["validate_target"]
    assert prepared.context is None
    assert prepared.auth_result.status is ExecutionStatus.ERROR
    assert "does not support" in (prepared.auth_result.message or "")


def test_ordinary_configuration_does_not_call_configured_validation_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    provider = _OfflineValidationProvider(events)
    monkeypatch.setattr("anvil.runner._load_provider", lambda provider_name: provider)
    monkeypatch.setattr(
        "anvil.runner.resolve_tasks",
        lambda **kwargs: SimpleNamespace(
            ordered=[
                SimpleNamespace(
                    id="ordinary_task", name="ordinary_task", scope=TaskScope.REGION
                )
            ]
        ),
    )
    target = TargetDescriptor(
        name="ordinary",
        provider="fake",
        mode="resources",
        regions=["region-a"],
        tasks=[{"name": "ordinary_task"}],
    )

    prepared = prepare_target(
        index=0,
        target=target,
        cli_dry_run=None,
        cli_include=None,
        cli_exclude=None,
        preparation_cache=_SingleFlightCache(),
        auth_cache=AuthCheckCache(),
    )

    assert events == ["validate_target", "auth", "prepare"]
    assert prepared.context is not None


class _LifecycleRuntime:
    def __init__(self, calls: list[tuple[str, str]], target_id: str) -> None:
        self.calls = calls
        self.target_id = target_id
        self.closed = False

    def build_session(self, *, region: str) -> object:
        self.calls.append(("session", region))
        return {"target_id": self.target_id, "region": region}

    def record_region_outcome(
        self, *, region: str, duration_seconds: float, failed: bool, interrupted: bool
    ) -> None:
        self.calls.append(("outcome", region))

    def close(self) -> None:
        self.calls.append(("close", ""))
        self.closed = True

    @property
    def benchmark(self) -> dict[str, object]:
        if self.closed:
            raise RuntimeError("benchmark accessed after runtime close")
        return {"access_strategy": f"runtime-{self.target_id}"}


class _LifecycleProvider:
    metadata = ProviderMetadata(
        name="fake",
        display_name="Fake",
        supported_task_scopes=frozenset({"configured_target", "region", "target"}),
    )

    def __init__(self, calls: list[tuple[str, str]]) -> None:
        self.calls = calls

    def prepare_execution_runtime(self, **kwargs) -> _LifecycleRuntime:
        self.calls.append(("runtime", kwargs["execution_target"].id))
        return _LifecycleRuntime(self.calls, kwargs["execution_target"].id)

    def prepare_configured_target_runtime(self, **kwargs) -> _LifecycleRuntime:
        self.calls.append(("configured_runtime", kwargs["execution_target"].id))
        return _LifecycleRuntime(self.calls, kwargs["execution_target"].id)


class _PlanLifecycleProvider(_LifecycleProvider):
    def __init__(
        self,
        calls: list[tuple[str, str]],
        *,
        execution_targets: list[ExecutionTarget],
        configured_target: ExecutionTarget,
    ) -> None:
        super().__init__(calls)
        self.execution_targets = execution_targets
        self.configured_target = configured_target

    def resolve_execution_targets(self, **kwargs) -> ProviderExecutionPlan:
        return ProviderExecutionPlan(
            execution_targets=self.execution_targets,
            configured_target=self.configured_target,
        )


def _execution_target(regions: list[str]) -> ExecutionTarget:
    return ExecutionTarget(
        id="entity-a",
        name="Entity A",
        type="resource",
        provider="fake",
        regions=regions,
    )


def _context(
    tasks: list[ResolvedTask], *, max_parallel_regions: int = 1
) -> ExecutionContext:
    return ExecutionContext(
        regions=["region-a", "region-b"],
        dry_run=False,
        tasks=tasks,
        metadata={},
        max_parallel_regions=max_parallel_regions,
    )


def _target(
    tasks: list[dict[str, object]], *, max_workers: int = 10
) -> TargetDescriptor:
    return TargetDescriptor(
        name="lifecycle",
        provider="fake",
        mode="resources",
        regions=["region-a", "region-b"],
        tasks=tasks,
        max_workers=max_workers,
    )


def _baseline_task(*, name: str, run) -> ResolvedTask:
    return ResolvedTask(name=name, run=run, depends_on=[], scope=TaskScope.REGION)


def _contract_task(
    *,
    task_id: str,
    name: str,
    run,
    depends_on: list[str] | None = None,
    always_run: bool = False,
    dependency_data: dict[str, dict[str, str]] | None = None,
    scope: str = "region",
) -> ResolvedTask:
    parameters = inspect.signature(ResolvedTask).parameters
    for field_name in ("id", "always_run", "metadata", "dependency_data"):
        assert field_name in parameters

    kwargs = {
        "id": task_id,
        "name": name,
        "run": run,
        "depends_on": depends_on or [],
        "always_run": always_run,
        "metadata": {},
        "dependency_data": dependency_data or {},
        "scope": TaskScope(scope),
    }
    return ResolvedTask(**kwargs)


def test_preserves_existing_empty_task_lifecycle() -> None:
    calls: list[tuple[str, str]] = []

    result = _execute_provider_execution_target(
        provider=_LifecycleProvider(calls),
        target=_target([]),
        execution_target=_execution_target(["region-a", "region-b"]),
        context=_context([]),
    )

    assert result.status is ExecutionStatus.SUCCESS
    assert calls == [
        ("runtime", "entity-a"),
        ("session", "region-a"),
        ("outcome", "region-a"),
        ("session", "region-b"),
        ("outcome", "region-b"),
        ("close", ""),
    ]


def test_preserves_runtime_session_reuse_boundary_and_action_isolation() -> None:
    calls: list[tuple[str, str]] = []
    seen_actions: list[list[str]] = []

    def run(**kwargs):
        seen_actions.append(list(kwargs["actions"].actions))
        kwargs["actions"].record(kwargs["region"])
        return {"region": kwargs["region"]}

    task = _baseline_task(name="scan", run=run)
    result = _execute_provider_execution_target(
        provider=_LifecycleProvider(calls),
        target=_target([{"name": "scan"}]),
        execution_target=_execution_target(["region-a", "region-b"]),
        context=_context([task]),
    )

    assert result.status is ExecutionStatus.SUCCESS
    assert [call for call in calls if call[0] == "runtime"] == [("runtime", "entity-a")]
    assert [call for call in calls if call[0] == "session"] == [
        ("session", "region-a"),
        ("session", "region-b"),
    ]
    assert seen_actions == [[], []]
    assert [task_result.actions for task_result in result.tasks] == [
        ["region-a"],
        ["region-b"],
    ]
    assert calls == [
        ("runtime", "entity-a"),
        ("session", "region-a"),
        ("outcome", "region-a"),
        ("session", "region-b"),
        ("outcome", "region-b"),
        ("close", ""),
    ]


def test_preserves_task_discovery_without_import_time_execution() -> None:
    discovery = discover_tasks()

    assert any(task.name == "noop" for task in discovery.tasks)
    assert any(task.name == "count_vpc" for task in discovery.tasks)
    assert not discovery.issues


def test_fail_fast_settles_unstarted_nodes_and_preserves_root_error() -> None:
    def fail(**kwargs):
        raise RuntimeError(f"failed {kwargs['region']}")

    context = _context([_contract_task(task_id="scan", name="scan", run=fail)])
    object.__setattr__(context, "fail_fast", True)
    result = _execute_provider_execution_target(
        provider=_LifecycleProvider([]),
        target=_target([{"name": "scan"}]),
        execution_target=_execution_target(["region-a", "region-b"]),
        context=context,
    )

    assert [(task.status.value, task.skip_reason) for task in result.tasks] == [
        ("error", None),
        ("skipped", "fail_fast"),
    ]
    assert result.status is ExecutionStatus.ERROR


def test_finalizer_activation_uses_same_transitive_dependency_gating() -> None:
    finalizer_calls: list[str] = []

    def fail(**kwargs):
        raise RuntimeError("root failure")

    def blocked(**kwargs):
        raise AssertionError("blocked task must not execute")

    def finalize(**kwargs):
        finalizer_calls.append(kwargs["execution_target_id"])
        return {"restored": True}

    tasks = [
        _contract_task(task_id="producer", name="producer", run=fail),
        _contract_task(
            task_id="blocked", name="blocked", run=blocked, depends_on=["producer"]
        ),
        _contract_task(
            task_id="finalizer",
            name="finalizer",
            run=finalize,
            depends_on=["blocked"],
            always_run=True,
        ),
    ]
    result = _execute_provider_execution_target(
        provider=_LifecycleProvider([]),
        target=_target([]),
        execution_target=_execution_target(["region-a"]),
        context=_context(tasks),
    )

    assert finalizer_calls == ["entity-a"]
    assert [(task.task_id, task.status.value) for task in result.tasks] == [
        ("producer", "error"),
        ("blocked", "skipped"),
        ("finalizer", "success"),
    ]
    assert result.status is ExecutionStatus.ERROR


def test_cancellation_before_chain_start_does_not_activate_finalizer() -> None:
    finalizer_ran = False

    def finalize(**kwargs):
        nonlocal finalizer_ran
        finalizer_ran = True

    tasks = [
        _contract_task(task_id="work", name="work", run=lambda **kwargs: {}),
        _contract_task(
            task_id="finalizer",
            name="finalizer",
            run=finalize,
            depends_on=["work"],
            always_run=True,
        ),
    ]
    context = _context(tasks)
    context.cancel_event.set()
    result = _execute_provider_execution_target(
        provider=_LifecycleProvider([]),
        target=_target([]),
        execution_target=_execution_target(["region-a"]),
        context=context,
    )

    assert not finalizer_ran
    assert [(task.status.value, task.skip_reason) for task in result.tasks] == [
        ("skipped", "cancelled_before_start"),
        ("skipped", "cancelled_before_start"),
    ]


def test_missing_dependency_path_is_task_error_before_consumer_call() -> None:
    consumer_ran = False

    def consume(**kwargs):
        nonlocal consumer_ran
        consumer_ran = True

    tasks = [
        _contract_task(
            task_id="producer", name="producer", run=lambda **kwargs: {"present": True}
        ),
        _contract_task(
            task_id="consumer",
            name="consumer",
            run=consume,
            depends_on=["producer"],
            dependency_data={
                "missing": {"task_id": "producer", "path": "result.missing"}
            },
        ),
    ]
    result = _execute_provider_execution_target(
        provider=_LifecycleProvider([]),
        target=_target([]),
        execution_target=_execution_target(["region-a"]),
        context=_context(tasks),
    )

    assert not consumer_ran
    assert result.tasks[-1].status is ExecutionStatus.ERROR
    assert "missing" in (result.tasks[-1].error or "")


def test_failed_producer_partial_result_is_available_to_always_run_cleanup() -> None:
    received: list[object] = []

    def produce(**kwargs):
        task_error_module = importlib.import_module("anvil.task_errors")
        task_execution_error = task_error_module.TaskExecutionError
        raise task_execution_error(
            "mutation failed", partial_result={"attachments": ["partial"]}
        )

    def cleanup(**kwargs):
        received.append(kwargs["dependency_data"]["attachments"])
        return {"restored": True}

    tasks = [
        _contract_task(task_id="producer", name="producer", run=produce),
        _contract_task(
            task_id="cleanup",
            name="cleanup",
            run=cleanup,
            depends_on=["producer"],
            always_run=True,
            dependency_data={
                "attachments": {"task_id": "producer", "path": "result.attachments"}
            },
        ),
    ]
    result = _execute_provider_execution_target(
        provider=_LifecycleProvider([]),
        target=_target([]),
        execution_target=_execution_target(["region-a"]),
        context=_context(tasks),
    )

    assert received == [["partial"]]
    assert [task.status.value for task in result.tasks] == ["error", "success"]
    assert result.status is ExecutionStatus.ERROR


def test_configured_only_execution_does_not_prepare_ordinary_runtimes() -> None:
    calls: list[tuple[str, str]] = []
    callbacks: list[dict[str, object]] = []

    def configured(**kwargs):
        callbacks.append(
            {
                "id": kwargs["execution_target_id"],
                "name": kwargs["execution_target_name"],
                "type": kwargs["execution_target_type"],
                "region": kwargs["region"],
                "session_identity": kwargs["session"]["target_id"],
            }
        )
        return {"configured": True}

    configured_task = _contract_task(
        task_id="configured",
        name="configured",
        run=configured,
        scope="configured_target",
    )
    _execute_provider_targets(
        **{
            "provider": _LifecycleProvider(calls),
            "target": _target([{"name": "configured"}]),
            "context": _context([configured_task]),
            "execution_targets": [
                _execution_target(["region-a"]),
                ExecutionTarget(
                    id="entity-b",
                    name="Entity B",
                    type="resource",
                    provider="fake",
                    regions=["region-a"],
                ),
            ],
            "configured_execution_target": ExecutionTarget(
                id="configured-owner",
                name="Configured Owner",
                type="configured_target",
                provider="fake",
                regions=["region-a"],
            ),
            "benchmark_data": None,
        }
    )

    assert [call for call in calls if "runtime" in call[0]] == [
        ("configured_runtime", "configured-owner")
    ]
    assert [call for call in calls if call[0] == "session"] == [("session", "region-a")]
    assert [call for call in calls if call[0] == "close"] == [("close", "")]
    assert callbacks == [
        {
            "id": "configured-owner",
            "name": "Configured Owner",
            "type": "configured_target",
            "region": "region-a",
            "session_identity": "configured-owner",
        }
    ]


def test_runner_uses_configured_identity_from_provider_execution_plan() -> None:
    calls: list[tuple[str, str]] = []
    callback_ids: list[str] = []
    configured_task = _contract_task(
        task_id="configured",
        name="configured",
        run=lambda **kwargs: callback_ids.append(kwargs["execution_target_id"]),
        scope="configured_target",
    )
    execution_target = _execution_target(["region-a"])
    configured_target = ExecutionTarget(
        id="provider-owner",
        name="Provider Owner",
        type="configured_target",
        provider="fake",
        regions=["home-region"],
    )
    provider = _PlanLifecycleProvider(
        calls, execution_targets=[execution_target], configured_target=configured_target
    )
    target = _target([{"name": "configured"}])
    now_at = "2026-01-01T00:00:00+00:00"

    outcome = run_prepared_target(
        prepared_target=PreparedTarget(
            index=0,
            provider=provider,
            effective_target=target,
            auth_result=AuthResult(
                target_name=target.name,
                status=ExecutionStatus.SUCCESS,
                source="fake",
                started_at=now_at,
                ended_at=now_at,
                duration_seconds=0.0,
            ),
            context=_context([configured_task]),
        )
    )

    assert callback_ids == ["provider-owner"]
    assert [call for call in calls if "runtime" in call[0]] == [
        ("configured_runtime", "provider-owner")
    ]
    assert outcome.target_result.error is None


def test_ordinary_only_execution_does_not_prepare_configured_runtime() -> None:
    calls: list[tuple[str, str]] = []
    ordinary_task = _contract_task(
        task_id="ordinary",
        name="ordinary",
        run=lambda **kwargs: {"entity": kwargs["execution_target_id"]},
    )

    _execute_provider_targets(
        provider=_LifecycleProvider(calls),
        target=_target([{"name": "ordinary"}]),
        context=_context([ordinary_task]),
        execution_targets=[_execution_target(["region-a"])],
        configured_execution_target=ExecutionTarget(
            id="configured-owner",
            name="Configured Owner",
            type="configured_target",
            provider="fake",
            regions=["region-a"],
        ),
        benchmark_data=None,
    )

    assert [call for call in calls if "runtime" in call[0]] == [("runtime", "entity-a")]


def test_empty_tasks_keep_ordinary_lifecycle_with_configured_identity() -> None:
    calls: list[tuple[str, str]] = []

    _execute_provider_targets(
        provider=_LifecycleProvider(calls),
        target=_target([]),
        context=_context([]),
        execution_targets=[_execution_target(["region-a", "region-b"])],
        configured_execution_target=ExecutionTarget(
            id="configured-owner",
            name="Configured Owner",
            type="configured_target",
            provider="fake",
            regions=["region-a"],
        ),
        benchmark_data=None,
    )

    assert calls == [
        ("runtime", "entity-a"),
        ("session", "region-a"),
        ("outcome", "region-a"),
        ("session", "region-b"),
        ("outcome", "region-b"),
        ("close", ""),
    ]


def test_mixed_scope_runtime_and_session_construction_is_bounded() -> None:
    calls: list[tuple[str, str]] = []
    tasks = [
        _contract_task(
            task_id="configured",
            name="configured",
            run=lambda **kwargs: {},
            scope="configured_target",
        ),
        _contract_task(
            task_id="target_wide",
            name="target_wide",
            run=lambda **kwargs: {},
            scope="target",
        ),
        _contract_task(task_id="regional", name="regional", run=lambda **kwargs: {}),
    ]

    result = _execute_provider_targets(
        provider=_LifecycleProvider(calls),
        target=_target([], max_workers=2),
        context=_context(tasks, max_parallel_regions=2),
        execution_targets=[
            _execution_target(["region-a", "region-b"]),
            ExecutionTarget(
                id="entity-b",
                name="Entity B",
                type="resource",
                provider="fake",
                regions=["region-a", "region-b"],
            ),
        ],
        configured_execution_target=ExecutionTarget(
            id="configured-owner",
            name="Configured Owner",
            type="configured_target",
            provider="fake",
            regions=["region-a"],
        ),
        benchmark_data=None,
    )

    assert sorted(call for call in calls if "runtime" in call[0]) == [
        ("configured_runtime", "configured-owner"),
        ("runtime", "entity-a"),
        ("runtime", "entity-b"),
    ]
    assert len([call for call in calls if call[0] == "session"]) == 5
    assert len([call for call in calls if call[0] == "outcome"]) == 5
    assert len([call for call in calls if call[0] == "close"]) == 3
    assert [entity.id for entity in result.entities] == ["entity-a", "entity-b"]
    assert [task.task_id for task in result.tasks] == ["configured"]


def test_mixed_graph_records_outcome_before_next_sequential_region_session() -> None:
    calls: list[tuple[str, str]] = []
    tasks = [
        _contract_task(
            task_id="regional_first",
            name="regional_first",
            run=lambda **kwargs: {"region": kwargs["region"]},
        ),
        _contract_task(
            task_id="regional_second",
            name="regional_second",
            run=lambda **kwargs: {"region": kwargs["region"]},
            depends_on=["regional_first"],
        ),
        _contract_task(
            task_id="configured",
            name="configured",
            run=lambda **kwargs: {},
            depends_on=["regional_second"],
            scope="configured_target",
        ),
    ]

    result = _execute_provider_targets(
        provider=_LifecycleProvider(calls),
        target=_target([], max_workers=1),
        context=_context(tasks, max_parallel_regions=1),
        execution_targets=[_execution_target(["region-a", "region-b"])],
        configured_execution_target=ExecutionTarget(
            id="configured-owner",
            name="Configured Owner",
            type="configured_target",
            provider="fake",
            regions=["region-a"],
        ),
        benchmark_data=None,
    )

    first_outcome_index = calls.index(("outcome", "region-a"))
    second_region_session_index = calls.index(("session", "region-b"))
    assert first_outcome_index < second_region_session_index
    assert result.entities[0].benchmark is not None
    assert result.entities[0].benchmark["access_strategy"] == "runtime-entity-a"
    assert set(result.entities[0].benchmark["regions"]) == {"region-a", "region-b"}


def test_dependent_configured_task_waits_for_upstream_runtime_outcome() -> None:
    calls: list[tuple[str, str]] = []
    ordinary_outcome_completed = threading.Event()
    configured_saw_completed_outcome: list[bool] = []

    class DelayedOutcomeRuntime(_LifecycleRuntime):
        def record_region_outcome(
            self,
            *,
            region: str,
            duration_seconds: float,
            failed: bool,
            interrupted: bool,
        ) -> None:
            time.sleep(0.03)
            super().record_region_outcome(
                region=region,
                duration_seconds=duration_seconds,
                failed=failed,
                interrupted=interrupted,
            )
            ordinary_outcome_completed.set()

    class DelayedOutcomeProvider(_LifecycleProvider):
        def prepare_execution_runtime(self, **kwargs) -> _LifecycleRuntime:
            execution_target = kwargs["execution_target"]
            self.calls.append(("runtime", execution_target.id))
            return DelayedOutcomeRuntime(self.calls, execution_target.id)

    def configured(**kwargs) -> dict[str, object]:
        configured_saw_completed_outcome.append(ordinary_outcome_completed.is_set())
        return {}

    tasks = [
        _contract_task(task_id="regional", name="regional", run=lambda **kwargs: {}),
        _contract_task(
            task_id="configured",
            name="configured",
            run=configured,
            depends_on=["regional"],
            scope="configured_target",
        ),
    ]

    _execute_provider_targets(
        provider=DelayedOutcomeProvider(calls),
        target=_target([], max_workers=1),
        context=_context(tasks, max_parallel_regions=1),
        execution_targets=[_execution_target(["region-a"])],
        configured_execution_target=ExecutionTarget(
            id="configured-owner",
            name="Configured Owner",
            type="configured_target",
            provider="fake",
            regions=["region-a"],
        ),
        benchmark_data=None,
    )

    assert configured_saw_completed_outcome == [True]


def test_mixed_graph_surfaces_outcome_hook_errors_and_closes_runtimes() -> None:
    calls: list[tuple[str, str]] = []

    class FailingOutcomeRuntime(_LifecycleRuntime):
        def record_region_outcome(
            self,
            *,
            region: str,
            duration_seconds: float,
            failed: bool,
            interrupted: bool,
        ) -> None:
            raise RuntimeError(f"outcome failed for {self.target_id}")

    class FailingOutcomeProvider(_LifecycleProvider):
        def prepare_execution_runtime(self, **kwargs) -> _LifecycleRuntime:
            execution_target = kwargs["execution_target"]
            self.calls.append(("runtime", execution_target.id))
            return FailingOutcomeRuntime(self.calls, execution_target.id)

    tasks = [
        _contract_task(task_id="regional", name="regional", run=lambda **kwargs: {}),
        _contract_task(
            task_id="configured",
            name="configured",
            run=lambda **kwargs: {},
            depends_on=["regional"],
            scope="configured_target",
        ),
    ]

    with pytest.raises(RuntimeError, match="outcome failed for entity-a"):
        _execute_provider_targets(
            provider=FailingOutcomeProvider(calls),
            target=_target([], max_workers=1),
            context=_context(tasks, max_parallel_regions=1),
            execution_targets=[_execution_target(["region-a"])],
            configured_execution_target=ExecutionTarget(
                id="configured-owner",
                name="Configured Owner",
                type="configured_target",
                provider="fake",
                regions=["region-a"],
            ),
            benchmark_data=None,
        )

    assert ("close", "") in calls


def test_mixed_target_only_benchmark_does_not_report_regional_execution() -> None:
    tasks = [
        _contract_task(
            task_id="target_wide",
            name="target_wide",
            run=lambda **kwargs: {"target": kwargs["execution_target_id"]},
            scope="target",
        ),
        _contract_task(
            task_id="configured",
            name="configured",
            run=lambda **kwargs: {},
            depends_on=["target_wide"],
            scope="configured_target",
        ),
    ]

    result = _execute_provider_targets(
        provider=_LifecycleProvider([]),
        target=_target([], max_workers=1),
        context=_context(tasks, max_parallel_regions=1),
        execution_targets=[_execution_target(["region-a", "region-b"])],
        configured_execution_target=ExecutionTarget(
            id="configured-owner",
            name="Configured Owner",
            type="configured_target",
            provider="fake",
            regions=["region-a"],
        ),
        benchmark_data=None,
    )

    benchmark = result.entities[0].benchmark
    assert benchmark is not None
    assert benchmark["target"]["task_count"] == 1
    assert benchmark["regions"] == {}
    assert benchmark["region_execution_seconds"] == 0.0


def test_repeated_component_result_order_uses_invocation_ids() -> None:
    tasks = [
        _contract_task(
            task_id="inventory_before",
            name="inventory",
            run=lambda **kwargs: {"stage": "before"},
        ),
        _contract_task(
            task_id="inventory_after",
            name="inventory",
            run=lambda **kwargs: {"stage": "after"},
            depends_on=["inventory_before"],
        ),
    ]

    result = _execute_provider_targets(
        provider=_LifecycleProvider([]),
        target=_target([]),
        context=_context(tasks, max_parallel_regions=2),
        execution_targets=[_execution_target(["region-a", "region-b"])],
        benchmark_data=None,
    )

    assert [(task.region, task.task_id) for task in result.entities[0].tasks] == [
        ("region-a", "inventory_before"),
        ("region-a", "inventory_after"),
        ("region-b", "inventory_before"),
        ("region-b", "inventory_after"),
    ]


def test_configured_graph_preserves_bounded_target_concurrency() -> None:
    lock = threading.Lock()
    active_targets: set[str] = set()
    max_active_targets = 0

    def regional(**kwargs):
        nonlocal max_active_targets
        target_id = kwargs["execution_target_id"]
        with lock:
            active_targets.add(target_id)
            max_active_targets = max(max_active_targets, len(active_targets))
        time.sleep(0.03)
        with lock:
            active_targets.remove(target_id)

    tasks = [
        _contract_task(task_id="regional", name="regional", run=regional),
        _contract_task(
            task_id="configured",
            name="configured",
            run=lambda **kwargs: {},
            scope="configured_target",
        ),
    ]
    _execute_provider_targets(
        provider=_LifecycleProvider([]),
        target=_target([], max_workers=1),
        context=_context(tasks, max_parallel_regions=2),
        execution_targets=[
            _execution_target(["region-a"]),
            ExecutionTarget(
                id="entity-b",
                name="Entity B",
                type="resource",
                provider="fake",
                regions=["region-a"],
            ),
        ],
        configured_execution_target=ExecutionTarget(
            id="configured-owner",
            name="Configured Owner",
            type="configured_target",
            provider="fake",
            regions=["region-a"],
        ),
        benchmark_data=None,
    )

    assert max_active_targets == 1


def test_configured_graph_preserves_parallel_regions_within_target_limit() -> None:
    lock = threading.Lock()
    active_regions = 0
    max_active_regions = 0

    def regional(**kwargs):
        nonlocal active_regions, max_active_regions
        with lock:
            active_regions += 1
            max_active_regions = max(max_active_regions, active_regions)
        time.sleep(0.03)
        with lock:
            active_regions -= 1

    tasks = [
        _contract_task(task_id="regional", name="regional", run=regional),
        _contract_task(
            task_id="configured",
            name="configured",
            run=lambda **kwargs: {},
            scope="configured_target",
        ),
    ]
    _execute_provider_targets(
        provider=_LifecycleProvider([]),
        target=_target([], max_workers=1),
        context=_context(tasks, max_parallel_regions=2),
        execution_targets=[_execution_target(["region-a", "region-b"])],
        configured_execution_target=ExecutionTarget(
            id="configured-owner",
            name="Configured Owner",
            type="configured_target",
            provider="fake",
            regions=["region-a"],
        ),
        benchmark_data=None,
    )

    assert max_active_regions == 2


def test_configured_graph_honors_single_region_limit_per_target() -> None:
    lock = threading.Lock()
    active_regions = 0
    max_active_regions = 0

    def regional(**kwargs):
        nonlocal active_regions, max_active_regions
        with lock:
            active_regions += 1
            max_active_regions = max(max_active_regions, active_regions)
        time.sleep(0.03)
        with lock:
            active_regions -= 1

    tasks = [
        _contract_task(task_id="regional", name="regional", run=regional),
        _contract_task(
            task_id="configured",
            name="configured",
            run=lambda **kwargs: {},
            scope="configured_target",
        ),
    ]
    _execute_provider_targets(
        provider=_LifecycleProvider([]),
        target=_target([], max_workers=2),
        context=_context(tasks, max_parallel_regions=1),
        execution_targets=[_execution_target(["region-a", "region-b"])],
        configured_execution_target=ExecutionTarget(
            id="configured-owner",
            name="Configured Owner",
            type="configured_target",
            provider="fake",
            regions=["region-a"],
        ),
        benchmark_data=None,
    )

    assert max_active_regions == 1


def test_configured_fan_in_dependency_order_uses_target_then_region_order() -> None:
    received: list[object] = []

    def regional(**kwargs):
        if (
            kwargs["execution_target_id"] == "entity-a"
            and kwargs["region"] == "region-a"
        ):
            time.sleep(0.03)
        return f"{kwargs['execution_target_id']}:{kwargs['region']}"

    def configured(**kwargs):
        received.append(kwargs["dependency_data"]["values"])

    tasks = [
        _contract_task(task_id="regional", name="regional", run=regional),
        _contract_task(
            task_id="configured",
            name="configured",
            run=configured,
            depends_on=["regional"],
            dependency_data={"values": {"task_id": "regional", "path": "result"}},
            scope="configured_target",
        ),
    ]
    _execute_provider_targets(
        **{
            "provider": _LifecycleProvider([]),
            "target": _target([]),
            "context": _context(tasks, max_parallel_regions=2),
            "execution_targets": [
                _execution_target(["region-a", "region-b"]),
                ExecutionTarget(
                    id="entity-b",
                    name="Entity B",
                    type="resource",
                    provider="fake",
                    regions=["region-a", "region-b"],
                ),
            ],
            "configured_execution_target": ExecutionTarget(
                id="configured-owner",
                name="Configured Owner",
                type="configured_target",
                provider="fake",
                regions=["region-a"],
            ),
            "benchmark_data": None,
        }
    )

    assert received == [
        [
            "entity-a:region-a",
            "entity-a:region-b",
            "entity-b:region-a",
            "entity-b:region-b",
        ]
    ]
