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

    assert args.tasks == []
    assert args.processors is None
    assert args.detail is False


def test_list_cli_parses_processor_listing(monkeypatch):
    args = _run_main_with_args(monkeypatch, ["anvil", "list", "--processors"])

    assert args.tasks is None
    assert args.processors == []
    assert args.providers is False
    assert args.detail is False


def test_list_cli_parses_task_detail(monkeypatch):
    args = _run_main_with_args(
        monkeypatch, ["anvil", "list", "--tasks", "count_vpc", "--detail"]
    )

    assert args.tasks == ["count_vpc"]
    assert args.processors is None
    assert args.detail is True


def test_list_cli_parses_processor_detail(monkeypatch):
    args = _run_main_with_args(
        monkeypatch, ["anvil", "list", "--processors", "html_report", "--detail"]
    )

    assert args.tasks is None
    assert args.processors == ["html_report"]
    assert args.detail is True


def test_list_cli_parses_provider_listing(monkeypatch):
    args = _run_main_with_args(monkeypatch, ["anvil", "list", "--providers"])

    assert args.tasks is None
    assert args.processors is None
    assert args.providers is True
    assert args.detail is False


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


def test_list_cli_rejects_detail_without_name(monkeypatch, capsys):
    cli = _import_cli_or_skip()
    monkeypatch.setattr("sys.argv", ["anvil", "list", "--tasks", "--detail"])

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 2
    assert "--detail requires exactly one task or processor name" in (
        capsys.readouterr().err
    )


def test_list_cli_rejects_detail_with_multiple_names(monkeypatch, capsys):
    cli = _import_cli_or_skip()
    monkeypatch.setattr(
        "sys.argv", ["anvil", "list", "--tasks", "count_vpc", "noop", "--detail"]
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 2
    assert "--detail requires exactly one task or processor name" in (
        capsys.readouterr().err
    )


def test_list_cli_rejects_name_without_detail(monkeypatch, capsys):
    cli = _import_cli_or_skip()
    monkeypatch.setattr("sys.argv", ["anvil", "list", "--tasks", "count_vpc"])

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 2
    assert "task and processor names require --detail" in capsys.readouterr().err


def test_list_cli_rejects_provider_detail(monkeypatch, capsys):
    cli = _import_cli_or_skip()
    monkeypatch.setattr("sys.argv", ["anvil", "list", "--providers", "--detail"])

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 2
    assert "--detail cannot be used with --providers" in capsys.readouterr().err


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

    assert (
        cli._cmd_list(
            SimpleNamespace(tasks=[], processors=None, providers=False, detail=False)
        )
        == 0
    )

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

    assert (
        cli._cmd_list(
            SimpleNamespace(tasks=None, processors=[], providers=False, detail=False)
        )
        == 0
    )

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

    args = SimpleNamespace(tasks=None, processors=None, providers=True, detail=False)
    assert cli._cmd_list(args) == 0

    assert capsys.readouterr().out == (
        "Available providers:\nstock:\n  - aws\n\nplugin: my-plugin:\n  - custom\n"
    )


def test_cmd_list_providers_does_not_call_cloud_discovery(monkeypatch, capsys):
    cli = _import_cli_or_skip()

    monkeypatch.setattr(
        "anvil.providers.azure.provider.AzureSessionFactory.list_subscriptions",
        lambda self, **kwargs: (_ for _ in ()).throw(
            AssertionError("provider listing should not discover Azure subscriptions")
        ),
    )
    monkeypatch.setattr(
        "anvil.providers.gcp.provider.GcpSessionFactory.list_projects",
        lambda self, **kwargs: (_ for _ in ()).throw(
            AssertionError("provider listing should not discover GCP projects")
        ),
    )

    args = SimpleNamespace(tasks=None, processors=None, providers=True, detail=False)
    assert cli._cmd_list(args) == 0

    output = capsys.readouterr().out
    assert "Available providers:" in output
    assert "azure" in output
    assert "gcp" in output


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
    assert "--detail" in output
    assert "tasks list" not in output
    assert "processors list" not in output


def test_cmd_list_task_detail_prints_run_docstring(monkeypatch, capsys):
    cli = _import_cli_or_skip()

    def run():
        """Count VPCs in the current execution region."""

    monkeypatch.setattr(
        cli,
        "discover_tasks",
        lambda: SimpleNamespace(
            tasks=[
                cli.TaskDescriptor(name="count_vpc", load=lambda: run, source="stock")
            ],
            issues=[],
        ),
    )

    args = SimpleNamespace(
        tasks=["count_vpc"], processors=None, providers=False, detail=True
    )
    assert cli._cmd_list(args) == 0

    assert capsys.readouterr().out == (
        "count_vpc (stock)\n\nCount VPCs in the current execution region.\n"
    )


def test_cmd_list_processor_detail_prints_run_docstring(monkeypatch, capsys):
    cli = _import_cli_or_skip()

    def run():
        """Write an HTML report."""

    monkeypatch.setattr(
        cli,
        "discover_processors",
        lambda: SimpleNamespace(
            processors=[
                cli.ProcessorDescriptor(
                    name="html_report", load=lambda: run, source="stock"
                )
            ],
            issues=[],
        ),
    )

    args = SimpleNamespace(
        tasks=None, processors=["html_report"], providers=False, detail=True
    )
    assert cli._cmd_list(args) == 0

    assert capsys.readouterr().out == "html_report (stock)\n\nWrite an HTML report.\n"


def test_cmd_list_detail_prefers_callable_docstring_over_module(monkeypatch, capsys):
    cli = _import_cli_or_skip()

    def run():
        """Callable detail."""

    monkeypatch.setattr(
        cli,
        "discover_tasks",
        lambda: SimpleNamespace(
            tasks=[cli.TaskDescriptor(name="task", load=lambda: run, source="stock")],
            issues=[],
        ),
    )

    args = SimpleNamespace(
        tasks=["task"], processors=None, providers=False, detail=True
    )
    assert cli._cmd_list(args) == 0

    assert "Callable detail." in capsys.readouterr().out


def test_cmd_list_detail_uses_module_docstring_fallback(monkeypatch, capsys):
    cli = _import_cli_or_skip()

    def run():
        pass

    run.__doc__ = None

    monkeypatch.setattr(
        cli.inspect, "getmodule", lambda _: SimpleNamespace(__doc__="Module detail.")
    )
    monkeypatch.setattr(
        cli,
        "discover_tasks",
        lambda: SimpleNamespace(
            tasks=[cli.TaskDescriptor(name="task", load=lambda: run, source="stock")],
            issues=[],
        ),
    )

    args = SimpleNamespace(
        tasks=["task"], processors=None, providers=False, detail=True
    )
    assert cli._cmd_list(args) == 0

    assert capsys.readouterr().out == "task (stock)\n\nModule detail.\n"


def test_cmd_list_detail_reports_unknown_names(monkeypatch):
    cli = _import_cli_or_skip()
    monkeypatch.setattr(
        cli,
        "discover_tasks",
        lambda: SimpleNamespace(
            tasks=[
                cli.TaskDescriptor(name="count_vpc", load=lambda: None, source="stock")
            ],
            issues=[],
        ),
    )

    args = SimpleNamespace(
        tasks=["missing"], processors=None, providers=False, detail=True
    )

    with pytest.raises(ValueError, match="Unknown task: missing"):
        cli._cmd_list(args)


def test_cmd_list_detail_reports_ambiguous_names(monkeypatch):
    cli = _import_cli_or_skip()
    monkeypatch.setattr(
        cli,
        "discover_tasks",
        lambda: SimpleNamespace(
            tasks=[
                cli.TaskDescriptor(
                    name="shared", load=lambda: None, source="universal"
                ),
                cli.TaskDescriptor(name="shared", load=lambda: None, source="aws"),
            ],
            issues=[],
        ),
    )

    args = SimpleNamespace(
        tasks=["shared"], processors=None, providers=False, detail=True
    )

    with pytest.raises(ValueError, match="sources: universal, aws"):
        cli._cmd_list(args)


def test_cmd_list_detail_reports_missing_docstring(monkeypatch):
    cli = _import_cli_or_skip()

    def run():
        pass

    run.__doc__ = None
    monkeypatch.setattr(cli.inspect, "getmodule", lambda _: None)
    monkeypatch.setattr(
        cli,
        "discover_tasks",
        lambda: SimpleNamespace(
            tasks=[cli.TaskDescriptor(name="task", load=lambda: run, source="stock")],
            issues=[],
        ),
    )

    args = SimpleNamespace(
        tasks=["task"], processors=None, providers=False, detail=True
    )

    with pytest.raises(ValueError, match="No detail available for task"):
        cli._cmd_list(args)
