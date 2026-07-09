from __future__ import annotations

import time

from anvil.account import Account, AccountAccessStrategy
from anvil.descriptors import ConfigBranch
from anvil.execution_context import ExecutionContext
from anvil.executor import execute_accounts
from anvil.results import (
    EngineResult,
    EngineState,
    EntityResult,
    ExecutionStatus,
    TargetResult,
)
from anvil.task_loader import ResolvedTask


class StubSessionFactory:
    def create_base_session(self, **kwargs):
        return object()

    def get_worker_session(self, **kwargs):
        class _WorkerSession:
            def client(self, service_name):
                class _STSClient:
                    def get_caller_identity(self):
                        return {"Account": "123456789012"}

                return _STSClient()

        return _WorkerSession()

    def assume_role_credentials(self, **kwargs):
        return object()

    def create_session_from_credentials(self, **kwargs):
        return object()

    def create_cached_client_session(self, **kwargs):
        return kwargs["session"]


def _base_session():
    class _BaseSession:
        profile_name = None

    return _BaseSession()


def _context(*, tasks: list[ResolvedTask], fail_fast: bool = False) -> ExecutionContext:
    return ExecutionContext(
        regions=["us-east-1"],
        role_name="TestRole",
        dry_run=True,
        tasks=tasks,
        metadata={},
        fail_fast=fail_fast,
    )


def _entity_result(*, entity_id: str, status: ExecutionStatus) -> EntityResult:
    return EntityResult(
        id=entity_id,
        name=f"acct-{entity_id}",
        type="account",
        status=status,
        started_at="2026-03-25T00:00:00+00:00",
        ended_at="2026-03-25T00:00:01+00:00",
        duration_seconds=1.0,
        tasks=[],
    )


def test_account_cancelled_before_finishing_is_interrupted():
    def task_one(**kwargs):
        kwargs["actions"].record("ran task one")
        context.cancel_event.set()
        return {"task": "one"}

    def task_two(**kwargs):
        raise AssertionError("task two should not run after cancellation")

    tasks = [
        ResolvedTask(name="task_one", run=task_one, depends_on=[], optional=False),
        ResolvedTask(name="task_two", run=task_two, depends_on=[], optional=False),
    ]
    context = _context(tasks=tasks)

    account = Account(
        account_id="123456789012",
        account_alias="test-account",
        is_management=True,
        access_strategy=AccountAccessStrategy.BASE_SESSION,
        base_session=_base_session(),
        context=context,
        regions=["us-east-1"],
        session_factory=StubSessionFactory(),
    )

    result = account.execute()

    assert result.status is ExecutionStatus.INTERRUPTED
    assert result.error is None
    assert [task.task_name for task in result.tasks] == ["task_one"]
    assert [task.region for task in result.tasks] == ["us-east-1"]
    assert result.tasks[0].status is ExecutionStatus.SUCCESS


def test_account_success_still_reports_success():
    def task_one(**kwargs):
        kwargs["actions"].record("task one")
        return {"task": "one"}

    def task_two(**kwargs):
        kwargs["actions"].record("task two")
        return {"task": "two"}

    tasks = [
        ResolvedTask(name="task_one", run=task_one, depends_on=[], optional=False),
        ResolvedTask(name="task_two", run=task_two, depends_on=[], optional=False),
    ]
    context = _context(tasks=tasks)

    account = Account(
        account_id="123456789012",
        account_alias="test-account",
        is_management=True,
        access_strategy=AccountAccessStrategy.BASE_SESSION,
        base_session=_base_session(),
        context=context,
        regions=["us-east-1"],
        session_factory=StubSessionFactory(),
    )

    result = account.execute()

    assert result.status is ExecutionStatus.SUCCESS
    assert result.error is None
    assert [task.task_name for task in result.tasks] == ["task_one", "task_two"]
    assert [task.region for task in result.tasks] == ["us-east-1", "us-east-1"]


def test_execute_accounts_fail_fast_sets_cancel_event():
    context = _context(tasks=[], fail_fast=True)

    class ErrorAccount:
        def execute(self) -> EntityResult:
            return _entity_result(
                entity_id="111111111111", status=ExecutionStatus.ERROR
            )

    class WaitingAccount:
        def execute(self) -> EntityResult:
            while not context.cancel_event.is_set():
                time.sleep(0.01)
            return _entity_result(
                entity_id="222222222222", status=ExecutionStatus.INTERRUPTED
            )

    result = execute_accounts(
        name="org-a",
        config_branch=ConfigBranch.TARGETS,
        max_workers=2,
        context=context,
        accounts=[ErrorAccount(), WaitingAccount()],
    )

    assert context.cancel_event.is_set()
    assert len(result.entities) >= 1
    assert any(
        entity_result.status is ExecutionStatus.ERROR
        for entity_result in result.entities
    )


def test_engine_summary_counts_interrupted_entities():
    successful = _entity_result(
        entity_id="111111111111", status=ExecutionStatus.SUCCESS
    )
    interrupted = _entity_result(
        entity_id="222222222222", status=ExecutionStatus.INTERRUPTED
    )
    failed = _entity_result(entity_id="333333333333", status=ExecutionStatus.ERROR)

    target_result = TargetResult.create(
        config_branch=ConfigBranch.TARGETS,
        target_name="org-a",
        dry_run=True,
        entities=[successful, interrupted, failed],
    )

    engine_result = EngineResult(
        config_branch=ConfigBranch.TARGETS,
        state=EngineState.CANCELLED,
        generated_at="2026-03-25T00:00:00+00:00",
        auth_results=[],
        target_results=[target_result],
    )

    summary = engine_result.build_summary()

    assert summary["total_failed_entities"] == 1
    assert summary["total_interrupted_entities"] == 1
    assert summary["targets"][0]["failed_entities"] == 1
    assert summary["targets"][0]["interrupted_entities"] == 1
    assert summary["targets"][0]["has_failures"] is True
