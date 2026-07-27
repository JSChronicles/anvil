from __future__ import annotations

import importlib

from anvil._components import ComponentCatalog, ComponentOrigin, ComponentSource
from anvil.providers.base import (
    ProviderAuthResult,
    ProviderExecutionPlan,
    ProviderMetadata,
    ProviderPreparation,
)
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

    source = ComponentSource(
        origin=ComponentOrigin.STOCK,
        package="tests.tasks",
        label="test",
        provider="test",
    )
    task_catalog = ComponentCatalog.build(
        [
            TaskDescriptor("alpha", source, lambda: alpha_run),
            TaskDescriptor("beta", source, lambda: beta_run),
        ]
    )
    monkeypatch.setattr(
        task_loader, "_provider_task_catalog", lambda provider_name: task_catalog
    )
    task_loader._load_provider_task_callable.cache_clear()
    task_loader._resolve_tasks_cached.cache_clear()

    target = descriptors.TargetDescriptor(
        config_branch=descriptors.ConfigBranch.TARGETS,
        name="demo-org",
        provider="test",
        mode="fleet",
        tasks=[{"name": "alpha"}, {"name": "beta", "depends_on": ["alpha"]}],
    )

    class FakeProvider:
        metadata = ProviderMetadata(
            name="test",
            display_name="Test",
            supported_task_scopes=frozenset({"region"}),
            default_regions=("global",),
        )

        def resolve_target_filters(self, *, target, include_override, exclude_override):
            return target.include, target.exclude

        def validate_target(self, target):
            return None

        def auth_cache_key(self, target):
            return None

        def auth_check(self, target):
            return ProviderAuthResult(
                status=results.ExecutionStatus.SUCCESS, source="test"
            )

        def prepare_target(self, **kwargs):
            return ProviderPreparation()

        def resolve_execution_targets(self, **kwargs):
            return ProviderExecutionPlan(execution_targets=[])

    monkeypatch.setattr(runner, "_load_provider", lambda provider_name: FakeProvider())

    observed_tasks: list[list[str]] = []

    def fake_execute_provider_targets(*, target, context, execution_targets, **kwargs):
        observed_tasks.append([task.name for task in context.tasks])
        return results.TargetResult.create(
            config_branch=target.config_branch,
            target_name=target.name,
            provider=target.provider,
            dry_run=context.dry_run,
            entities=[],
        )

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
