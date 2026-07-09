from __future__ import annotations

import threading

import pytest
from botocore.exceptions import ClientError

from anvil import session as session_module
from anvil.session import AssumedRoleCredentials, SessionFactory


class FakeBotoSession:
    created = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        FakeBotoSession.created.append(self)


class FakeSTSClient:
    def __init__(self, *, response=None, error=None) -> None:
        self.response = response
        self.error = error
        self.calls = []

    def assume_role(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


class FakeSession:
    def __init__(self, *, sts_client: FakeSTSClient) -> None:
        self.sts_client = sts_client
        self.client_calls = []

    def client(self, service_name, **kwargs):
        self.client_calls.append((service_name, kwargs))
        assert service_name == "sts"
        return self.sts_client


class FakeClientSession:
    def __init__(self) -> None:
        self.region_name = "us-east-1"
        self.profile_name = "profile-a"
        self.client_calls = []

    def client(self, service_name, *args, **kwargs):
        self.client_calls.append((service_name, args, kwargs))
        return object()


def test_create_base_session_omits_none_profile(monkeypatch):
    FakeBotoSession.created = []
    monkeypatch.setattr(session_module.boto3, "Session", FakeBotoSession)

    session = SessionFactory().create_base_session(
        profile_name=None, region_name="us-east-1"
    )

    assert session.kwargs == {"region_name": "us-east-1"}


def test_create_base_session_passes_profile_name(monkeypatch):
    FakeBotoSession.created = []
    monkeypatch.setattr(session_module.boto3, "Session", FakeBotoSession)

    session = SessionFactory().create_base_session(
        profile_name="profile-a", region_name="us-east-1"
    )

    assert session.kwargs == {"profile_name": "profile-a", "region_name": "us-east-1"}


def test_get_worker_session_caches_by_profile_and_region(monkeypatch):
    FakeBotoSession.created = []
    monkeypatch.setattr(session_module.boto3, "Session", FakeBotoSession)

    factory = SessionFactory()

    first = factory.get_worker_session(
        profile_name="profile-a", region_name="us-east-1"
    )
    second = factory.get_worker_session(
        profile_name="profile-a", region_name="us-east-1"
    )
    third = factory.get_worker_session(
        profile_name="profile-a", region_name="us-west-2"
    )

    assert first is second
    assert third is not first
    assert [session.kwargs for session in FakeBotoSession.created] == [
        {"profile_name": "profile-a", "region_name": "us-east-1"},
        {"profile_name": "profile-a", "region_name": "us-west-2"},
    ]


def test_get_worker_session_cache_is_thread_local(monkeypatch):
    FakeBotoSession.created = []
    monkeypatch.setattr(session_module.boto3, "Session", FakeBotoSession)

    factory = SessionFactory()
    barrier = threading.Barrier(3)
    sessions: list[object] = []
    sessions_lock = threading.Lock()

    def get_session_twice() -> None:
        barrier.wait()
        first = factory.get_worker_session(
            profile_name="profile-a", region_name="us-east-1"
        )
        second = factory.get_worker_session(
            profile_name="profile-a", region_name="us-east-1"
        )
        assert first is second
        with sessions_lock:
            sessions.append(first)

    threads = [threading.Thread(target=get_session_twice) for _ in range(2)]
    for thread in threads:
        thread.start()

    barrier.wait()
    for thread in threads:
        thread.join()

    assert len(sessions) == 2
    assert sessions[0] is not sessions[1]
    assert [session.kwargs for session in FakeBotoSession.created] == [
        {"profile_name": "profile-a", "region_name": "us-east-1"},
        {"profile_name": "profile-a", "region_name": "us-east-1"},
    ]


def test_assume_role_credentials_maps_sts_response():
    sts_client = FakeSTSClient(
        response={
            "Credentials": {
                "AccessKeyId": "access",
                "SecretAccessKey": "secret",
                "SessionToken": "token",
                "Expiration": "expiration",
            }
        }
    )
    session = FakeSession(sts_client=sts_client)

    credentials = SessionFactory().assume_role_credentials(
        session=session,
        account_id="123456789012",
        role_name="TestRole",
        role_session_name="test-session",
    )

    assert credentials == AssumedRoleCredentials(
        access_key_id="access",
        secret_access_key="secret",
        session_token="token",
        expiration="expiration",
    )
    assert sts_client.calls == [
        {
            "RoleArn": "arn:aws:iam::123456789012:role/TestRole",
            "RoleSessionName": "test-session",
        }
    ]


def test_assume_role_credentials_reraises_client_error():
    error = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "denied"}}, "AssumeRole"
    )
    session = FakeSession(sts_client=FakeSTSClient(error=error))

    with pytest.raises(ClientError):
        SessionFactory().assume_role_credentials(
            session=session, account_id="123456789012", role_name="TestRole"
        )


def test_create_session_from_credentials_passes_credentials_to_boto3(monkeypatch):
    FakeBotoSession.created = []
    monkeypatch.setattr(session_module.boto3, "Session", FakeBotoSession)

    credentials = AssumedRoleCredentials(
        access_key_id="access", secret_access_key="secret", session_token="token"
    )

    session = SessionFactory().create_session_from_credentials(
        credentials=credentials, region_name="us-west-2"
    )

    assert session.kwargs == {
        "aws_access_key_id": "access",
        "aws_secret_access_key": "secret",
        "aws_session_token": "token",
        "region_name": "us-west-2",
    }


def test_cached_client_session_reuses_matching_client_calls():
    raw_session = FakeClientSession()
    session = SessionFactory().create_cached_client_session(session=raw_session)

    first = session.client("ec2")
    second = session.client("ec2")

    assert first is second
    assert raw_session.client_calls == [("ec2", (), {})]
    assert session.region_name == "us-east-1"
    assert session.profile_name == "profile-a"


def test_cached_client_session_separates_services_and_kwargs():
    raw_session = FakeClientSession()
    session = SessionFactory().create_cached_client_session(session=raw_session)

    ec2_default = session.client("ec2")
    ec2_west = session.client("ec2", region_name="us-west-2")
    s3_default = session.client("s3")

    assert ec2_default is not ec2_west
    assert ec2_default is not s3_default
    assert raw_session.client_calls == [
        ("ec2", (), {}),
        ("ec2", (), {"region_name": "us-west-2"}),
        ("s3", (), {}),
    ]
