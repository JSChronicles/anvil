from types import SimpleNamespace

import pytest


def _import_cli_or_skip():
    try:
        from anvil import cli
    except PermissionError as error:
        pytest.skip(f"jsonschema package resources unavailable in test env: {error}")

    return cli


def test_run_single_config_file_passes_max_parallel_targets(monkeypatch):
    from pathlib import Path

    cli = _import_cli_or_skip()

    loaded_config = SimpleNamespace(
        branch=SimpleNamespace(value="organizations"),
        targets=["target-a"],
        max_parallel_targets=4,
    )
    args = SimpleNamespace(dry_run=None, include=None, exclude=None)
    seen = {}

    monkeypatch.setattr(cli, "_load_targets_from_config_file", lambda path: loaded_config)
    monkeypatch.setattr(cli, "_validate_cli_overrides", lambda **kwargs: None)

    def fake_run_multiple_targets(**kwargs):
        seen["kwargs"] = kwargs
        return SimpleNamespace(state=cli.EngineState.COMPLETED_SUCCESS)

    monkeypatch.setattr(cli, "run_multiple_targets", fake_run_multiple_targets)
    monkeypatch.setattr(cli, "_write_run_results", lambda **kwargs: None)

    exit_code = cli._run_single_config_file(config_file=Path("orgs.yaml"), args=args)

    assert exit_code == 0
    assert seen["kwargs"]["max_parallel_targets"] == 4
