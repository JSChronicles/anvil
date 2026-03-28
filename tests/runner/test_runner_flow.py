from types import SimpleNamespace

from anvil.results import ExecutionStatus
from anvil.runner import run_multiple_orgs


def test_runner_auth_failure_short_circuits(monkeypatch):
    monkeypatch.setattr(
        "anvil.runner.auth_check",
        lambda **_: SimpleNamespace(
            status=ExecutionStatus.ERROR,
            is_error=True,
            message="fail",
            to_dict=lambda: {"status": "error"},
        ),
    )

    org = SimpleNamespace(
        name="org",
        profile=None,
        tasks=[],
        region="us-east-1",
        role_name="role",
        metadata={},
        max_workers=1,
        include=None,
        exclude=None,
        dry_run=True,
        fail_fast=True,
    )

    engine_result = run_multiple_orgs(
        orgs=[org], cli_dry_run=True, cli_include=None, cli_exclude=None
    )

    # Auth failure should be recorded
    assert engine_result.auth_results[0].status is ExecutionStatus.ERROR

    # Fail-fast should prevent organization execution
    assert engine_result.organization_results == []

    # Optional: stronger semantic assertion
    assert engine_result.has_auth_failures
