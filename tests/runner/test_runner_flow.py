from types import SimpleNamespace

from anvil.descriptors import ConfigBranch
from anvil.results import ExecutionStatus
from anvil.runner import run_multiple_targets


def test_runner_auth_failure_short_circuits(monkeypatch):
    monkeypatch.setattr(
        "anvil.runner.auth_check",
        lambda **_: SimpleNamespace(
            status=ExecutionStatus.ERROR,
            is_error=True,
            message="fail",
            to_dict=lambda **kwargs: {"status": "error"},
        ),
    )

    target = SimpleNamespace(
        config_branch=ConfigBranch.ORGANIZATIONS,
        is_organization_config=True,
        name="org",
        profile=None,
        tasks=[],
        regions=["us-east-1"],
        role_name="role",
        metadata={},
        max_workers=1,
        include=None,
        exclude=None,
        dry_run=True,
        fail_fast=True,
    )

    engine_result = run_multiple_targets(
        targets=[target], cli_dry_run=True, cli_include=None, cli_exclude=None
    )

    assert engine_result.auth_results[0].status is ExecutionStatus.ERROR
    assert engine_result.target_results == []
    assert engine_result.has_auth_failures
