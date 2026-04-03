from anvil.descriptors import ConfigBranch, TargetDescriptor
from anvil.results import AuthResult, ExecutionStatus
from anvil.runner import run_multiple_targets


def test_runner_auth_failure_short_circuits(monkeypatch):
    monkeypatch.setattr(
        "anvil.runner.auth_check",
        lambda **kwargs: AuthResult(
            target_name=kwargs["target_name"],
            status=ExecutionStatus.ERROR,
            source="test",
            started_at="start",
            ended_at="end",
            duration_seconds=0.0,
            message="fail",
        ),
    )

    target = TargetDescriptor(
        config_branch=ConfigBranch.ORGANIZATIONS,
        name="org",
        tasks=[],
        regions=["us-east-1"],
        role_name="role",
        max_workers=1,
        dry_run=True,
        fail_fast=True,
    )

    engine_result = run_multiple_targets(
        targets=[target],
        max_parallel_targets=1,
        cli_dry_run=True,
        cli_include=None,
        cli_exclude=None,
    )

    assert engine_result.auth_results[0].status is ExecutionStatus.ERROR
    assert engine_result.target_results == []
    assert engine_result.has_auth_failures
