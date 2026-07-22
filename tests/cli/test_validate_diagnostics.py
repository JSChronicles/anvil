from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest


def _import_cli_or_skip():
    try:
        from anvil import cli
    except PermissionError as error:
        pytest.skip(f"jsonschema package resources unavailable in test env: {error}")

    return cli


def test_validate_cli_allows_no_category_switches(monkeypatch):
    cli = _import_cli_or_skip()
    seen = {}

    def fake_cmd_validate(args):
        seen["args"] = args
        return 0

    monkeypatch.setattr(cli, "_cmd_validate", fake_cmd_validate)
    monkeypatch.setattr("sys.argv", ["anvil", "validate"])

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    assert seen["args"].tasks is None
    assert seen["args"].processors is None
    assert seen["args"].providers is None
    assert seen["args"].auth is False
    assert seen["args"].config_file is None


def test_validate_no_categories_runs_offline_diagnostics(monkeypatch, capsys):
    cli = _import_cli_or_skip()
    args = SimpleNamespace(
        tasks=None,
        processors=None,
        providers=None,
        auth=False,
        config_file=None,
        include=None,
        exclude=None,
        quiet=False,
    )

    monkeypatch.setattr(
        cli,
        "_diagnostic_checks",
        lambda _: [cli.DiagnosticCheck("Environment", "OK", "Python", "3.14")],
    )

    assert cli._cmd_validate(args) == 0
    output = capsys.readouterr().out
    assert "Anvil Validation Diagnostics" in output
    assert "[OK] Python 3.14" in output


def test_validate_no_categories_returns_error_for_diagnostic_errors(
    monkeypatch, capsys
):
    cli = _import_cli_or_skip()
    args = SimpleNamespace(
        tasks=None,
        processors=None,
        providers=None,
        auth=False,
        config_file=None,
        include=None,
        exclude=None,
        quiet=False,
    )

    monkeypatch.setattr(
        cli,
        "_diagnostic_checks",
        lambda _: [cli.DiagnosticCheck("Config", "ERROR", "missing.yaml", "not found")],
    )

    assert cli._cmd_validate(args) == 1
    assert "[ERROR] missing.yaml not found" in capsys.readouterr().out


def test_validate_no_categories_respects_quiet(monkeypatch, capsys):
    cli = _import_cli_or_skip()
    args = SimpleNamespace(
        tasks=None,
        processors=None,
        providers=None,
        auth=False,
        config_file=None,
        include=None,
        exclude=None,
        quiet=True,
    )

    monkeypatch.setattr(
        cli,
        "_diagnostic_checks",
        lambda _: [cli.DiagnosticCheck("Environment", "OK", "Python", "3.14")],
    )

    assert cli._cmd_validate(args) == 0
    assert capsys.readouterr().out == ""


def test_diagnostic_dependency_checks_report_importable_and_missing(monkeypatch):
    cli = _import_cli_or_skip()

    monkeypatch.setattr(
        cli, "_module_available", lambda module_name: module_name in {"boto3", "github"}
    )

    checks = cli._diagnostic_dependency_checks()
    by_label = {check.label: check for check in checks}

    assert by_label["aws"].status == "OK"
    assert by_label["github"].status == "OK"
    assert by_label["azure"].status == "WARN"
    assert by_label["gcp"].status == "WARN"


def test_module_available_treats_missing_parent_package_as_unavailable(monkeypatch):
    cli = _import_cli_or_skip()

    def fake_find_spec(module_name):
        raise ModuleNotFoundError(module_name)

    monkeypatch.setattr(cli.importlib_util, "find_spec", fake_find_spec)

    assert cli._module_available("azure.identity") is False


def test_diagnostic_config_checks_load_config_without_auth(monkeypatch):
    cli = _import_cli_or_skip()
    config_file = Path("yaml/noop.yaml")
    loaded_config = SimpleNamespace(
        targets=[SimpleNamespace(provider="aws"), SimpleNamespace(provider="github")]
    )
    calls = []
    args = SimpleNamespace(config_file=[config_file], include=None, exclude=None)

    monkeypatch.setattr(
        cli,
        "_load_targets_from_config_file",
        lambda path: calls.append(("load", path)) or loaded_config,
    )
    monkeypatch.setattr(
        cli,
        "_validate_cli_overrides",
        lambda **kwargs: calls.append(("overrides", kwargs["loaded_config"])),
    )
    monkeypatch.setattr(
        cli,
        "_cmd_validate_auth",
        lambda _: (_ for _ in ()).throw(
            AssertionError("diagnostics should not run auth validation")
        ),
    )

    checks = cli._diagnostic_config_checks(args)

    assert calls == [("load", config_file), ("overrides", loaded_config)]
    assert checks == [
        cli.DiagnosticCheck(
            section="Config",
            status="OK",
            label=str(config_file),
            detail="2 target(s); providers: aws, github",
        )
    ]
