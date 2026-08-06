from __future__ import annotations

import inspect

from anvil.result_query import (
    ResultFilters,
    build_jsonl_records_for_target,
    failure_records,
    filter_records,
)
from anvil.results import (
    EngineResult,
    EngineState,
    EntityResult,
    ExecutionStatus,
    TargetResult,
    TaskResult,
)
from anvil.task_context import TaskCallContext


def _task_result(
    *,
    task_id: str,
    task_name: str,
    status: ExecutionStatus,
    result: object | None = None,
    error: str | None = None,
    skip_reason: str | None = None,
) -> TaskResult:
    required_fields = inspect.signature(TaskResult).parameters
    assert "task_id" in required_fields
    assert "skip_reason" in required_fields
    kwargs = {
        "task_id": task_id,
        "task_name": task_name,
        "region": "us-east-1",
        "status": status,
        "started_at": "2026-07-28T00:00:00+00:00",
        "ended_at": "2026-07-28T00:00:01+00:00",
        "duration_seconds": 1.0,
        "result": result,
        "error": error,
        "skip_reason": skip_reason,
    }
    return TaskResult(**kwargs)


def _entity(status: ExecutionStatus, tasks: list[TaskResult]) -> EntityResult:
    return EntityResult(
        id="111111111111",
        name="account",
        type="account",
        provider="aws",
        metadata={},
        status=status,
        tasks=tasks,
        started_at="2026-07-28T00:00:00+00:00",
        ended_at="2026-07-28T00:00:01+00:00",
        duration_seconds=1.0,
    )


def test_task_call_contract_has_separate_dependency_data() -> None:
    assert TaskCallContext.keyword_names() == frozenset(
        {
            "provider",
            "execution_target_id",
            "execution_target_name",
            "execution_target_type",
            "region",
            "session",
            "dry_run",
            "metadata",
            "dependency_data",
            "actions",
        }
    )


def test_task_context_deeply_isolates_metadata_and_dependency_data() -> None:
    parameters = inspect.signature(TaskCallContext).parameters
    assert "dependency_data" in parameters

    metadata = {"nested": {"items": ["original"]}}
    dependency_data = {"payload": {"items": ["original"]}}
    common_kwargs = {
        "provider": "aws",
        "execution_target_type": "account",
        "session": object(),
        "dry_run": False,
        "metadata": metadata,
        "dependency_data": dependency_data,
        "actions": object(),
    }
    first = TaskCallContext(
        **common_kwargs,
        execution_target_id="111111111111",
        execution_target_name="account",
        region="us-east-1",
    ).to_kwargs()
    second = TaskCallContext(
        **common_kwargs,
        execution_target_id="222222222222",
        execution_target_name="other",
        region="us-west-2",
    ).to_kwargs()

    first["metadata"]["nested"]["items"].append("mutated")
    first["dependency_data"]["payload"]["items"].append("mutated")

    assert second["metadata"] == {"nested": {"items": ["original"]}}
    assert second["dependency_data"] == {"payload": {"items": ["original"]}}
    assert metadata == {"nested": {"items": ["original"]}}
    assert dependency_data == {"payload": {"items": ["original"]}}


def test_skipped_is_neutral_and_not_unsuccessful() -> None:
    skipped = ExecutionStatus("skipped")

    assert skipped.is_skipped
    assert not skipped.is_error
    assert not skipped.is_interrupted
    assert not skipped.is_unsuccessful


def test_task_result_serializes_invocation_id_and_component_name() -> None:
    payload = _task_result(
        task_id="detach_guardrails",
        task_name="reconcile_config_guardrails",
        status=ExecutionStatus.SUCCESS,
    ).to_dict()

    assert payload["task_id"] == "detach_guardrails"
    assert payload["task_name"] == "reconcile_config_guardrails"
    assert "task" not in payload


def test_failed_queries_exclude_skipped_tasks() -> None:
    records = [
        {"record_type": "task", "status": "skipped", "task_id": "blocked"},
        {"record_type": "task", "status": "error", "task_id": "failed"},
    ]

    assert failure_records(records) == [records[1]]
    assert filter_records(records, filters=ResultFilters(status="failed")) == [
        records[1]
    ]


def test_success_plus_skipped_aggregates_to_success_and_counts_skip() -> None:
    skipped = ExecutionStatus("skipped")
    tasks = [
        _task_result(
            task_id="successful", task_name="noop", status=ExecutionStatus.SUCCESS
        ),
        _task_result(
            task_id="blocked",
            task_name="noop",
            status=skipped,
            skip_reason="dependency_unsuccessful",
        ),
    ]
    target = TargetResult.create(
        target_name="target",
        provider="aws",
        dry_run=False,
        entities=[_entity(ExecutionStatus.SUCCESS, tasks)],
    )
    engine = EngineResult.create(
        state=EngineState.COMPLETED_SUCCESS, auth_results=[], target_results=[target]
    )

    assert not target.has_failures
    summary = engine.build_summary()
    assert summary["total_skipped_tasks"] == 1
    assert summary["total_interrupted_tasks"] == 0
    assert engine.state is EngineState.COMPLETED_SUCCESS


def test_successful_finalizer_does_not_erase_upstream_failure() -> None:
    tasks = [
        _task_result(
            task_id="mutate",
            task_name="mutate",
            status=ExecutionStatus.ERROR,
            result={"changed": ["resource-a"]},
            error="mutation failed",
        ),
        _task_result(
            task_id="restore", task_name="restore", status=ExecutionStatus.SUCCESS
        ),
    ]
    target = TargetResult.create(
        target_name="target",
        provider="aws",
        dry_run=False,
        entities=[_entity(ExecutionStatus.ERROR, tasks)],
    )

    assert target.has_failures
    assert target.entities[0].status is ExecutionStatus.ERROR
    assert target.entities[0].tasks[0].result == {"changed": ["resource-a"]}


def test_configured_target_results_are_stored_directly_on_target() -> None:
    parameters = inspect.signature(TargetResult).parameters
    assert "tasks" in parameters
    configured_task = _task_result(
        task_id="configured_inventory",
        task_name="inventory",
        status=ExecutionStatus.SUCCESS,
    )
    target = TargetResult.create(
        **{
            "target_name": "target",
            "provider": "aws",
            "dry_run": False,
            "entities": [],
            "tasks": [configured_task],
        }
    )

    assert target.tasks == [configured_task]
    assert target.to_dict()["tasks"][0]["task_id"] == "configured_inventory"


def test_configured_target_results_are_present_in_jsonl_and_summary() -> None:
    parameters = inspect.signature(TargetResult).parameters
    assert "tasks" in parameters
    configured_task = _task_result(
        task_id="configured_inventory",
        task_name="inventory",
        status=ExecutionStatus.SUCCESS,
    )
    target = TargetResult.create(
        **{
            "target_name": "target",
            "provider": "aws",
            "dry_run": False,
            "entities": [],
            "tasks": [configured_task],
        }
    )
    engine = EngineResult.create(
        state=EngineState.COMPLETED_SUCCESS, auth_results=[], target_results=[target]
    )

    task_records = [
        record
        for record in build_jsonl_records_for_target(target)
        if record["record_type"] == "task"
    ]
    assert len(task_records) == 1
    assert task_records[0]["task_id"] == "configured_inventory"
    assert task_records[0]["task_name"] == "inventory"
    assert task_records[0]["entity_type"] == "configured_target"
    assert engine.build_summary()["targets"][0]["total_tasks"] == 1
