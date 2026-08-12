from __future__ import annotations

import datetime
import inspect
import threading

import pytest

from anvil.execution_context import ExecutionContext
from anvil.providers.base import ExecutionTarget
from anvil.results import ExecutionStatus, TaskResult
from anvil.runner import _execute_provider_region
from anvil.task_loader import ResolvedTask, TaskScope


class _Runtime:
    def build_session(self, *, region: str) -> object:
        return {"region": region}

    def record_region_outcome(
        self, *, region: str, duration_seconds: float, failed: bool, interrupted: bool
    ) -> None:
        pass


def _task(
    task_id: str, run, *, depends_on: list[str] | None = None, always_run: bool = False
) -> ResolvedTask:
    return ResolvedTask(
        id=task_id,
        name=task_id,
        run=run,
        depends_on=depends_on or [],
        always_run=always_run,
        scope=TaskScope.REGION,
    )


def _terminal_result(
    task_id: str, status: ExecutionStatus, *, skip_reason: str | None = None
) -> TaskResult:
    parameters = inspect.signature(TaskResult).parameters
    assert "skip_reason" in parameters
    now = datetime.datetime.now(datetime.UTC).isoformat()
    return TaskResult(
        task_id=task_id,
        task_name=task_id,
        region="region-a",
        status=status,
        started_at=now,
        ended_at=now,
        duration_seconds=0.0,
        error="upstream failed" if status.is_error else None,
        skip_reason=skip_reason,
    )


def _execute(
    tasks: list[ResolvedTask],
    *,
    dependency_results: dict[str, TaskResult] | None = None,
    fail_fast: bool = False,
    cancel_event: threading.Event | None = None,
):
    context = ExecutionContext(
        regions=["region-a"],
        dry_run=False,
        tasks=tasks,
        metadata={},
        fail_fast=fail_fast,
        cancel_event=cancel_event or threading.Event(),
    )
    outcome = _execute_provider_region(
        execution_target=ExecutionTarget(
            id="entity-a",
            name="Entity A",
            type="resource",
            provider="fake",
            regions=["region-a"],
        ),
        runtime=_Runtime(),
        context=context,
        region="region-a",
        target_cancel_event=threading.Event(),
        dependency_results=dependency_results,
    )
    return context, outcome


def _status_rows(outcome) -> list[tuple[str, str, str | None]]:
    return [
        (result.task_name, result.status.value, result.skip_reason)
        for result in outcome.task_results
    ]


def test_normal_dependency_failure_skips_consumer_but_not_independent_branch() -> None:
    ran: list[str] = []

    def fail(**kwargs):
        ran.append("producer")
        raise RuntimeError("root failure")

    tasks = [
        _task("producer", fail),
        _task(
            "consumer", lambda **kwargs: ran.append("consumer"), depends_on=["producer"]
        ),
        _task("independent", lambda **kwargs: ran.append("independent")),
    ]

    _context, outcome = _execute(tasks)

    assert ran == ["producer", "independent"]
    assert _status_rows(outcome) == [
        ("producer", "error", None),
        ("consumer", "skipped", "dependency_unsuccessful"),
        ("independent", "success", None),
    ]
    assert outcome.failed


@pytest.mark.parametrize(
    ("dependency_status_value", "skip_reason"),
    [("error", None), ("interrupted", None), ("skipped", "dependency_unsuccessful")],
)
def test_normal_task_is_skipped_after_any_unsuccessful_dependency(
    dependency_status_value: str, skip_reason: str | None
) -> None:
    ran = False

    def consumer(**kwargs):
        nonlocal ran
        ran = True

    _context, outcome = _execute(
        [_task("consumer", consumer, depends_on=["producer"])],
        dependency_results={
            "producer": _terminal_result(
                "producer",
                ExecutionStatus(dependency_status_value),
                skip_reason=skip_reason,
            )
        },
    )

    assert not ran
    assert _status_rows(outcome) == [("consumer", "skipped", "dependency_unsuccessful")]


@pytest.mark.parametrize(
    ("dependency_status_value", "skip_reason"),
    [
        ("success", None),
        ("error", None),
        ("interrupted", None),
        ("skipped", "dependency_unsuccessful"),
    ],
)
def test_always_run_executes_after_every_activated_terminal_dependency(
    dependency_status_value: str, skip_reason: str | None
) -> None:
    ran: list[str] = []
    _context, outcome = _execute(
        [
            _task(
                "cleanup",
                lambda **kwargs: ran.append("cleanup"),
                depends_on=["producer"],
                always_run=True,
            )
        ],
        dependency_results={
            "producer": _terminal_result(
                "producer",
                ExecutionStatus(dependency_status_value),
                skip_reason=skip_reason,
            )
        },
    )

    assert ran == ["cleanup"]
    assert _status_rows(outcome) == [("cleanup", "success", None)]


def test_always_run_failure_remains_an_error_and_root_error_is_preserved() -> None:
    tasks = [
        _task(
            "producer",
            lambda **kwargs: (_ for _ in ()).throw(RuntimeError("root failure")),
        ),
        _task(
            "cleanup",
            lambda **kwargs: (_ for _ in ()).throw(RuntimeError("cleanup failure")),
            depends_on=["producer"],
            always_run=True,
        ),
    ]

    _context, outcome = _execute(tasks)

    assert [result.status for result in outcome.task_results] == [
        ExecutionStatus.ERROR,
        ExecutionStatus.ERROR,
    ]
    assert outcome.task_results[0].error == "root failure"
    assert outcome.task_results[1].error == "cleanup failure"
    assert outcome.failed


def test_successful_finalizer_does_not_clear_upstream_failure() -> None:
    tasks = [
        _task(
            "producer",
            lambda **kwargs: (_ for _ in ()).throw(RuntimeError("root failure")),
        ),
        _task(
            "cleanup",
            lambda **kwargs: {"restored": True},
            depends_on=["producer"],
            always_run=True,
        ),
    ]

    _context, outcome = _execute(tasks)

    assert _status_rows(outcome) == [
        ("producer", "error", None),
        ("cleanup", "success", None),
    ]
    assert outcome.task_results[0].error == "root failure"
    assert outcome.failed


def test_fail_fast_settles_pending_nodes_and_runs_activated_finalizer() -> None:
    ran: list[str] = []
    tasks = [
        _task(
            "producer",
            lambda **kwargs: (_ for _ in ()).throw(RuntimeError("root failure")),
        ),
        _task("pending", lambda **kwargs: ran.append("pending")),
        _task(
            "cleanup",
            lambda **kwargs: ran.append("cleanup"),
            depends_on=["producer"],
            always_run=True,
        ),
    ]

    _context, outcome = _execute(tasks, fail_fast=True)

    assert ran == ["cleanup"]
    assert _status_rows(outcome) == [
        ("producer", "error", None),
        ("pending", "skipped", "fail_fast"),
        ("cleanup", "success", None),
    ]
    assert outcome.task_results[0].error == "root failure"


def test_graceful_cancellation_runs_only_activated_finalizers() -> None:
    cancel_event = threading.Event()
    ran: list[str] = []

    def producer(**kwargs):
        ran.append("producer")
        cancel_event.set()
        return {"changed": True}

    tasks = [
        _task("producer", producer),
        _task("ordinary", lambda **kwargs: ran.append("ordinary")),
        _task(
            "cleanup",
            lambda **kwargs: ran.append("cleanup"),
            depends_on=["producer"],
            always_run=True,
        ),
        _task(
            "never_activated",
            lambda **kwargs: ran.append("never_activated"),
            depends_on=["ordinary"],
            always_run=True,
        ),
    ]

    _context, outcome = _execute(tasks, cancel_event=cancel_event)

    assert ran == ["producer", "cleanup"]
    assert _status_rows(outcome) == [
        ("producer", "success", None),
        ("ordinary", "skipped", "cancelled_before_start"),
        ("cleanup", "success", None),
        ("never_activated", "skipped", "cancelled_before_start"),
    ]


def test_cancellation_before_chain_start_settles_every_task_without_cleanup() -> None:
    cancel_event = threading.Event()
    cancel_event.set()
    ran: list[str] = []
    tasks = [
        _task("producer", lambda **kwargs: ran.append("producer")),
        _task(
            "cleanup",
            lambda **kwargs: ran.append("cleanup"),
            depends_on=["producer"],
            always_run=True,
        ),
    ]

    _context, outcome = _execute(tasks, cancel_event=cancel_event)

    assert not ran
    assert _status_rows(outcome) == [
        ("producer", "skipped", "cancelled_before_start"),
        ("cleanup", "skipped", "cancelled_before_start"),
    ]


def test_success_plus_skipped_aggregates_to_success() -> None:
    results_module = __import__(
        "anvil.results", fromlist=["aggregate_execution_statuses"]
    )
    aggregate = getattr(results_module, "aggregate_execution_statuses", None)
    assert callable(aggregate)

    skipped = ExecutionStatus("skipped")
    assert aggregate([ExecutionStatus.SUCCESS, skipped]) is ExecutionStatus.SUCCESS
    assert aggregate([skipped]) is ExecutionStatus.SUCCESS
    assert (
        aggregate([ExecutionStatus.ERROR, ExecutionStatus.INTERRUPTED, skipped])
        is ExecutionStatus.ERROR
    )
