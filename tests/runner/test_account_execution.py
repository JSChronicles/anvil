from __future__ import annotations

import datetime
import threading
import time
from dataclasses import dataclass

from anvil.account import (
    ASSUMED_CREDENTIAL_REFRESH_BUFFER,
    MINIMUM_ASSUMED_CREDENTIAL_REFRESH_WINDOW,
    Account,
    AccountAccessStrategy,
)
from anvil.execution_context import ExecutionContext
from anvil.results import ExecutionStatus
from anvil.session import AssumedRoleCredentials, CachedClientSession
from anvil.task_loader import ResolvedTask


@dataclass
class BaseSession:
    profile_name: str | None = "profile-a"


class WorkerSession:
    def __init__(
        self,
        *,
        caller_account_id: str = "123456789012",
        region_name: str = "us-east-1",
        caller_identity_calls: list[str] | None = None,
    ) -> None:
        self._caller_account_id = caller_account_id
        self._caller_identity_calls = caller_identity_calls
        self.region_name = region_name
        self.client_calls = []

    def client(self, service_name, **kwargs):
        self.client_calls.append((service_name, kwargs))

        if service_name == "sts":

            class STSClient:
                def __init__(
                    self, *, account_id: str, caller_identity_calls: list[str] | None
                ) -> None:
                    self._account_id = account_id
                    self._caller_identity_calls = caller_identity_calls

                def get_caller_identity(self):
                    if self._caller_identity_calls is not None:
                        self._caller_identity_calls.append(self._account_id)
                    return {"Account": self._account_id}

            return STSClient(
                account_id=self._caller_account_id,
                caller_identity_calls=self._caller_identity_calls,
            )

        return object()


class RecordingSessionFactory:
    def __init__(
        self,
        *,
        caller_account_id: str = "123456789012",
        credential_expirations: list[datetime.datetime | None] | None = None,
    ) -> None:
        self.caller_account_id = caller_account_id
        self.credential_expirations = credential_expirations or []
        self.worker_session_calls = []
        self.assume_role_calls = []
        self.create_session_from_credentials_calls = []
        self.cached_session_calls = []
        self.caller_identity_calls = []

    def get_worker_session(self, **kwargs):
        self.worker_session_calls.append(kwargs)
        return WorkerSession(
            caller_account_id=self.caller_account_id,
            region_name=kwargs["region_name"],
            caller_identity_calls=self.caller_identity_calls,
        )

    def assume_role_credentials(self, **kwargs):
        self.assume_role_calls.append(kwargs)
        expiration = (
            self.credential_expirations.pop(0) if self.credential_expirations else None
        )
        return AssumedRoleCredentials(
            access_key_id=f"access-{len(self.assume_role_calls)}",
            secret_access_key="secret",
            session_token="token",
            expiration=expiration,
        )

    def create_session_from_credentials(self, **kwargs):
        self.create_session_from_credentials_calls.append(kwargs)
        return WorkerSession(region_name=kwargs["region_name"])

    def create_cached_client_session(self, **kwargs):
        self.cached_session_calls.append(kwargs)
        return CachedClientSession(session=kwargs["session"])


def _context(
    *,
    tasks: list[ResolvedTask],
    regions: list[str] | None = None,
    max_parallel_regions: int = 1,
):
    return ExecutionContext(
        regions=regions or ["us-east-1"],
        role_name="TestRole",
        dry_run=True,
        tasks=tasks,
        metadata={"source": "test"},
        max_parallel_regions=max_parallel_regions,
    )


def _account(
    *,
    tasks: list[ResolvedTask],
    session_factory: RecordingSessionFactory | None = None,
    access_strategy: AccountAccessStrategy = AccountAccessStrategy.DIRECT_PROFILE,
    regions: list[str] | None = None,
    max_parallel_regions: int = 1,
) -> Account:
    resolved_regions = regions or ["us-east-1"]
    return Account(
        account_id="123456789012",
        account_alias="test-account",
        is_management=access_strategy is AccountAccessStrategy.BASE_SESSION,
        access_strategy=access_strategy,
        base_session=BaseSession(),
        context=_context(
            tasks=tasks,
            regions=resolved_regions,
            max_parallel_regions=max_parallel_regions,
        ),
        regions=resolved_regions,
        session_factory=session_factory or RecordingSessionFactory(),
    )


def test_optional_task_failure_continues_to_later_tasks():
    def optional_failure(**kwargs):
        raise RuntimeError("optional failed")

    def required_success(**kwargs):
        return {"ok": True}

    account = _account(
        tasks=[
            ResolvedTask("optional", optional_failure, depends_on=[], optional=True),
            ResolvedTask("required", required_success, depends_on=[], optional=False),
        ]
    )

    result = account.execute()

    assert result.status is ExecutionStatus.SUCCESS
    assert [task.task_name for task in result.tasks] == ["optional", "required"]
    assert result.tasks[0].status is ExecutionStatus.ERROR
    assert result.tasks[1].status is ExecutionStatus.SUCCESS


def test_required_task_failure_stops_later_tasks():
    def required_failure(**kwargs):
        raise RuntimeError("required failed")

    def should_not_run(**kwargs):
        raise AssertionError("later task should not run")

    account = _account(
        tasks=[
            ResolvedTask("required", required_failure, depends_on=[], optional=False),
            ResolvedTask("later", should_not_run, depends_on=[], optional=False),
        ]
    )

    result = account.execute()

    assert result.status is ExecutionStatus.ERROR
    assert [task.task_name for task in result.tasks] == ["required"]
    assert result.tasks[0].error == "required failed"


def test_dependency_failure_blocks_optional_dependent_task():
    def dependency_failure(**kwargs):
        raise RuntimeError("dependency failed")

    def should_not_run(**kwargs):
        raise AssertionError("dependent task should not run")

    account = _account(
        tasks=[
            ResolvedTask(
                "dependency", dependency_failure, depends_on=[], optional=True
            ),
            ResolvedTask(
                "dependent", should_not_run, depends_on=["dependency"], optional=True
            ),
        ]
    )

    result = account.execute()

    assert result.status is ExecutionStatus.SUCCESS
    assert [task.task_name for task in result.tasks] == ["dependency", "dependent"]
    assert result.tasks[1].status is ExecutionStatus.ERROR
    assert result.tasks[1].error == "Blocked: dependency failed"


def test_direct_account_mismatch_returns_account_error():
    account = _account(
        tasks=[],
        session_factory=RecordingSessionFactory(caller_account_id="999999999999"),
    )

    result = account.execute()

    assert result.status is ExecutionStatus.ERROR
    assert result.tasks == []
    assert "Direct execution credentials resolve to account" in result.error


def test_direct_account_validation_runs_once_across_regions():
    factory = RecordingSessionFactory()

    def noop_task(**kwargs):
        return {"ok": True}

    account = _account(
        tasks=[ResolvedTask("noop", noop_task, depends_on=[], optional=False)],
        session_factory=factory,
        regions=["us-east-1", "us-west-2", "eu-west-1"],
    )

    result = account.execute()

    assert result.status is ExecutionStatus.SUCCESS
    assert factory.caller_identity_calls == ["123456789012"]


def test_aws_task_invocation_preserves_legacy_kwargs():
    seen: dict[str, object] = {}

    def legacy_task(*, account_id, account_alias, session, dry_run, metadata, actions):
        seen.update(
            {
                "account_id": account_id,
                "account_alias": account_alias,
                "region": session.region_name,
                "dry_run": dry_run,
                "metadata": metadata,
                "actions": actions,
            }
        )
        return {"ok": True}

    account = _account(
        tasks=[ResolvedTask("legacy", legacy_task, depends_on=[], optional=False)]
    )

    result = account.execute()

    assert result.status is ExecutionStatus.SUCCESS
    assert seen["account_id"] == "123456789012"
    assert seen["account_alias"] == "test-account"
    assert seen["region"] == "us-east-1"
    assert seen["dry_run"] is True
    assert seen["metadata"] == {"source": "test"}


def test_aws_task_invocation_provides_provider_neutral_kwargs():
    seen: dict[str, object] = {}

    def neutral_task(
        *,
        provider,
        execution_target_id,
        execution_target_name,
        execution_target_type,
        region,
        location,
        task_context,
        session,
        dry_run,
        metadata,
        actions,
    ):
        seen.update(
            {
                "provider": provider,
                "execution_target_id": execution_target_id,
                "execution_target_name": execution_target_name,
                "execution_target_type": execution_target_type,
                "region": region,
                "location": location,
                "context_provider": task_context.provider,
                "session_region": session.region_name,
                "dry_run": dry_run,
                "metadata": metadata,
                "actions": actions,
            }
        )
        return {"ok": True}

    account = _account(
        tasks=[ResolvedTask("neutral", neutral_task, depends_on=[], optional=False)]
    )

    result = account.execute()

    assert result.status is ExecutionStatus.SUCCESS
    assert seen == {
        "provider": "aws",
        "execution_target_id": "123456789012",
        "execution_target_name": "test-account",
        "execution_target_type": "account",
        "region": "us-east-1",
        "location": "us-east-1",
        "context_provider": "aws",
        "session_region": "us-east-1",
        "dry_run": True,
        "metadata": {"source": "test"},
        "actions": seen["actions"],
    }


def test_assume_role_path_reuses_assumed_credentials_for_regions():
    session_factory = RecordingSessionFactory()

    account = _account(
        tasks=[],
        session_factory=session_factory,
        access_strategy=AccountAccessStrategy.ASSUME_ROLE,
        regions=["us-east-1", "us-west-2"],
    )

    result = account.execute()

    assert result.status is ExecutionStatus.SUCCESS
    assert session_factory.assume_role_calls[0]["account_id"] == "123456789012"
    assert session_factory.assume_role_calls[0]["role_name"] == "TestRole"
    assert [
        call["region_name"]
        for call in session_factory.create_session_from_credentials_calls
    ] == ["us-east-1", "us-west-2"]


def test_assume_role_path_refreshes_expiring_credentials_before_region():
    now = datetime.datetime.now(datetime.UTC)
    session_factory = RecordingSessionFactory(
        credential_expirations=[
            now + MINIMUM_ASSUMED_CREDENTIAL_REFRESH_WINDOW,
            now + datetime.timedelta(hours=1),
        ]
    )

    account = _account(
        tasks=[],
        session_factory=session_factory,
        access_strategy=AccountAccessStrategy.ASSUME_ROLE,
        regions=["us-east-1"],
    )

    result = account.execute()

    assert result.status is ExecutionStatus.SUCCESS
    assert len(session_factory.assume_role_calls) == 2
    assert (
        session_factory.create_session_from_credentials_calls[0][
            "credentials"
        ].access_key_id
        == "access-2"
    )


def test_assume_role_parallel_regions_refresh_credentials_once():
    now = datetime.datetime.now(datetime.UTC)
    session_factory = RecordingSessionFactory(
        credential_expirations=[
            now + MINIMUM_ASSUMED_CREDENTIAL_REFRESH_WINDOW,
            now + datetime.timedelta(hours=1),
        ]
    )
    started_regions: set[str] = set()
    started_lock = threading.Lock()
    both_started = threading.Event()
    release_tasks = threading.Event()

    def blocking_task(**kwargs):
        region = kwargs["session"].region_name
        with started_lock:
            started_regions.add(region)
            if {"us-east-1", "us-west-2"}.issubset(started_regions):
                both_started.set()

        assert both_started.wait(timeout=1)
        assert release_tasks.wait(timeout=1)
        return {"region": region}

    account = _account(
        tasks=[ResolvedTask("blocking", blocking_task, depends_on=[], optional=False)],
        session_factory=session_factory,
        access_strategy=AccountAccessStrategy.ASSUME_ROLE,
        regions=["us-east-1", "us-west-2"],
        max_parallel_regions=2,
    )

    result_holder = {}
    thread = threading.Thread(
        target=lambda: result_holder.setdefault("result", account.execute())
    )
    thread.start()

    assert both_started.wait(timeout=1)
    release_tasks.set()
    thread.join(timeout=1)

    assert not thread.is_alive()
    assert result_holder["result"].status is ExecutionStatus.SUCCESS
    assert len(session_factory.assume_role_calls) == 2
    assert [
        call["credentials"].access_key_id
        for call in session_factory.create_session_from_credentials_calls
    ] == ["access-2", "access-2"]


def test_adaptive_assume_role_refresh_window_uses_observed_region_duration():
    region_duration = datetime.timedelta(minutes=15)

    assert (
        Account._refresh_window_for_region_duration(region_duration.total_seconds())
        == region_duration + ASSUMED_CREDENTIAL_REFRESH_BUFFER
    )


def test_adaptive_assume_role_refresh_window_keeps_minimum_for_short_regions():
    region_duration = datetime.timedelta(seconds=10)

    assert (
        Account._refresh_window_for_region_duration(region_duration.total_seconds())
        == MINIMUM_ASSUMED_CREDENTIAL_REFRESH_WINDOW
    )


def test_sequential_regions_use_adaptive_refresh_window(monkeypatch):
    now = datetime.datetime.now(datetime.UTC)
    session_factory = RecordingSessionFactory(
        credential_expirations=[
            now + datetime.timedelta(minutes=10),
            now + datetime.timedelta(hours=1),
        ]
    )

    monkeypatch.setattr(
        Account,
        "_refresh_window_for_region_duration",
        staticmethod(lambda region_duration_seconds: datetime.timedelta(minutes=12)),
    )

    def task(**kwargs):
        return {"region": kwargs["session"].region_name}

    account = _account(
        tasks=[ResolvedTask("task", task, depends_on=[], optional=False)],
        session_factory=session_factory,
        access_strategy=AccountAccessStrategy.ASSUME_ROLE,
        regions=["us-east-1", "us-west-2"],
    )

    result = account.execute()

    assert result.status is ExecutionStatus.SUCCESS
    assert len(session_factory.assume_role_calls) == 2
    assert [
        call["credentials"].access_key_id
        for call in session_factory.create_session_from_credentials_calls
    ] == ["access-1", "access-2"]


def test_serial_regions_keep_account_scoped_action_recorder():
    seen_actions: list[list[str]] = []

    def task(**kwargs):
        seen_actions.append(list(kwargs["actions"].actions))
        kwargs["actions"].record(kwargs["session"].region_name)
        return {"region": kwargs["session"].region_name}

    account = _account(
        tasks=[ResolvedTask("task", task, depends_on=[], optional=False)],
        regions=["us-east-1", "us-west-2"],
    )

    result = account.execute()

    assert result.status is ExecutionStatus.SUCCESS
    assert seen_actions == [[], ["us-east-1"]]


def test_tasks_in_same_region_share_cached_clients():
    seen_clients = []

    def task(**kwargs):
        seen_clients.append(kwargs["session"].client("ec2"))
        return {"ok": True}

    account = _account(
        tasks=[
            ResolvedTask("first", task, depends_on=[], optional=False),
            ResolvedTask("second", task, depends_on=[], optional=False),
        ]
    )

    result = account.execute()

    assert result.status is ExecutionStatus.SUCCESS
    assert len(seen_clients) == 2
    assert seen_clients[0] is seen_clients[1]


def test_parallel_regions_overlap_and_preserve_result_order():
    started_regions: set[str] = set()
    started_lock = threading.Lock()
    both_started = threading.Event()
    release_tasks = threading.Event()

    def blocking_task(**kwargs):
        region = kwargs["session"].region_name
        with started_lock:
            started_regions.add(region)
            if {"us-east-1", "us-west-2"}.issubset(started_regions):
                both_started.set()

        if region in {"us-east-1", "us-west-2"}:
            assert both_started.wait(timeout=1)
            assert release_tasks.wait(timeout=1)

        return {"region": region}

    account = _account(
        tasks=[ResolvedTask("blocking", blocking_task, depends_on=[], optional=False)],
        regions=["us-east-1", "us-west-2", "eu-west-1"],
        max_parallel_regions=2,
    )

    result_holder = {}
    thread = threading.Thread(
        target=lambda: result_holder.setdefault("result", account.execute())
    )
    thread.start()

    assert both_started.wait(timeout=1)
    release_tasks.set()
    thread.join(timeout=1)

    assert not thread.is_alive()
    result = result_holder["result"]
    assert result.status is ExecutionStatus.SUCCESS
    assert [task.region for task in result.tasks] == [
        "us-east-1",
        "us-west-2",
        "eu-west-1",
    ]


def test_parallel_region_failure_prevents_unscheduled_regions_from_starting():
    started_regions: list[str] = []
    started_lock = threading.Lock()

    def task(**kwargs):
        region = kwargs["session"].region_name
        with started_lock:
            started_regions.append(region)

        if region == "us-east-1":
            raise RuntimeError("hard failure")

        time.sleep(0.05)
        return {"region": region}

    account = _account(
        tasks=[ResolvedTask("task", task, depends_on=[], optional=False)],
        regions=["us-east-1", "us-west-2", "eu-west-1"],
        max_parallel_regions=2,
    )

    result = account.execute()

    assert result.status is ExecutionStatus.ERROR
    assert "us-east-1" in started_regions
    assert "eu-west-1" not in started_regions
    assert [task.region for task in result.tasks] in (
        ["us-east-1"],
        ["us-east-1", "us-west-2"],
    )


def test_parallel_account_cancelled_before_regions_start_is_interrupted():
    def task(**kwargs):
        raise AssertionError("task should not run after cancellation")

    context = _context(
        tasks=[ResolvedTask("task", task, depends_on=[], optional=False)],
        regions=["us-east-1", "us-west-2"],
        max_parallel_regions=2,
    )
    context.cancel_event.set()

    account = Account(
        account_id="123456789012",
        account_alias="test-account",
        is_management=True,
        access_strategy=AccountAccessStrategy.BASE_SESSION,
        base_session=BaseSession(),
        context=context,
        regions=["us-east-1", "us-west-2"],
        session_factory=RecordingSessionFactory(),
    )

    result = account.execute()

    assert result.status is ExecutionStatus.INTERRUPTED
    assert result.tasks == []


