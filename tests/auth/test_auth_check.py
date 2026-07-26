from botocore.exceptions import TokenRetrievalError

from anvil.providers.aws.auth import AuthSource, auth_check
from anvil.results import ExecutionStatus


def test_auth_check_success(patch_boto3_success):
    result = auth_check(
        target_name="test-org", profile=None, auth_source=AuthSource.ENVIRONMENT
    )

    assert result.status is ExecutionStatus.SUCCESS
    assert result.source == "environment"
    assert result.duration_seconds >= 0


def test_auth_check_failure_no_credentials(patch_boto3_failure):
    result = auth_check(
        target_name="test-org", profile=None, auth_source=AuthSource.ENVIRONMENT
    )

    assert result.status is ExecutionStatus.ERROR
    assert result.message == "No AWS credentials available."


def test_auth_check_maps_sso_token_retrieval_error(monkeypatch):
    class FailingSTSClient:
        def get_caller_identity(self):
            raise TokenRetrievalError(
                provider="sso", error_msg="Token has expired and refresh failed"
            )

    class FailingSession:
        def __init__(self, *args, **kwargs):
            pass

        def client(self, service_name):
            assert service_name == "sts"
            return FailingSTSClient()

    import boto3

    monkeypatch.setattr(boto3, "Session", FailingSession)

    result = auth_check(
        target_name="test-org", profile="chronicles", auth_source=AuthSource.SSO
    )

    assert result.status is ExecutionStatus.ERROR
    assert result.message == "AWS SSO session is invalid or expired."
    assert result.remediation == "aws sso login --profile chronicles"
