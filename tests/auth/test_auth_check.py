from anvil.auth import AuthSource, auth_check
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
