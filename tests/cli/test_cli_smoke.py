import pytest


def _import_cli_or_skip():
    try:
        from anvil import cli
    except PermissionError as error:
        pytest.skip(f"jsonschema package resources unavailable in test env: {error}")

    return cli


def test_cli_no_args_exits(monkeypatch):
    try:
        from anvil.cli import main
    except PermissionError as error:
        pytest.skip(f"jsonschema package resources unavailable in test env: {error}")

    monkeypatch.setattr("sys.argv", ["anvil"])
    with pytest.raises(SystemExit):
        main()


def test_cmd_run_processes_multiple_config_files_in_order(monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace

    cli = _import_cli_or_skip()

    seen_paths: list[Path] = []

    def fake_run_single_config_file(*, config_file, args):
        seen_paths.append(config_file)
        return 0

    monkeypatch.setattr(cli, "_run_single_config_file", fake_run_single_config_file)

    args = SimpleNamespace(
        config_file=[Path("orgs.yaml"), Path("orgs2.yaml"), Path("orgs3.yaml")]
    )

    exit_code = cli._cmd_run(args)

    assert exit_code == 0
    assert seen_paths == [Path("orgs.yaml"), Path("orgs2.yaml"), Path("orgs3.yaml")]


def test_cmd_run_returns_failure_if_any_config_file_fails(monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace

    cli = _import_cli_or_skip()

    exit_codes = {
        Path("orgs.yaml"): 0,
        Path("orgs2.yaml"): 1,
        Path("orgs3.yaml"): 0,
    }
    seen_paths: list[Path] = []

    def fake_run_single_config_file(*, config_file, args):
        seen_paths.append(config_file)
        return exit_codes[config_file]

    monkeypatch.setattr(cli, "_run_single_config_file", fake_run_single_config_file)

    args = SimpleNamespace(
        config_file=[Path("orgs.yaml"), Path("orgs2.yaml"), Path("orgs3.yaml")]
    )

    exit_code = cli._cmd_run(args)

    assert exit_code == 1
    assert seen_paths == [Path("orgs.yaml"), Path("orgs2.yaml"), Path("orgs3.yaml")]


def test_write_run_results_prefixes_summary_with_config_stem(monkeypatch):
    from pathlib import Path
    import shutil
    import tempfile
    from types import SimpleNamespace

    cli = _import_cli_or_skip()

    engine_result = SimpleNamespace(
        target_results=[
            SimpleNamespace(
                target_name="org2",
                to_dict=lambda: {"name": "org2"},
            )
        ],
        build_summary=lambda: {"state": "completed_success"},
    )

    temp_dir = Path(tempfile.mkdtemp(dir=Path.cwd()))
    original_cwd = Path.cwd()
    monkeypatch.chdir(temp_dir)

    try:
        cli._write_run_results(
            config_file=Path("yaml/orgs.yaml"),
            engine_result=engine_result,
        )

        assert (temp_dir / "results" / "orgs-target-summary.json").exists()
        assert (temp_dir / "results" / "org2.json").exists()
    finally:
        monkeypatch.chdir(original_cwd)
        shutil.rmtree(temp_dir)
