from types import SimpleNamespace

import pytest


def _import_cli_or_skip():
    try:
        from anvil import cli
    except PermissionError as error:
        pytest.skip(f"jsonschema package resources unavailable in test env: {error}")

    return cli


@pytest.mark.parametrize(
    ("args", "expected_benchmark"),
    [
        (SimpleNamespace(dry_run=None, include=None, exclude=None), False),
        (
            SimpleNamespace(dry_run=None, include=None, exclude=None, benchmark=True),
            True,
        ),
    ],
)
def test_run_single_config_file_passes_run_controls(
    monkeypatch, args, expected_benchmark
):
    from pathlib import Path

    from anvil.descriptors import ConfigBranch, TargetDescriptor

    cli = _import_cli_or_skip()

    target = TargetDescriptor(config_branch=ConfigBranch.ORGANIZATIONS, name="target-a")
    loaded_config = SimpleNamespace(
        branch=SimpleNamespace(value="organizations"),
        targets=[target],
        max_parallel_targets=4,
    )
    seen = {}

    monkeypatch.setattr(
        cli, "_load_targets_from_config_file", lambda path: loaded_config
    )
    monkeypatch.setattr(cli, "_validate_cli_overrides", lambda **kwargs: None)

    def fake_run_multiple_targets(**kwargs):
        seen["kwargs"] = kwargs
        return SimpleNamespace(
            state=cli.EngineState.COMPLETED_SUCCESS, target_results=[]
        )

    monkeypatch.setattr(cli, "run_multiple_targets", fake_run_multiple_targets)
    monkeypatch.setattr(
        cli,
        "_write_run_results",
        lambda **kwargs: SimpleNamespace(
            run_dir=Path("results/orgs/run"),
            summary_path=Path("results/orgs/run/summary.json"),
            jsonl_path=Path("results/orgs/run/results.jsonl"),
            summary={},
            target_result_paths={},
        ),
    )

    exit_code = cli._run_single_config_file(config_file=Path("orgs.yaml"), args=args)

    assert exit_code == 0
    assert seen["kwargs"]["max_parallel_targets"] == 4
    assert seen["kwargs"]["benchmark_enabled"] is expected_benchmark
