from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from anvil.providers.base import ProviderMetadata


def _import_cli_or_skip():
    try:
        from anvil import cli
    except PermissionError as error:
        pytest.skip(f"jsonschema package resources unavailable in test env: {error}")

    return cli


def _run_main_with_args(monkeypatch, argv: list[str]):
    cli = _import_cli_or_skip()
    seen = {}

    def fake_cmd_validate(args):
        seen["args"] = args
        return 0

    monkeypatch.setattr(cli, "_cmd_validate", fake_cmd_validate)
    monkeypatch.setattr("sys.argv", argv)

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    return seen["args"]


def test_validate_cli_parses_all_task_validation(monkeypatch):
    args = _run_main_with_args(monkeypatch, ["anvil", "validate", "--tasks"])

    assert args.tasks == []
    assert args.processors is None
    assert args.auth is False


def test_validate_cli_parses_selected_task_validation(monkeypatch):
    args = _run_main_with_args(
        monkeypatch, ["anvil", "validate", "--tasks", "count_vpc"]
    )

    assert args.tasks == ["count_vpc"]


def test_validate_cli_parses_all_processor_validation(monkeypatch):
    args = _run_main_with_args(monkeypatch, ["anvil", "validate", "--processors"])

    assert args.processors == []
    assert args.tasks is None


def test_validate_cli_parses_selected_processor_validation(monkeypatch):
    args = _run_main_with_args(
        monkeypatch, ["anvil", "validate", "--processors", "summary_report"]
    )

    assert args.processors == ["summary_report"]


def test_validate_cli_parses_all_provider_validation(monkeypatch):
    args = _run_main_with_args(monkeypatch, ["anvil", "validate", "--providers"])

    assert args.providers == []
    assert args.tasks is None
    assert args.processors is None


def test_validate_cli_parses_selected_provider_validation(monkeypatch):
    args = _run_main_with_args(monkeypatch, ["anvil", "validate", "--providers", "aws"])

    assert args.providers == ["aws"]


def test_validate_cli_parses_auth_validation(monkeypatch):
    args = _run_main_with_args(
        monkeypatch, ["anvil", "validate", "--auth", "--config-file", "yaml/orgs.yaml"]
    )

    assert args.auth is True
    assert args.config_file == [Path("yaml/orgs.yaml")]


def test_validate_cli_parses_quiet(monkeypatch):
    args = _run_main_with_args(monkeypatch, ["anvil", "validate", "--tasks", "--quiet"])

    assert args.tasks == []
    assert args.quiet is True


def test_validate_cli_parses_multiple_categories(monkeypatch):
    args = _run_main_with_args(
        monkeypatch, ["anvil", "validate", "--tasks", "--processors", "--providers"]
    )

    assert args.tasks == []
    assert args.processors == []
    assert args.providers == []


def test_validate_cli_parses_tasks_and_auth(monkeypatch):
    args = _run_main_with_args(
        monkeypatch,
        ["anvil", "validate", "--tasks", "--auth", "--config-file", "yaml/orgs.yaml"],
    )

    assert args.tasks == []
    assert args.auth is True
    assert args.config_file == [Path("yaml/orgs.yaml")]


def test_validate_auth_requires_config_file(monkeypatch, capsys):
    cli = _import_cli_or_skip()
    monkeypatch.setattr("sys.argv", ["anvil", "validate", "--auth"])

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 2
    assert "--config-file is required with --auth" in capsys.readouterr().err


def test_validate_combined_auth_requires_config_file(monkeypatch, capsys):
    cli = _import_cli_or_skip()
    monkeypatch.setattr(
        "sys.argv", ["anvil", "validate", "--tasks", "--processors", "--auth"]
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 2
    assert "--config-file is required with --auth" in capsys.readouterr().err


def test_validate_selected_tasks_validates_all_when_no_names(monkeypatch):
    cli = _import_cli_or_skip()
    seen = {}

    def fake_load_task_callable(task_name):
        def run(*, account_id, account_alias, session, dry_run, metadata, actions):
            return None

        seen.setdefault("loaded", []).append(task_name)
        return run

    monkeypatch.setattr(
        cli,
        "discover_tasks",
        lambda: SimpleNamespace(
            tasks=[
                cli.TaskDescriptor(
                    name="count_vpc",
                    load=lambda: fake_load_task_callable("count_vpc"),
                    source="stock",
                ),
                cli.TaskDescriptor(
                    name="noop",
                    load=lambda: fake_load_task_callable("noop"),
                    source="stock",
                ),
            ],
            issues=[],
        ),
    )

    def fake_task_validation_errors(tasks):
        seen["validated"] = [task.name for task in tasks]
        return []

    monkeypatch.setattr(cli, "task_validation_errors", fake_task_validation_errors)

    cli._validate_selected_tasks([])

    assert seen["loaded"] == ["count_vpc", "noop"]
    assert seen["validated"] == ["count_vpc", "noop"]


def test_validate_selected_tasks_validates_selected_names(monkeypatch):
    cli = _import_cli_or_skip()
    seen = {}

    def fake_load_task_callable(task_name):
        def run(*, account_id, account_alias, session, dry_run, metadata, actions):
            return None

        seen.setdefault("loaded", []).append(task_name)
        return run

    monkeypatch.setattr(
        cli,
        "discover_tasks",
        lambda: SimpleNamespace(
            tasks=[
                cli.TaskDescriptor(
                    name="count_vpc",
                    load=lambda: fake_load_task_callable("count_vpc"),
                    source="stock",
                ),
                cli.TaskDescriptor(
                    name="noop",
                    load=lambda: fake_load_task_callable("noop"),
                    source="stock",
                ),
            ],
            issues=[],
        ),
    )

    def fake_task_validation_errors(tasks):
        seen["validated"] = [task.name for task in tasks]
        return []

    monkeypatch.setattr(cli, "task_validation_errors", fake_task_validation_errors)

    cli._validate_selected_tasks(["noop"])

    assert seen["loaded"] == ["noop"]
    assert seen["validated"] == ["noop"]


def test_validate_selected_tasks_reports_unknown_names(monkeypatch):
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

    with pytest.raises(ValueError, match="Unknown task"):
        cli._validate_selected_tasks(["missing"])


def test_validate_all_tasks_reports_plugin_discovery_issues(monkeypatch):
    cli = _import_cli_or_skip()
    monkeypatch.setattr(
        cli,
        "discover_tasks",
        lambda: SimpleNamespace(
            tasks=[],
            issues=[
                cli.DiscoveryIssue(
                    name="broken-plugin",
                    source="plugin: broken-package",
                    error="package import failed (missing dependency)",
                )
            ],
        ),
    )

    with pytest.raises(ValueError) as exc_info:
        cli._validate_selected_tasks([])

    assert "broken-plugin (plugin: broken-package)" in str(exc_info.value)
    assert "package import failed (missing dependency)" in str(exc_info.value)


def test_validate_selected_unknown_task_reports_plugin_discovery_issues(monkeypatch):
    cli = _import_cli_or_skip()
    monkeypatch.setattr(
        cli,
        "discover_tasks",
        lambda: SimpleNamespace(
            tasks=[],
            issues=[
                cli.DiscoveryIssue(
                    name="broken-plugin",
                    source="plugin: broken-package",
                    error="package import failed (missing dependency)",
                )
            ],
        ),
    )

    with pytest.raises(ValueError) as exc_info:
        cli._validate_selected_tasks(["plugin_task"])

    assert "Unknown task(s): plugin_task" in str(exc_info.value)
    assert "broken-plugin (plugin: broken-package)" in str(exc_info.value)


def test_validate_selected_processors_validates_all_when_no_names(monkeypatch):
    cli = _import_cli_or_skip()
    seen = {}

    processors = [
        cli.ProcessorDescriptor(
            name="summary_report", load=lambda: None, source="stock"
        ),
        cli.ProcessorDescriptor(name="html_export", load=lambda: None, source="stock"),
    ]
    monkeypatch.setattr(
        cli,
        "discover_processors",
        lambda: SimpleNamespace(processors=processors, issues=[]),
    )

    def fake_processor_validation_errors(processors):
        seen["validated"] = [processor.name for processor in processors]
        return []

    monkeypatch.setattr(
        cli, "processor_validation_errors", fake_processor_validation_errors
    )

    cli._validate_selected_processors([])

    assert seen["validated"] == ["summary_report", "html_export"]


def test_validate_selected_processors_validates_selected_names(monkeypatch):
    cli = _import_cli_or_skip()
    seen = {}

    processors = [
        cli.ProcessorDescriptor(
            name="summary_report", load=lambda: None, source="stock"
        ),
        cli.ProcessorDescriptor(name="html_export", load=lambda: None, source="stock"),
    ]
    monkeypatch.setattr(
        cli,
        "discover_processors",
        lambda: SimpleNamespace(processors=processors, issues=[]),
    )

    def fake_processor_validation_errors(processors):
        seen["validated"] = [processor.name for processor in processors]
        return []

    monkeypatch.setattr(
        cli, "processor_validation_errors", fake_processor_validation_errors
    )

    cli._validate_selected_processors(["html_export"])

    assert seen["validated"] == ["html_export"]


def test_validate_selected_processors_preserves_duplicate_discoveries(monkeypatch):
    cli = _import_cli_or_skip()
    seen = {}

    processors = [
        cli.ProcessorDescriptor(
            name="summary_report", load=lambda: None, source="stock"
        ),
        cli.ProcessorDescriptor(
            name="summary_report", load=lambda: None, source="plugin"
        ),
        cli.ProcessorDescriptor(name="html_export", load=lambda: None, source="stock"),
    ]
    monkeypatch.setattr(
        cli,
        "discover_processors",
        lambda: SimpleNamespace(processors=processors, issues=[]),
    )

    def fake_processor_validation_errors(processors):
        seen["validated"] = [
            (processor.name, processor.source) for processor in processors
        ]
        return []

    monkeypatch.setattr(
        cli, "processor_validation_errors", fake_processor_validation_errors
    )

    cli._validate_selected_processors(["summary_report"])

    assert seen["validated"] == [
        ("summary_report", "stock"),
        ("summary_report", "plugin"),
    ]


def test_validate_selected_processors_reports_unknown_names(monkeypatch):
    cli = _import_cli_or_skip()
    monkeypatch.setattr(
        cli,
        "discover_processors",
        lambda: SimpleNamespace(
            processors=[
                cli.ProcessorDescriptor(
                    name="summary_report", load=lambda: None, source="stock"
                )
            ],
            issues=[],
        ),
    )

    with pytest.raises(ValueError, match="Unknown processor"):
        cli._validate_selected_processors(["missing"])


def test_validate_all_processors_reports_plugin_discovery_issues(monkeypatch):
    cli = _import_cli_or_skip()
    monkeypatch.setattr(
        cli,
        "discover_processors",
        lambda: SimpleNamespace(
            processors=[],
            issues=[
                cli.DiscoveryIssue(
                    name="broken-plugin",
                    source="plugin: broken-package",
                    error="package import failed (missing dependency)",
                )
            ],
        ),
    )

    with pytest.raises(ValueError) as exc_info:
        cli._validate_selected_processors([])

    assert "broken-plugin (plugin: broken-package)" in str(exc_info.value)
    assert "package import failed (missing dependency)" in str(exc_info.value)


def test_validate_selected_known_processor_ignores_unrelated_discovery_issues(
    monkeypatch,
):
    cli = _import_cli_or_skip()

    def run(*, context, output, metadata):
        return None

    monkeypatch.setattr(
        cli,
        "discover_processors",
        lambda: SimpleNamespace(
            processors=[
                cli.ProcessorDescriptor(
                    name="summary_report", load=lambda: run, source="stock"
                )
            ],
            issues=[
                cli.DiscoveryIssue(
                    name="broken-plugin",
                    source="plugin: broken-package",
                    error="package import failed (missing dependency)",
                )
            ],
        ),
    )

    cli._validate_selected_processors(["summary_report"])


def test_validate_selected_providers_validates_all_when_no_names(monkeypatch):
    cli = _import_cli_or_skip()
    seen = {}

    class Provider:
        metadata = ProviderMetadata(name="aws", display_name="AWS")

        def validate_target(self, target):
            return None

        def default_regions(self, target):
            return []

        def auth_cache_key(self, target):
            return None

        def auth_check(self, target):
            return None

        def discover_regions(self, target):
            return []

        def resolve_execution_targets(self, *, target, regions, include, exclude):
            return None

        def prepare_execution_runtime(self, *, target, execution_target, context):
            return None

    def load_provider():
        seen.setdefault("loaded", []).append("aws")
        return Provider()

    monkeypatch.setattr(
        cli,
        "discover_providers",
        lambda: SimpleNamespace(
            providers=[
                cli.ProviderDescriptor(
                    name="aws", display_name="AWS", load=load_provider, source="stock"
                )
            ],
            issues=[],
        ),
    )

    cli._validate_selected_providers([])

    assert seen["loaded"] == ["aws"]


def test_validate_selected_providers_does_not_call_azure_subscription_discovery(
    monkeypatch,
):
    cli = _import_cli_or_skip()

    monkeypatch.setattr(
        "anvil.providers.azure.provider.AzureSessionFactory.list_subscriptions",
        lambda self, **kwargs: (_ for _ in ()).throw(
            AssertionError("provider validation should not discover Azure subscriptions")
        ),
    )

    cli._validate_selected_providers(["azure"])


def test_validate_selected_providers_reports_unknown_names(monkeypatch):
    cli = _import_cli_or_skip()
    monkeypatch.setattr(
        cli,
        "discover_providers",
        lambda: SimpleNamespace(
            providers=[
                cli.ProviderDescriptor(
                    name="aws", display_name="AWS", load=lambda: None, source="stock"
                )
            ],
            issues=[],
        ),
    )

    with pytest.raises(ValueError, match="Unknown provider"):
        cli._validate_selected_providers(["missing"])


def test_validate_selected_providers_reports_contract_member(monkeypatch):
    cli = _import_cli_or_skip()

    class BrokenProvider:
        metadata = ProviderMetadata(name="broken", display_name="Broken")

        def validate_target(self, target):
            return None

    monkeypatch.setattr(
        cli,
        "discover_providers",
        lambda: SimpleNamespace(
            providers=[
                cli.ProviderDescriptor(
                    name="broken",
                    display_name="Broken",
                    load=lambda: BrokenProvider(),
                    source="plugin: broken-provider",
                )
            ],
            issues=[],
        ),
    )

    with pytest.raises(ValueError) as exc_info:
        cli._validate_selected_providers(["broken"])

    error = str(exc_info.value)
    assert "broken (plugin: broken-provider)" in error
    assert "default_regions" in error


def test_validate_aggregates_failures_and_successes(monkeypatch, capsys):
    cli = _import_cli_or_skip()
    args = SimpleNamespace(
        tasks=[],
        processors=[],
        providers=[],
        auth=True,
        config_file=[Path("yaml/orgs.yaml")],
        include=None,
        exclude=None,
        quiet=False,
    )

    calls = []
    monkeypatch.setattr(
        cli, "_validate_selected_tasks", lambda _: calls.append("tasks")
    )

    def fail_processors(_):
        calls.append("processors")
        raise ValueError("processor failed")

    monkeypatch.setattr(cli, "_validate_selected_processors", fail_processors)
    monkeypatch.setattr(
        cli, "_validate_selected_providers", lambda _: calls.append("providers")
    )
    monkeypatch.setattr(cli, "_cmd_validate_auth", lambda _: calls.append("auth") or 0)

    assert cli._cmd_validate(args) == 1
    assert calls == ["tasks", "processors", "providers", "auth"]

    output = capsys.readouterr().out
    assert "Validation Summary" not in output
    assert "[OK]     Tasks" in output
    assert "[ERROR]  Processors" in output
    assert "[OK]     Providers" in output
    assert "[OK]     Authentication" in output
    assert "Result:" not in output


def test_validate_treats_nonzero_auth_exit_code_as_failure(monkeypatch, capsys):
    cli = _import_cli_or_skip()
    args = SimpleNamespace(
        tasks=None,
        processors=None,
        providers=None,
        auth=True,
        config_file=[Path("yaml/orgs.yaml")],
        include=None,
        exclude=None,
        quiet=False,
    )

    monkeypatch.setattr(cli, "_cmd_validate_auth", lambda _: 1)

    assert cli._cmd_validate(args) == 1
    output = capsys.readouterr().out
    assert "[ERROR]  Authentication" in output
    assert "Result:" not in output


def test_validate_combined_categories_report_auth_failure(monkeypatch, capsys):
    cli = _import_cli_or_skip()
    args = SimpleNamespace(
        tasks=[],
        processors=[],
        providers=None,
        auth=True,
        config_file=[Path("yaml/noop.yaml")],
        include=None,
        exclude=None,
        quiet=False,
    )

    monkeypatch.setattr(cli, "_validate_selected_tasks", lambda _: None)
    monkeypatch.setattr(cli, "_validate_selected_processors", lambda _: None)
    monkeypatch.setattr(cli, "_cmd_validate_auth", lambda _: 1)

    assert cli._cmd_validate(args) == 1
    output = capsys.readouterr().out
    assert "Validation Summary" not in output
    assert "[OK]     Tasks" in output
    assert "[OK]     Processors" in output
    assert "[ERROR]  Authentication" in output
    assert "Result:" not in output


def test_validate_tasks_and_processors_report_success(monkeypatch, capsys):
    cli = _import_cli_or_skip()
    args = SimpleNamespace(
        tasks=[],
        processors=[],
        providers=[],
        auth=False,
        config_file=None,
        include=None,
        exclude=None,
        quiet=False,
    )

    monkeypatch.setattr(cli, "_validate_selected_tasks", lambda _: None)
    monkeypatch.setattr(cli, "_validate_selected_processors", lambda _: None)
    monkeypatch.setattr(cli, "_validate_selected_providers", lambda _: None)

    assert cli._cmd_validate(args) == 0
    output = capsys.readouterr().out
    assert "[OK]     Tasks" in output
    assert "[OK]     Processors" in output
    assert "[OK]     Providers" in output
    assert "Result:" not in output


def test_validate_task_failure_reports_failed_result(monkeypatch, capsys):
    cli = _import_cli_or_skip()
    args = SimpleNamespace(
        tasks=[],
        processors=None,
        providers=None,
        auth=False,
        config_file=None,
        include=None,
        exclude=None,
        quiet=False,
    )

    def fail_tasks(_):
        raise ValueError(
            "\n  - task 'count_vpc' is missing required run() parameters: ['dry_run']"
        )

    monkeypatch.setattr(cli, "_validate_selected_tasks", fail_tasks)

    assert cli._cmd_validate(args) == 1
    output = capsys.readouterr().out
    assert "[ERROR]  Tasks" in output
    assert "task 'count_vpc' is missing required run() parameters" in output
    assert "Result:" not in output


def test_validate_auth_uses_quiet_auth_check(monkeypatch):
    cli = _import_cli_or_skip()
    seen = {}
    args = SimpleNamespace(
        config_file=[Path("yaml/orgs.yaml")], include=None, exclude=None
    )

    def fake_cmd_auth_check(auth_args):
        seen["quiet"] = auth_args.quiet
        seen["config_file"] = auth_args.config_file
        return 0

    monkeypatch.setattr(cli, "_cmd_auth_check", fake_cmd_auth_check)

    assert cli._cmd_validate_auth(args) == 0
    assert seen == {"quiet": True, "config_file": [Path("yaml/orgs.yaml")]}


def test_validate_quiet_suppresses_success_output(monkeypatch, capsys):
    cli = _import_cli_or_skip()
    args = SimpleNamespace(
        tasks=[],
        processors=[],
        providers=None,
        auth=False,
        config_file=None,
        include=None,
        exclude=None,
        quiet=True,
    )

    monkeypatch.setattr(cli, "_validate_selected_tasks", lambda _: None)
    monkeypatch.setattr(cli, "_validate_selected_processors", lambda _: None)

    assert cli._cmd_validate(args) == 0
    assert capsys.readouterr().out == ""


def test_validate_quiet_suppresses_failure_output(monkeypatch, capsys):
    cli = _import_cli_or_skip()
    args = SimpleNamespace(
        tasks=[],
        processors=None,
        auth=False,
        config_file=None,
        include=None,
        exclude=None,
        quiet=True,
    )

    def fail_tasks(_):
        raise ValueError("task failed")

    monkeypatch.setattr(cli, "_validate_selected_tasks", fail_tasks)

    assert cli._cmd_validate(args) == 1
    assert capsys.readouterr().out == ""


def test_validate_returns_success_when_all_categories_pass(monkeypatch, capsys):
    cli = _import_cli_or_skip()
    args = SimpleNamespace(
        tasks=["count_vpc"],
        processors=["summary_report"],
        providers=["aws"],
        auth=False,
        config_file=None,
        include=None,
        exclude=None,
        quiet=False,
    )

    monkeypatch.setattr(cli, "_validate_selected_tasks", lambda _: None)
    monkeypatch.setattr(cli, "_validate_selected_processors", lambda _: None)
    monkeypatch.setattr(cli, "_validate_selected_providers", lambda _: None)

    assert cli._cmd_validate(args) == 0
    assert "Result:" not in capsys.readouterr().out
