from __future__ import annotations

import datetime
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import pytest

from anvil.providers.aws.account import (
    MINIMUM_ASSUMED_CREDENTIAL_REFRESH_WINDOW,
    AccountAccessStrategy,
)
from anvil.descriptors import TargetDescriptor
from anvil.execution_context import ExecutionContext
from anvil.providers.aws.provider import AwsExecutionTargetData, AwsProvider
from anvil.providers.base import ExecutionTarget
from anvil.results import ExecutionStatus
from anvil.runner import _execute_provider_execution_target
from anvil.providers.aws.session import AssumedRoleCredentials, CachedClientSession
from anvil.task_loader import ResolvedTask


@dataclass
class BaseSession:
    profile_name: str | None = "profile-a"


class WorkerSession:
    def __init__(
        self, *, caller_account_id: str = "123456789012", region_name: str = "us-east-1"
    ) -> None:
        self._caller_account_id = caller_account_id
        self.region_name = region_name
        self.client_calls = []

    def client(self, service_name, **kwargs):
        self.client_calls.append((service_name, kwargs))
        if service_name == "sts":

            class STSClient:
                def __init__(self, *, account_id: str) -> None:
                    self._account_id = account_id

                def get_caller_identity(self):
                    return {"Account": self._account_id}

            return STSClient(account_id=self._caller_account_id)

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

    def get_worker_session(self, **kwargs):
        self.worker_session_calls.append(kwargs)
        return WorkerSession(
            caller_account_id=self.caller_account_id, region_name=kwargs["region_name"]
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


def _target() -> TargetDescriptor:
    return TargetDescriptor(
        name="selected",
        provider="aws",
        mode="accounts",
        include=["123456789012"],
        provider_options={"role_name": "TestRole"},
    )


def _context(
    *,
    regions: list[str] | None = None,
    tasks: list[ResolvedTask] | None = None,
    benchmark_enabled: bool = False,
) -> ExecutionContext:
    return ExecutionContext(
        regions=regions or ["us-east-1"],
        dry_run=True,
        tasks=tasks or [],
        metadata={},
        benchmark_enabled=benchmark_enabled,
    )


def _execution_target(
    *,
    session_factory: RecordingSessionFactory,
    access_strategy: AccountAccessStrategy,
    regions: list[str] | None = None,
) -> ExecutionTarget:
    return ExecutionTarget(
        id="123456789012",
        name="test-account",
        type="account",
        provider="aws",
        regions=regions or ["us-east-1"],
        provider_data=AwsExecutionTargetData(
            account_id="123456789012",
            account_alias="test-account",
            is_management=False,
            access_strategy=access_strategy,
            role_name=(
                "TestRole"
                if access_strategy is AccountAccessStrategy.ASSUME_ROLE
                else None
            ),
            base_session=BaseSession(),
            regions=regions or ["us-east-1"],
            session_factory=session_factory,
        ),
    )


def test_runtime_validates_direct_profile_once_for_multiple_regions():
    session_factory = RecordingSessionFactory()
    runtime = AwsProvider().prepare_execution_runtime(
        target=_target(),
        execution_target=_execution_target(
            session_factory=session_factory,
            access_strategy=AccountAccessStrategy.DIRECT_PROFILE,
            regions=["us-east-1", "us-west-2"],
        ),
        context=_context(regions=["us-east-1", "us-west-2"]),
    )

    runtime.build_session(region="us-east-1")
    runtime.build_session(region="us-west-2")

    assert [call["region_name"] for call in session_factory.worker_session_calls] == [
        "us-east-1",
        "us-east-1",
        "us-west-2",
    ]


def test_runtime_rejects_direct_profile_for_wrong_account():
    session_factory = RecordingSessionFactory(caller_account_id="999999999999")

    with pytest.raises(ValueError, match="not target account '123456789012'"):
        AwsProvider().prepare_execution_runtime(
            target=_target(),
            execution_target=_execution_target(
                session_factory=session_factory,
                access_strategy=AccountAccessStrategy.DIRECT_PROFILE,
            ),
            context=_context(),
        )


def test_runtime_assumes_role_once_and_reuses_credentials_across_regions():
    session_factory = RecordingSessionFactory()
    runtime = AwsProvider().prepare_execution_runtime(
        target=_target(),
        execution_target=_execution_target(
            session_factory=session_factory,
            access_strategy=AccountAccessStrategy.ASSUME_ROLE,
            regions=["us-east-1", "us-west-2"],
        ),
        context=_context(regions=["us-east-1", "us-west-2"]),
    )

    runtime.build_session(region="us-east-1")
    runtime.build_session(region="us-west-2")

    assert len(session_factory.assume_role_calls) == 1
    assert [
        call["credentials"].access_key_id
        for call in session_factory.create_session_from_credentials_calls
    ] == ["access-1", "access-1"]


def test_runtime_refreshes_expiring_assume_role_credentials_before_reuse():
    now = datetime.datetime.now(datetime.UTC)
    session_factory = RecordingSessionFactory(
        credential_expirations=[
            now + MINIMUM_ASSUMED_CREDENTIAL_REFRESH_WINDOW,
            now + datetime.timedelta(hours=1),
        ]
    )
    runtime = AwsProvider().prepare_execution_runtime(
        target=_target(),
        execution_target=_execution_target(
            session_factory=session_factory,
            access_strategy=AccountAccessStrategy.ASSUME_ROLE,
        ),
        context=_context(),
    )

    runtime.build_session(region="us-east-1")

    assert len(session_factory.assume_role_calls) == 2
    assert (
        session_factory.create_session_from_credentials_calls[0][
            "credentials"
        ].access_key_id
        == "access-2"
    )


def test_runtime_parallel_regions_refresh_expiring_credentials_once():
    now = datetime.datetime.now(datetime.UTC)
    session_factory = RecordingSessionFactory(
        credential_expirations=[
            now + MINIMUM_ASSUMED_CREDENTIAL_REFRESH_WINDOW,
            now + datetime.timedelta(hours=1),
        ]
    )
    runtime = AwsProvider().prepare_execution_runtime(
        target=_target(),
        execution_target=_execution_target(
            session_factory=session_factory,
            access_strategy=AccountAccessStrategy.ASSUME_ROLE,
            regions=["us-east-1", "us-west-2"],
        ),
        context=_context(regions=["us-east-1", "us-west-2"]),
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        sessions = list(
            executor.map(
                lambda region: runtime.build_session(region=region),
                ["us-east-1", "us-west-2"],
            )
        )

    assert len(sessions) == 2
    assert len(session_factory.assume_role_calls) == 2
    assert {
        call["credentials"].access_key_id
        for call in session_factory.create_session_from_credentials_calls
    } == {"access-2"}


def test_runtime_records_region_duration_for_adaptive_refresh_window():
    now = datetime.datetime.now(datetime.UTC)
    session_factory = RecordingSessionFactory(
        credential_expirations=[
            now + datetime.timedelta(minutes=10),
            now + datetime.timedelta(hours=1),
        ]
    )
    runtime = AwsProvider().prepare_execution_runtime(
        target=_target(),
        execution_target=_execution_target(
            session_factory=session_factory,
            access_strategy=AccountAccessStrategy.ASSUME_ROLE,
            regions=["us-east-1", "us-west-2"],
        ),
        context=_context(regions=["us-east-1", "us-west-2"]),
    )

    runtime.build_session(region="us-east-1")
    runtime.record_region_outcome(
        region="us-east-1",
        duration_seconds=datetime.timedelta(minutes=15).total_seconds(),
        failed=False,
        interrupted=False,
    )
    runtime.build_session(region="us-west-2")

    assert [
        call["credentials"].access_key_id
        for call in session_factory.create_session_from_credentials_calls
    ] == ["access-1", "access-2"]


def test_aws_provider_execution_path_preserves_runtime_benchmark_data():
    session_factory = RecordingSessionFactory()

    def run(**kwargs):
        return {"region": kwargs["region"]}

    context = _context(
        regions=["us-east-1", "us-west-2"],
        tasks=[ResolvedTask("scan", run, depends_on=[], optional=False)],
        benchmark_enabled=True,
    )

    result = _execute_provider_execution_target(
        provider=AwsProvider(),
        target=_target(),
        execution_target=_execution_target(
            session_factory=session_factory,
            access_strategy=AccountAccessStrategy.ASSUME_ROLE,
            regions=["us-east-1", "us-west-2"],
        ),
        context=context,
    )

    assert result.status is ExecutionStatus.SUCCESS
    assert result.benchmark is not None
    assert result.benchmark["access_strategy"] == "assume_role"
    assert result.benchmark["assume_role_seconds"] >= 0.0
    assert result.benchmark["direct_access_validation_seconds"] == 0.0
    assert result.benchmark["assume_role_refresh_count"] == 0
    assert result.benchmark["assume_role_refresh_window_seconds"] >= 300.0
    assert result.benchmark["region_execution_seconds"] >= 0.0
    region_benchmarks = result.benchmark["regions"]
    assert isinstance(region_benchmarks, dict)
    assert set(region_benchmarks) == {"us-east-1", "us-west-2"}
    for region_benchmark in region_benchmarks.values():
        assert isinstance(region_benchmark, dict)
        assert region_benchmark["duration_seconds"] >= 0.0
        assert region_benchmark["task_count"] == 1
        assert region_benchmark["interrupted"] is False
        assert region_benchmark["failed"] is False
