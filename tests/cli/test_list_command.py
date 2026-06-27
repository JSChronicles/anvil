from __future__ import annotations

from types import SimpleNamespace

import pytest


def _import_cli_or_skip():
    try:
        from anvil import cli
    except PermissionError as error:
        pytest.skip(f"jsonschema package resources unavailable in test env: {error}")

    return cli


def _run_main_with_args(monkeypatch, argv: list[str]):
    cli = _import_cli_or_skip()
    seen = {}

    def fake_cmd_list(args):
        seen["args"] = args
        return 0

    monkeypatch.setattr(cli, "_cmd_list", fake_cmd_list)
    monkeypatch.setattr("sys.argv", argv)

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    return seen["args"]


def test_list_cli_parses_task_listing(monkeypatch):
    args = _run_main_with_args(monkeypatch, ["anvil", "list", "--tasks"])

    assert args.tasks is True
    assert args.processors is False


def test_list_cli_parses_processor_listing(monkeypatch):
    args = _run_main_with_args(monkeypatch, ["anvil", "list", "--processors"])

    assert args.tasks is False
    assert args.processors is True
    assert args.providers is False


def test_list_cli_parses_provider_listing(monkeypatch):
    args = _run_main_with_args(monkeypatch, ["anvil", "list", "--providers"])

    assert args.tasks is False
    assert args.processors is False
    assert args.providers is True


def test_list_cli_requires_selector(monkeypatch, capsys):
    cli = _import_cli_or_skip()
    monkeypatch.setattr("sys.argv", ["anvil", "list"])

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 2
    assert (
        "One of --tasks, --processors, or --providers is required."
        in capsys.readouterr().err
    )


def test_list_cli_rejects_multiple_selectors(monkeypatch, capsys):
    cli = _import_cli_or_skip()
    monkeypatch.setattr("sys.argv", ["anvil", "list", "--tasks", "--processors"])

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 2
    assert "--tasks, --processors cannot be used together" in capsys.readouterr().err


def test_list_cli_rejects_provider_with_other_selectors(monkeypatch, capsys):
    cli = _import_cli_or_skip()
    monkeypatch.setattr("sys.argv", ["anvil", "list", "--tasks", "--providers"])

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 2
    assert "--tasks, --providers cannot be used together" in capsys.readouterr().err


def test_list_cli_removes_old_tasks_list_command(monkeypatch, capsys):
    cli = _import_cli_or_skip()
    monkeypatch.setattr("sys.argv", ["anvil", "tasks", "list"])

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 2
    assert "invalid choice: 'tasks'" in capsys.readouterr().err


def test_list_cli_removes_old_processors_list_command(monkeypatch, capsys):
    cli = _import_cli_or_skip()
    monkeypatch.setattr("sys.argv", ["anvil", "processors", "list"])

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 2
    assert "invalid choice: 'processors'" in capsys.readouterr().err


def test_cmd_list_tasks_groups_by_source(monkeypatch, capsys):
    cli = _import_cli_or_skip()
    monkeypatch.setattr(
        cli,
        "list_tasks",
        lambda: [
            cli.TaskDescriptor(name="count_vpc", load=lambda: None, source="stock"),
            cli.TaskDescriptor(name="noop", load=lambda: None, source="stock"),
            cli.TaskDescriptor(
                name="custom_task", load=lambda: None, source="plugin: my-plugin"
            ),
        ],
    )

    assert cli._cmd_list(SimpleNamespace(tasks=True, processors=False)) == 0

    assert capsys.readouterr().out == (
        "Available tasks:\n"
        "stock:\n"
        "  - count_vpc\n"
        "  - noop\n"
        "\n"
        "plugin: my-plugin:\n"
        "  - custom_task\n"
    )


def test_cmd_list_processors_groups_by_source(monkeypatch, capsys):
    cli = _import_cli_or_skip()
    monkeypatch.setattr(
        cli,
        "list_processors",
        lambda: [
            cli.ProcessorDescriptor(
                name="summary_json", load=lambda: None, source="stock"
            ),
            cli.ProcessorDescriptor(
                name="custom_report", load=lambda: None, source="plugin: my-plugin"
            ),
        ],
    )

    assert cli._cmd_list(SimpleNamespace(tasks=False, processors=True)) == 0

    assert capsys.readouterr().out == (
        "Available processors:\n"
        "stock:\n"
        "  - summary_json\n"
        "\n"
        "plugin: my-plugin:\n"
        "  - custom_report\n"
    )


def test_cmd_list_providers_groups_by_source(monkeypatch, capsys):
    cli = _import_cli_or_skip()
    monkeypatch.setattr(
        cli,
        "list_providers",
        lambda: [
            cli.ProviderDescriptor(
                name="aws", display_name="AWS", load=lambda: None, source="stock"
            ),
            cli.ProviderDescriptor(
                name="custom",
                display_name="Custom",
                load=lambda: None,
                source="plugin: my-plugin",
            ),
        ],
    )

    args = SimpleNamespace(tasks=False, processors=False, providers=True)
    assert cli._cmd_list(args) == 0

    assert capsys.readouterr().out == (
        "Available providers:\nstock:\n  - aws\n\nplugin: my-plugin:\n  - custom\n"
    )


def test_cmd_list_providers_does_not_call_azure_subscription_discovery(
    monkeypatch, capsys
):
    cli = _import_cli_or_skip()

    monkeypatch.setattr(
        "anvil.providers.azure.provider.AzureSessionFactory.list_subscriptions",
        lambda self, **kwargs: (_ for _ in ()).throw(
            AssertionError("provider listing should not discover Azure subscriptions")
        ),
    )

    args = SimpleNamespace(tasks=False, processors=False, providers=True)
    assert cli._cmd_list(args) == 0

    output = capsys.readouterr().out
    assert "Available providers:" in output
    assert "azure" in output


def test_list_help_shows_new_flags_and_not_old_groups(monkeypatch, capsys):
    cli = _import_cli_or_skip()
    monkeypatch.setattr("sys.argv", ["anvil", "list", "--help"])

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "--tasks" in output
    assert "--processors" in output
    assert "--providers" in output
    assert "tasks list" not in output
    assert "processors list" not in output
