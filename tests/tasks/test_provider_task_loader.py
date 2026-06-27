from __future__ import annotations

import importlib

import pytest

from anvil._loader_utils import DiscoveryIssue
from anvil.task_loader import TaskConfigError, TaskDescriptor


def _run_for(name: str):
    def run(**kwargs):
        return name

    return run


def _descriptor(name: str, source: str) -> TaskDescriptor:
    return TaskDescriptor(
        name=name, load=lambda: _run_for(f"{source}:{name}"), source=source
    )


def _clear_task_loader_caches(task_loader) -> None:
    task_loader._load_task_callable.cache_clear()
    task_loader._resolve_tasks_cached.cache_clear()


def test_legacy_task_imports_reexport_moved_aws_implementation():
    legacy_module = importlib.import_module("anvil.tasks.count_vpc")
    provider_module = importlib.import_module("anvil.providers.aws.tasks.count_vpc")

    assert legacy_module.run is provider_module.run
    assert legacy_module.run.__module__ == "anvil.providers.aws.tasks.count_vpc"


def test_legacy_task_imports_reexport_moved_universal_implementation():
    legacy_module = importlib.import_module("anvil.tasks.noop")
    provider_module = importlib.import_module("anvil.providers.tasks.noop")

    assert legacy_module.run is provider_module.run
    assert legacy_module.run.__module__ == "anvil.providers.tasks.noop"


def test_real_aws_descriptor_index_includes_moved_aws_tasks():
    task_loader = importlib.import_module("anvil.task_loader")
    _clear_task_loader_caches(task_loader)

    index = task_loader.provider_task_descriptor_index(provider_name="aws")

    assert "count_vpc" in index
    assert [descriptor.source for descriptor in index["count_vpc"]] == ["stock", "aws"]


def test_real_non_aws_descriptor_index_excludes_aws_only_tasks():
    task_loader = importlib.import_module("anvil.task_loader")
    _clear_task_loader_caches(task_loader)

    azure_index = task_loader.provider_task_descriptor_index(provider_name="azure")
    gcp_index = task_loader.provider_task_descriptor_index(provider_name="gcp")

    assert "count_vpc" not in azure_index
    assert "count_vpc" not in gcp_index


def test_universal_noop_resolves_for_all_providers():
    task_loader = importlib.import_module("anvil.task_loader")
    _clear_task_loader_caches(task_loader)

    for provider_name in ("aws", "azure", "gcp"):
        execution = task_loader.resolve_tasks(
            task_specs=[{"name": "noop"}], provider_name=provider_name
        )
        assert execution.ordered[0].name == "noop"
        assert execution.ordered[0].run.__module__ in {
            "anvil.providers.tasks.noop",
        }


def test_aws_only_tasks_do_not_resolve_for_azure_or_gcp():
    task_loader = importlib.import_module("anvil.task_loader")
    _clear_task_loader_caches(task_loader)

    for provider_name in ("azure", "gcp"):
        with pytest.raises(TaskConfigError, match="count_vpc"):
            task_loader.resolve_tasks(
                task_specs=[{"name": "count_vpc"}], provider_name=provider_name
            )


def test_legacy_plugin_tasks_are_aws_only(monkeypatch):
    task_loader = importlib.import_module("anvil.task_loader")
    _clear_task_loader_caches(task_loader)

    monkeypatch.setattr(
        task_loader,
        "_legacy_task_descriptors",
        lambda: ([_descriptor("legacy_plugin_task", "plugin: legacy")], []),
    )
    monkeypatch.setattr(
        task_loader, "_iter_package_task_descriptors", lambda *, package_name, source: []
    )

    aws_execution = task_loader.resolve_tasks(
        task_specs=[{"name": "legacy_plugin_task"}], provider_name="aws"
    )
    assert aws_execution.ordered[0].run() == "plugin: legacy:legacy_plugin_task"

    for provider_name in ("azure", "gcp"):
        with pytest.raises(TaskConfigError, match="AWS-compatible only"):
            task_loader.resolve_tasks(
                task_specs=[{"name": "legacy_plugin_task"}],
                provider_name=provider_name,
            )


def test_provider_descriptor_index_preserves_legacy_aws_tasks_first(monkeypatch):
    task_loader = importlib.import_module("anvil.task_loader")
    _clear_task_loader_caches(task_loader)

    monkeypatch.setattr(
        task_loader,
        "_legacy_task_descriptors",
        lambda: ([_descriptor("count_vpc", "stock")], []),
    )
    monkeypatch.setattr(
        task_loader,
        "_iter_package_task_descriptors",
        lambda *, package_name, source: [_descriptor("count_vpc", source)],
    )

    index = task_loader.provider_task_descriptor_index(provider_name="aws")
    execution = task_loader.resolve_tasks(task_specs=[{"name": "count_vpc"}])

    assert [descriptor.source for descriptor in index["count_vpc"]] == [
        "stock",
        "universal",
        "aws",
    ]
    assert execution.ordered[0].run() == "stock:count_vpc"


def test_provider_descriptor_index_adds_provider_package_tasks(monkeypatch):
    task_loader = importlib.import_module("anvil.task_loader")
    _clear_task_loader_caches(task_loader)

    monkeypatch.setattr(task_loader, "_legacy_task_descriptors", lambda: ([], []))

    def fake_package_descriptors(*, package_name, source):
        if source == "aws":
            return [_descriptor("aws_only", source)]
        return []

    monkeypatch.setattr(
        task_loader, "_iter_package_task_descriptors", fake_package_descriptors
    )

    execution = task_loader.resolve_tasks(task_specs=[{"name": "aws_only"}])

    assert execution.ordered[0].name == "aws_only"
    assert execution.ordered[0].run() == "aws:aws_only"


def test_resolve_tasks_accepts_non_aws_provider_name(monkeypatch):
    task_loader = importlib.import_module("anvil.task_loader")
    _clear_task_loader_caches(task_loader)

    monkeypatch.setattr(task_loader, "_legacy_task_descriptors", lambda: ([], []))

    def fake_package_descriptors(*, package_name, source):
        if source == "universal":
            return [_descriptor("shared_task", source)]
        return []

    monkeypatch.setattr(
        task_loader, "_iter_package_task_descriptors", fake_package_descriptors
    )

    execution = task_loader.resolve_tasks(
        task_specs=[{"name": "shared_task"}], provider_name="future"
    )

    assert execution.ordered[0].run() == "universal:shared_task"


def test_provider_descriptor_index_builds_once_for_multiple_configured_tasks(
    monkeypatch,
):
    task_loader = importlib.import_module("anvil.task_loader")
    _clear_task_loader_caches(task_loader)
    calls = {"legacy": 0, "packages": 0}

    def fake_legacy_descriptors():
        calls["legacy"] += 1
        return (
            [
                _descriptor("alpha", "stock"),
                _descriptor("beta", "stock"),
                _descriptor("gamma", "stock"),
            ],
            [],
        )

    def fake_package_descriptors(*, package_name, source):
        calls["packages"] += 1
        return []

    monkeypatch.setattr(
        task_loader, "_legacy_task_descriptors", fake_legacy_descriptors
    )
    monkeypatch.setattr(
        task_loader, "_iter_package_task_descriptors", fake_package_descriptors
    )

    execution = task_loader.resolve_tasks(
        task_specs=[
            {"name": "alpha"},
            {"name": "beta", "depends_on": ["alpha"]},
            {"name": "gamma", "depends_on": ["beta"]},
        ]
    )

    assert [task.name for task in execution.ordered] == ["alpha", "beta", "gamma"]
    assert calls == {"legacy": 1, "packages": 2}


def test_discover_tasks_still_returns_legacy_discovery_issues(monkeypatch):
    task_loader = importlib.import_module("anvil.task_loader")

    issue = DiscoveryIssue(
        name="broken-plugin",
        source="plugin: broken-plugin",
        error="package import failed",
    )
    monkeypatch.setattr(task_loader, "_legacy_task_descriptors", lambda: ([], [issue]))

    discovery = task_loader.discover_tasks()

    assert discovery.tasks == []
    assert discovery.issues == [issue]
