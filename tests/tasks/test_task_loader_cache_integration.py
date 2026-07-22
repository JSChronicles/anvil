from __future__ import annotations

import importlib
from types import SimpleNamespace

from anvil.task_loader import TaskDescriptor


def test_run_path_reuses_cached_task_resolution(monkeypatch):
    task_loader = importlib.import_module("anvil.task_loader")
    runner = importlib.import_module("anvil.runner")
    descriptors = importlib.import_module("anvil.descriptors")
    results = importlib.import_module("anvil.results")

    task_loader._resolve_tasks_cached.cache_clear()
    task_loader._clear_task_caches()

    def alpha_run(**kwargs):
        return "alpha"

    def beta_run(**kwargs):
        return "beta"

    monkeypatch.setattr(
        task_loader,
        "_provider_task_descriptor_index",
        lambda provider_name: {
            "alpha": (TaskDescriptor("alpha", lambda: alpha_run, "aws"),),
            "beta": (TaskDescriptor("beta", lambda: beta_run, "aws"),),
        },
    )
    task_loader._load_provider_task_callable.cache_clear()
    task_loader._resolve_tasks_cached.cache_clear()

    target = descriptors.TargetDescriptor(
        config_branch=descriptors.ConfigBranch.TARGETS,
        name="demo-org",
        tasks=[{"name": "alpha"}, {"name": "beta", "depends_on": ["alpha"]}],
    )

    monkeypatch.setattr(
        runner,
        "auth_check",
        lambda **kwargs: results.AuthResult(
            target_name=kwargs["target_name"],
            status=results.ExecutionStatus.SUCCESS,
            source="test",
            started_at="start",
            ended_at="end",
            duration_seconds=0.0,
            message="ok",
        ),
    )
    monkeypatch.setattr(
        runner, "infer_auth_source", lambda profile: SimpleNamespace(value="test")
    )
    monkeypatch.setattr(
        runner.AwsProvider,
        "preflight_execution",
        lambda self, **kwargs: SimpleNamespace(
            data=SimpleNamespace(
                session_factory=kwargs["session_factory"],
                base_session=object(),
                organization_id="o-example",
                management_account_id="123456789012",
                base_session_account_id="123456789012",
                discovered_accounts={},
                region_statuses={"us-east-1": "ENABLED_BY_DEFAULT"},
            ),
            exclusive_execution_key="o-example",
        ),
    )

    observed_tasks: list[list[str]] = []

    class FakeResolver:
        def __init__(self, *, descriptor, context, **kwargs):
            self.descriptor = descriptor
            self.context = context

        def resolve_accounts(self):
            return []

    def fake_execute_provider_targets(*, target, context, execution_targets, **kwargs):
        observed_tasks.append([task.name for task in context.tasks])
        return results.TargetResult.create(
            config_branch=target.config_branch,
            target_name=target.name,
            dry_run=context.dry_run,
            entities=[],
        )

    monkeypatch.setattr(runner, "OrganizationResolver", FakeResolver)
    monkeypatch.setattr(
        runner, "_execute_provider_targets", fake_execute_provider_targets
    )

    engine_result = runner.run_multiple_targets(
        targets=[target, target],
        max_parallel_targets=1,
        cli_dry_run=None,
        cli_include=None,
        cli_exclude=None,
    )

    assert observed_tasks == [["alpha", "beta"], ["alpha", "beta"]]
    assert engine_result.state is results.EngineState.COMPLETED_SUCCESS
    assert len(engine_result.target_results) == 2
