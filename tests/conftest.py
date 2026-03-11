import pytest


class DummySTSClient:
    def get_caller_identity(self):
        return {"Account": "123456789012"}


class DummySession:
    def __init__(self, *args, **kwargs):
        pass

    def client(self, service_name):
        assert service_name == "sts"
        return DummySTSClient()


@pytest.fixture
def patch_boto3_success(monkeypatch):
    import boto3

    monkeypatch.setattr(boto3, "Session", lambda *a, **k: DummySession())


@pytest.fixture
def patch_boto3_failure(monkeypatch):
    import boto3
    from botocore.exceptions import NoCredentialsError

    class FailingSession:
        def __init__(self, *a, **k):
            raise NoCredentialsError()

    monkeypatch.setattr(boto3, "Session", FailingSession)
