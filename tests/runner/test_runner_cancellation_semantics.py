from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from anvil.account import Account
from anvil.execution_context import ExecutionContext
from anvil.organization import Organization
from anvil.results import AccountResult, EngineResult, EngineState, ExecutionStatus
from anvil.task_loader import ResolvedTask


def _base_session() -> SimpleNamespace:
    return SimpleNamespace(profile_name=None)


def _context(*, tasks: list[ResolvedTask], fail_fast: bool = False) -> ExecutionContext:
    return ExecutionContext(
        region="us-east-1",
        role_name="TestRole",
        dry_run=True,
        tasks=tasks,
        metadata={},
        fail_fast=fail_fast,
    )


def _account_result(*, account_id: str, status: ExecutionStatus) -> AccountResult:
    return AccountResult(
        account_id=account_id,
        account_alias=f"acct-{account_id}",
        status=status,
        started_at="2026-03-25T00:00:00+00:00",
        ended_at="2026-03-25T00:00:01+00:00",
        duration_seconds=1.0,
        tasks=[],
    )


def test_account_cancelled_before_finishing_is_interrupted(monkeypatch):
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

    monkeypatch.setattr("anvil.account.get_worker_session", lambda **_: object())
    monkeypatch.setattr("anvil.account.assume_role", lambda **_: object())

    account = Account(
        account_id="123456789012",
        account_alias="test-account",
        is_management=True,
        base_session=_base_session(),
        context=context,
    )

    result = account.execute()

    assert result.status is ExecutionStatus.INTERRUPTED
    assert result.error is None
    assert [task.task_name for task in result.tasks] == ["task_one"]
    assert result.tasks[0].status is ExecutionStatus.SUCCESS



def test_account_success_still_reports_success(monkeypatch):
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

    monkeypatch.setattr("anvil.account.get_worker_session", lambda **_: object())
    monkeypatch.setattr("anvil.account.assume_role", lambda **_: object())

    account = Account(
        account_id="123456789012",
        account_alias="test-account",
        is_management=True,
        base_session=_base_session(),
        context=context,
    )

    result = account.execute()

    assert result.status is ExecutionStatus.SUCCESS
    assert result.error is None
    assert [task.task_name for task in result.tasks] == ["task_one", "task_two"]



def test_organization_fail_fast_sets_cancel_event(monkeypatch):
    context = _context(tasks=[], fail_fast=True)

    class ErrorAccount:
        def execute(self) -> AccountResult:
            return _account_result(account_id="111111111111", status=ExecutionStatus.ERROR)

    class WaitingAccount:
        def execute(self) -> AccountResult:
            while not context.cancel_event.is_set():
                time.sleep(0.01)
            return _account_result(
                account_id="222222222222",
                status=ExecutionStatus.INTERRUPTED,
            )

    monkeypatch.setattr(
        Organization,
        "_get_management_account_id",
        lambda self, session: "111111111111",
    )
    monkeypatch.setattr(
        Organization,
        "_build_accounts",
        lambda self, base_session, management_account_id: [
            ErrorAccount(),
            WaitingAccount(),
        ],
    )
    monkeypatch.setattr(
        "anvil.organization.create_base_session",
        lambda **_: object(),
    )

    organization = Organization(
        name="org-a",
        profile_name=None,
        max_workers=2,
        include_ids=None,
        exclude_ids=None,
        context=context,
    )

    result = organization.execute()

    assert context.cancel_event.is_set()
    assert len(result.account_results) >= 1
    assert any(
        account_result.status is ExecutionStatus.ERROR
        for account_result in result.account_results
    )



def test_engine_summary_counts_interrupted_accounts():
    successful = _account_result(
        account_id="111111111111", status=ExecutionStatus.SUCCESS
    )
    interrupted = _account_result(
        account_id="222222222222", status=ExecutionStatus.INTERRUPTED
    )
    failed = _account_result(account_id="333333333333", status=ExecutionStatus.ERROR)

    organization_result = SimpleNamespace(
        org_name="org-a",
        total_accounts=3,
        account_results=[successful, interrupted, failed],
        has_failures=True,
    )

    engine_result = EngineResult(
        state=EngineState.CANCELLED,
        generated_at="2026-03-25T00:00:00+00:00",
        auth_results=[],
        organization_results=[organization_result],
    )

    summary = engine_result.build_summary()

    assert summary["total_failed_accounts"] == 1
    assert summary["total_interrupted_accounts"] == 1
    assert summary["organizations"][0]["failed_accounts"] == 1
    assert summary["organizations"][0]["interrupted_accounts"] == 1
    assert summary["organizations"][0]["has_failures"] is True
