from __future__ import annotations

import importlib
import inspect
import threading

import pytest

from anvil.execution_context import ExecutionContext
from anvil.providers.base import ExecutionTarget
from anvil.results import ExecutionStatus, TaskResult
from anvil.runner import _execute_provider_region
from anvil.task_context import TaskCallContext
from anvil.task_loader import ResolvedTask, TaskScope, list_tasks
from anvil.task_validation import validate_tasks


class _Runtime:
    def build_session(self, *, region: str) -> object:
        return {"region": region}

    def record_region_outcome(
        self, *, region: str, duration_seconds: float, failed: bool, interrupted: bool
    ) -> None:
        pass


def _result(
    *,
    result: object | None = None,
    status: ExecutionStatus = ExecutionStatus.SUCCESS,
    error: str | None = None,
) -> TaskResult:
    return TaskResult(
        task_id="producer",
        task_name="producer",
        region="us-east-1",
        status=status,
        started_at="2026-07-28T00:00:00+00:00",
        ended_at="2026-07-28T00:00:01+00:00",
        duration_seconds=1.0,
        result=result,
        error=error,
        actions=["recorded"],
    )


def _run_region(
    *, tasks: list[ResolvedTask], metadata: dict[str, object] | None = None
):
    return _execute_provider_region(
        execution_target=ExecutionTarget(
            id="111111111111",
            name="account",
            type="account",
            provider="aws",
            regions=["us-east-1"],
        ),
        runtime=_Runtime(),
        context=ExecutionContext(
            regions=["us-east-1"], dry_run=False, tasks=tasks, metadata=metadata or {}
        ),
        region="us-east-1",
        target_cancel_event=threading.Event(),
    )


def test_all_discovered_tasks_use_the_phase2_call_signature() -> None:
    validate_tasks(list_tasks())

    expected = TaskCallContext.keyword_names()
    assert "dependency_data" in expected
    for task in list_tasks():
        run = task.load()
        assert set(inspect.signature(run).parameters) == expected


def test_task_metadata_is_merged_recursively_without_mutating_sources() -> None:
    received: list[dict[str, object]] = []
    target_metadata = {
        "nested": {"kept": "target", "replaced": "target"},
        "list": ["target"],
        "scalar": "target",
    }
    task_metadata = {
        "nested": {"replaced": "task", "added": "task"},
        "list": ["task"],
        "scalar": {"now": "mapping"},
    }
    task = ResolvedTask(
        id="consumer",
        name="consumer",
        run=lambda **kwargs: received.append(kwargs["metadata"]),
        depends_on=[],
        scope=TaskScope.REGION,
        metadata=task_metadata,
    )

    _run_region(tasks=[task], metadata=target_metadata)

    assert received == [
        {
            "nested": {"kept": "target", "replaced": "task", "added": "task"},
            "list": ["task"],
            "scalar": {"now": "mapping"},
        }
    ]
    assert target_metadata == {
        "nested": {"kept": "target", "replaced": "target"},
        "list": ["target"],
        "scalar": "target",
    }
    assert task_metadata == {
        "nested": {"replaced": "task", "added": "task"},
        "list": ["task"],
        "scalar": {"now": "mapping"},
    }


def test_dependency_input_resolution_supports_complete_results_and_paths() -> None:
    task_context_module = importlib.import_module("anvil.task_context")
    resolver = getattr(task_context_module, "resolve_dependency_data", None)
    assert callable(resolver)
    producer = _result(
        result={"attachments": {"items": ["a"]}, "nullable": None},
        error="retained error detail",
    )

    resolved = resolver(
        references={
            "complete": {"task_id": "producer"},
            "payload": {"task_id": "producer", "path": "result"},
            "nested": {"task_id": "producer", "path": "result.attachments.items"},
            "nullable": {"task_id": "producer", "path": "result.nullable"},
            "status": {"task_id": "producer", "path": "status"},
            "error": {"task_id": "producer", "path": "error"},
            "actions": {"task_id": "producer", "path": "actions"},
        },
        dependency_results={"producer": producer},
    )

    assert resolved == {
        "complete": producer,
        "payload": {"attachments": {"items": ["a"]}, "nullable": None},
        "nested": ["a"],
        "nullable": None,
        "status": ExecutionStatus.SUCCESS,
        "error": "retained error detail",
        "actions": ["recorded"],
    }


def test_dependency_input_resolution_applies_paths_to_all_ordered_results() -> None:
    task_context_module = importlib.import_module("anvil.task_context")
    resolver = getattr(task_context_module, "resolve_dependency_data", None)
    assert callable(resolver)

    resolved = resolver(
        references={"values": {"task_id": "producer", "path": "result.value"}},
        dependency_results={
            "producer": [
                _result(result={"value": "first"}),
                _result(result={"value": "second"}),
            ]
        },
    )

    assert resolved == {"values": ["first", "second"]}


def test_multi_result_resolution_fails_if_any_result_lacks_the_path() -> None:
    task_context_module = importlib.import_module("anvil.task_context")
    resolver = getattr(task_context_module, "resolve_dependency_data", None)
    error_type = getattr(task_context_module, "TaskInputResolutionError", None)
    assert callable(resolver)
    assert isinstance(error_type, type)

    with pytest.raises(error_type, match=r"values.*result\.value"):
        resolver(
            references={"values": {"task_id": "producer", "path": "result.value"}},
            dependency_results={
                "producer": [
                    _result(result={"value": "first"}),
                    _result(result={"other": "second"}),
                ]
            },
        )


def test_missing_dependency_path_is_a_clear_input_error() -> None:
    task_context_module = importlib.import_module("anvil.task_context")
    resolver = getattr(task_context_module, "resolve_dependency_data", None)
    error_type = getattr(task_context_module, "TaskInputResolutionError", None)
    assert callable(resolver)
    assert isinstance(error_type, type)

    with pytest.raises(error_type, match=r"consumer_input.*result\.missing"):
        resolver(
            references={
                "consumer_input": {"task_id": "producer", "path": "result.missing"}
            },
            dependency_results={"producer": _result(result={"present": None})},
        )


def test_task_context_deep_copies_complete_dependency_results() -> None:
    producer = _result(result={"items": ["original"]})
    context = TaskCallContext(
        provider="aws",
        execution_target_id="111111111111",
        execution_target_name="account",
        execution_target_type="account",
        region="us-east-1",
        session=object(),
        dry_run=False,
        metadata={},
        dependency_data={"complete": producer},
        actions=object(),
    )

    first = context.to_kwargs()
    second = context.to_kwargs()
    first["dependency_data"]["complete"].result["items"].append("mutated")

    assert second["dependency_data"]["complete"].result == {"items": ["original"]}
    assert producer.result == {"items": ["original"]}


def test_runner_passes_resolved_dependency_data_to_the_consumer() -> None:
    received: list[dict[str, object]] = []
    producer = ResolvedTask(
        id="producer_invocation",
        name="shared_component",
        run=lambda **kwargs: {"value": {"items": ["resolved"]}},
        depends_on=[],
        scope=TaskScope.REGION,
    )
    consumer = ResolvedTask(
        id="consumer",
        name="consumer",
        run=lambda **kwargs: received.append(kwargs["dependency_data"]),
        depends_on=["producer_invocation"],
        scope=TaskScope.REGION,
        dependency_data={
            "payload": {"task_id": "producer_invocation", "path": "result.value"}
        },
    )

    outcome = _run_region(tasks=[producer, consumer])

    assert [result.status for result in outcome.task_results] == [
        ExecutionStatus.SUCCESS,
        ExecutionStatus.SUCCESS,
    ]
    assert received == [{"payload": {"items": ["resolved"]}}]


def test_task_execution_error_carries_validated_partial_result() -> None:
    task_error_module = importlib.import_module("anvil.task_errors")
    task_execution_error = getattr(task_error_module, "TaskExecutionError")

    error = task_execution_error(
        "mutation failed", partial_result={"attachments": ["partial"]}
    )

    assert str(error) == "mutation failed"
    assert error.partial_result == {"attachments": ["partial"]}
    with pytest.raises(TypeError, match="JSON-serializable"):
        task_execution_error("invalid", partial_result={"bad": object()})


def test_failed_task_result_preserves_partial_execution_data() -> None:
    task_error_module = importlib.import_module("anvil.task_errors")
    task_execution_error = getattr(task_error_module, "TaskExecutionError")

    def run(**kwargs):
        raise task_execution_error(
            "mutation failed", partial_result={"attachments": ["partial"]}
        )

    outcome = _run_region(
        tasks=[
            ResolvedTask(
                id="producer",
                name="producer",
                run=run,
                depends_on=[],
                scope=TaskScope.REGION,
            )
        ]
    )

    assert outcome.task_results[0].status is ExecutionStatus.ERROR
    assert outcome.task_results[0].error == "mutation failed"
    assert outcome.task_results[0].result == {"attachments": ["partial"]}
