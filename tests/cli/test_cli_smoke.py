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

    exit_codes = {Path("orgs.yaml"): 0, Path("orgs2.yaml"): 1, Path("orgs3.yaml"): 0}
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


def test_write_run_results_uses_config_stem_and_run_id_directories(monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace

    from anvil.descriptors import ConfigBranch

    cli = _import_cli_or_skip()
    scratch_dir = (Path("tests") / "_tmp" / "cli-smoke").resolve()
    run_dir = scratch_dir / "results" / "orgs" / "2026-05-01T120000Z"
    target_dir = run_dir / "organizations"
    summary_path = run_dir / "summary.json"
    target_path = target_dir / "org2.json"
    jsonl_path = run_dir / "results.jsonl"

    engine_result = SimpleNamespace(
        config_branch=ConfigBranch.ORGANIZATIONS,
        benchmark=None,
        target_results=[
            SimpleNamespace(
                config_branch=ConfigBranch.ORGANIZATIONS,
                target_name="org2",
                generated_at="2026-04-30T00:00:00+00:00",
                dry_run=True,
                account_results=[],
                to_dict=lambda: {"name": "org2"},
            )
        ],
        build_summary=lambda: {"state": "completed_success"},
    )

    original_cwd = Path.cwd()
    scratch_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(scratch_dir)
    monkeypatch.setattr(cli, "_build_run_id", lambda: "2026-05-01T120000Z")

    try:
        written_results = cli._write_run_results(
            config_file=Path("yaml/orgs.yaml"), engine_result=engine_result
        )

        assert summary_path.exists()
        assert target_path.exists()
        assert jsonl_path.exists()
        assert written_results.run_dir == run_dir
        assert written_results.summary_path == summary_path
        assert written_results.jsonl_path == jsonl_path
        assert written_results.summary == {"state": "completed_success"}
        assert written_results.target_file_count == 1
        assert written_results.jsonl_record_count == 0
    finally:
        monkeypatch.chdir(original_cwd)
        summary_path.unlink(missing_ok=True)
        target_path.unlink(missing_ok=True)
        jsonl_path.unlink(missing_ok=True)
        if target_dir.exists():
            target_dir.rmdir()
        if run_dir.exists():
            run_dir.rmdir()
        config_dir = scratch_dir / "results" / "orgs"
        if config_dir.exists():
            config_dir.rmdir()
        results_dir = scratch_dir / "results"
        if results_dir.exists():
            results_dir.rmdir()
        if scratch_dir.exists():
            scratch_dir.rmdir()


def test_target_result_file_path_avoids_sanitized_name_collisions():
    from pathlib import Path

    cli = _import_cli_or_skip()
    scratch_dir = (Path("tests") / "_tmp" / "cli-target-files").resolve()

    try:
        scratch_dir.mkdir(parents=True)
        existing_path = scratch_dir / "org_a.json"
        existing_path.write_text("{}", encoding="utf-8")

        result_path = cli._target_result_file_path(
            target_results_dir=scratch_dir, target_name="org/a"
        )

        assert result_path == scratch_dir / "org_a-1.json"
    finally:
        for path in scratch_dir.glob("*.json"):
            path.unlink()
        if scratch_dir.exists():
            scratch_dir.rmdir()


def test_print_failure_followups_uses_results_file_command(capsys, monkeypatch):
    from pathlib import Path

    cli = _import_cli_or_skip()
    scratch_dir = (Path("tests") / "_tmp" / "cli-followups").resolve()
    results_file = scratch_dir / "results" / "orgs" / "run-a" / "results.jsonl"
    original_cwd = Path.cwd()

    try:
        results_file.parent.mkdir(parents=True)
        monkeypatch.chdir(scratch_dir)

        cli._print_failure_followups(results_file=results_file)

        output = capsys.readouterr().out
        assert (
            "anvil results --status failed --results-file "
            "./results/orgs/run-a/results.jsonl" in output
        )
        assert (
            "anvil results --status failed --results-file "
            "./results/orgs/run-a/results.jsonl --rerun"
        ) in output
    finally:
        monkeypatch.chdir(original_cwd)
        if results_file.parent.exists():
            results_file.parent.rmdir()
        run_parent = scratch_dir / "results" / "orgs"
        if run_parent.exists():
            run_parent.rmdir()
        results_dir = scratch_dir / "results"
        if results_dir.exists():
            results_dir.rmdir()
        if scratch_dir.exists():
            scratch_dir.rmdir()


def test_build_rerun_targets_narrows_accounts_regions_and_task_dependencies():
    from anvil.descriptors import ConfigBranch, LoadedConfig, TargetDescriptor
    from anvil.result_query import build_rerun_targets

    loaded_config = LoadedConfig(
        branch=ConfigBranch.ORGANIZATIONS,
        targets=[
            TargetDescriptor(
                config_branch=ConfigBranch.ORGANIZATIONS,
                name="org-a",
                regions=["us-east-1", "us-west-2"],
                include=["111111111111", "222222222222"],
                tasks=[
                    {"name": "inventory"},
                    {"name": "cleanup", "depends_on": ["inventory"]},
                    {"name": "notify"},
                ],
            ),
            TargetDescriptor(
                config_branch=ConfigBranch.ORGANIZATIONS,
                name="org-b",
                regions=["us-east-1"],
                tasks=[{"name": "inventory"}],
            ),
        ],
    )

    targets = build_rerun_targets(
        loaded_config=loaded_config,
        failures=[
            {
                "record_type": "account",
                "target": "org-a",
                "account_id": "111111111111",
                "status": "error",
            },
            {
                "record_type": "task",
                "target": "org-a",
                "account_id": "111111111111",
                "region": "us-west-2",
                "task": "cleanup",
                "status": "error",
            },
            {
                "record_type": "account",
                "target": "org-a",
                "account_id": "222222222222",
                "status": "interrupted",
            },
        ],
    )

    assert len(targets) == 2
    assert targets[0].name == "org-a"
    assert targets[0].include == ["111111111111"]
    assert targets[0].exclude is None
    assert targets[0].regions == ["us-west-2"]
    assert targets[0].tasks == [
        {"name": "inventory"},
        {"name": "cleanup", "depends_on": ["inventory"]},
    ]
    assert targets[1].include == ["222222222222"]
    assert targets[1].regions == ["us-east-1", "us-west-2"]
    assert targets[1].tasks == [
        {"name": "inventory"},
        {"name": "cleanup", "depends_on": ["inventory"]},
        {"name": "notify"},
    ]


def test_cmd_results_filters_account_type_status_and_outputs_json(capsys):
    from pathlib import Path
    from types import SimpleNamespace

    cli = _import_cli_or_skip()
    scratch_dir = (Path("tests") / "_tmp" / "cli-results").resolve()
    jsonl_path = scratch_dir / "results.jsonl"
    scratch_dir.mkdir(parents=True, exist_ok=True)

    try:
        jsonl_path.write_text(
            "\n".join(
                [
                    (
                        '{"record_type":"account","target":"org-a","account_id":'
                        '"111111111111","account_alias":"dev","status":"error"}'
                    ),
                    (
                        '{"record_type":"account","target":"org-a","account_id":'
                        '"222222222222","account_alias":"prod","status":"success"}'
                    ),
                    (
                        '{"record_type":"account","target":"org-a","account_id":'
                        '"333333333333","account_alias":"qa","status":"interrupted"}'
                    ),
                    (
                        '{"record_type":"task","target":"org-a","account_id":'
                        '"444444444444","account_alias":"ops","status":"error"}'
                    ),
                ]
            ),
            encoding="utf-8",
        )

        args = SimpleNamespace(
            results_file=[Path(jsonl_path)],
            type="account",
            status="failed",
            target=None,
            account=None,
            region=None,
            task=None,
            fields=None,
            limit=None,
            json=True,
            jsonl=False,
            rerun=False,
        )

        assert cli._cmd_results(args) == 0
        output = capsys.readouterr().out

        assert '"account_id": "111111111111"' in output
        assert '"account_id": "222222222222"' not in output
        assert '"account_id": "333333333333"' in output
        assert '"account_id": "444444444444"' not in output
    finally:
        jsonl_path.unlink(missing_ok=True)
        if scratch_dir.exists():
            scratch_dir.rmdir()


def test_cmd_results_outputs_jsonl_with_fields_and_limit(capsys):
    from pathlib import Path
    from types import SimpleNamespace

    cli = _import_cli_or_skip()
    scratch_dir = (Path("tests") / "_tmp" / "cli-results-jsonl").resolve()
    jsonl_path = scratch_dir / "results.jsonl"
    scratch_dir.mkdir(parents=True, exist_ok=True)

    try:
        jsonl_path.write_text(
            "\n".join(
                [
                    (
                        '{"record_type":"task","target":"org-a","account_id":'
                        '"111111111111","account_alias":"dev","region":"us-east-1",'
                        '"task":"count_vpcs","status":"error","error":"boom"}'
                    ),
                    (
                        '{"record_type":"task","target":"org-a","account_id":'
                        '"222222222222","account_alias":"prod","region":"us-west-2",'
                        '"task":"count_vpcs","status":"error","error":"nope"}'
                    ),
                ]
            ),
            encoding="utf-8",
        )

        args = SimpleNamespace(
            results_file=[Path(jsonl_path)],
            type="task",
            status="failed",
            target=None,
            account=None,
            region=None,
            task="count_vpcs",
            fields="account_id,region,error",
            limit=1,
            json=False,
            jsonl=True,
            rerun=False,
        )

        assert cli._cmd_results(args) == 0
        output = capsys.readouterr().out

        assert output.count("\n") == 1
        assert '"account_id":"111111111111"' in output
        assert '"region":"us-east-1"' in output
        assert '"task"' not in output
        assert "222222222222" not in output
    finally:
        jsonl_path.unlink(missing_ok=True)
        if scratch_dir.exists():
            scratch_dir.rmdir()


def test_cmd_results_rerun_rejects_report_only_flags():
    from types import SimpleNamespace

    cli = _import_cli_or_skip()
    args = SimpleNamespace(
        rerun=True, type="account", fields="account_id", limit=1, json=True, jsonl=False
    )

    with pytest.raises(ValueError, match="--type, --fields, --limit, --json"):
        cli._cmd_results(args)
