from __future__ import annotations

import importlib
from types import SimpleNamespace


def test_graph_and_run_paths_behave_the_same_with_cached_resolution(monkeypatch, capsys):
    task_loader = importlib.import_module("anvil.task_loader")
    graph = importlib.import_module("anvil.graph")
    runner = importlib.import_module("anvil.runner")
    descriptors = importlib.import_module("anvil.descriptors")
    results = importlib.import_module("anvil.results")

    task_loader._resolve_tasks_cached.cache_clear()
    task_loader._load_task_callable.cache_clear()

    def alpha_run(**kwargs):
        return "alpha"

    def beta_run(**kwargs):
        return "beta"

    def fake_load(task_name: str):
        return {"alpha": alpha_run, "beta": beta_run}[task_name]

    monkeypatch.setattr(task_loader, "_load_task_callable", fake_load)

    org = descriptors.OrgDescriptor(
        name="demo-org",
        tasks=[
            {"name": "alpha"},
            {"name": "beta", "depends_on": ["alpha"]},
        ],
    )

    graph.render_graph(orgs=[org], output_json=True)
    graph_output = capsys.readouterr().out
    assert '"organization": "demo-org"' in graph_output
    assert '"name": "alpha"' in graph_output
    assert '"name": "beta"' in graph_output

    monkeypatch.setattr(
        runner,
        "auth_check",
        lambda **kwargs: results.AuthResult(
            org_name=kwargs["org_name"],
            status=results.ExecutionStatus.SUCCESS,
            source="test",
            started_at="start",
            ended_at="end",
            duration_seconds=0.0,
            message="ok",
        ),
    )
    monkeypatch.setattr(runner, "infer_auth_source", lambda profile: SimpleNamespace(value="test"))

    observed_tasks: list[list[str]] = []

    class FakeOrganization:
        def __init__(self, *, context, **kwargs):
            observed_tasks.append([task.name for task in context.tasks])
            self.context = context
            self.name = kwargs["name"]

        def execute(self):
            return results.OrgResult.create(
                org_name=self.name,
                dry_run=self.context.dry_run,
                account_results=[],
            )

    monkeypatch.setattr(runner, "Organization", FakeOrganization)

    engine_result = runner.run_multiple_orgs(
        orgs=[org, org],
        cli_dry_run=None,
        cli_include=None,
        cli_exclude=None,
    )

    assert observed_tasks == [["alpha", "beta"], ["alpha", "beta"]]
    assert engine_result.state is results.EngineState.COMPLETED_SUCCESS
    assert len(engine_result.organization_results) == 2
