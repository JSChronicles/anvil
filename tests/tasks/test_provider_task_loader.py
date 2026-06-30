from __future__ import annotations

import importlib

import pytest

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
    task_loader._clear_task_caches()
    task_loader._resolve_tasks_cached.cache_clear()


def test_legacy_task_imports_are_not_supported():
    with pytest.raises(ModuleNotFoundError, match="anvil.tasks"):
        importlib.import_module("anvil.tasks.count_vpc")


def test_real_aws_descriptor_index_includes_moved_aws_tasks():
    task_loader = importlib.import_module("anvil.task_loader")
    _clear_task_loader_caches(task_loader)

    index = task_loader.provider_task_descriptor_index(provider_name="aws")

    assert "count_vpc" in index
    assert [descriptor.source for descriptor in index["count_vpc"]] == ["aws"]


def test_real_non_aws_descriptor_index_excludes_aws_only_tasks():
    task_loader = importlib.import_module("anvil.task_loader")
    _clear_task_loader_caches(task_loader)

    azure_index = task_loader.provider_task_descriptor_index(provider_name="azure")
    gcp_index = task_loader.provider_task_descriptor_index(provider_name="gcp")

    assert "count_vpc" not in azure_index
    assert "count_vpc" not in gcp_index


def test_real_azure_descriptor_index_includes_azure_tasks_only_for_azure():
    task_loader = importlib.import_module("anvil.task_loader")
    _clear_task_loader_caches(task_loader)

    aws_index = task_loader.provider_task_descriptor_index(provider_name="aws")
    azure_index = task_loader.provider_task_descriptor_index(provider_name="azure")
    gcp_index = task_loader.provider_task_descriptor_index(provider_name="gcp")

    assert "count_resource_groups" in azure_index
    assert [descriptor.source for descriptor in azure_index["count_resource_groups"]] == [
        "azure"
    ]
    assert "count_resource_groups" not in aws_index
    assert "count_resource_groups" not in gcp_index


def test_real_gcp_descriptor_index_includes_gcp_tasks_only_for_gcp():
    task_loader = importlib.import_module("anvil.task_loader")
    _clear_task_loader_caches(task_loader)

    aws_index = task_loader.provider_task_descriptor_index(provider_name="aws")
    azure_index = task_loader.provider_task_descriptor_index(provider_name="azure")
    gcp_index = task_loader.provider_task_descriptor_index(provider_name="gcp")

    assert "get_project_info" in gcp_index
    assert [descriptor.source for descriptor in gcp_index["get_project_info"]] == [
        "gcp"
    ]
    assert "get_project_info" not in aws_index
    assert "get_project_info" not in azure_index


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


def test_legacy_plugin_tasks_are_ignored_for_all_providers(monkeypatch):
    task_loader = importlib.import_module("anvil.task_loader")
    _clear_task_loader_caches(task_loader)

    monkeypatch.setattr(
        task_loader, "_provider_task_descriptor_index", lambda provider_name: {}
    )

    for provider_name in ("aws", "azure", "gcp"):
        with pytest.raises(TaskConfigError, match="provider package"):
            task_loader.resolve_tasks(
                task_specs=[{"name": "legacy_plugin_task"}],
                provider_name=provider_name,
            )


def test_duplicate_universal_and_provider_task_name_is_ambiguous(monkeypatch):
    task_loader = importlib.import_module("anvil.task_loader")
    _clear_task_loader_caches(task_loader)

    monkeypatch.setattr(
        task_loader,
        "_provider_task_descriptor_index",
        lambda provider_name: {
            "shared": (
                _descriptor("shared", "universal"),
                _descriptor("shared", provider_name),
            )
        },
    )

    index = task_loader.provider_task_descriptor_index(provider_name="aws")

    assert [descriptor.source for descriptor in index["shared"]] == [
        "universal",
        "aws",
    ]
    with pytest.raises(TaskConfigError, match="ambiguous.*universal.*aws"):
        task_loader.resolve_tasks(
            task_specs=[{"name": "shared"}], provider_name="aws"
        )


def test_provider_descriptor_index_adds_provider_package_tasks(monkeypatch):
    task_loader = importlib.import_module("anvil.task_loader")
    _clear_task_loader_caches(task_loader)

    def fake_package_descriptors(*, package_name, source):
        if source == "example":
            return [_descriptor("aws_only", source)]
        return []

    monkeypatch.setattr(
        task_loader, "_iter_package_task_descriptors", fake_package_descriptors
    )
    _clear_task_loader_caches(task_loader)

    execution = task_loader.resolve_tasks(
        task_specs=[{"name": "aws_only"}], provider_name="example"
    )

    assert execution.ordered[0].name == "aws_only"
    assert execution.ordered[0].run() == "example:aws_only"


def test_resolve_tasks_accepts_non_aws_provider_name(monkeypatch):
    task_loader = importlib.import_module("anvil.task_loader")
    _clear_task_loader_caches(task_loader)

    def fake_package_descriptors(*, package_name, source):
        if source == "universal":
            return [_descriptor("shared_task", source)]
        return []

    monkeypatch.setattr(
        task_loader, "_iter_package_task_descriptors", fake_package_descriptors
    )
    _clear_task_loader_caches(task_loader)

    execution = task_loader.resolve_tasks(
        task_specs=[{"name": "shared_task"}], provider_name="future"
    )

    assert execution.ordered[0].run() == "universal:shared_task"


def test_provider_descriptor_index_builds_once_for_multiple_configured_tasks(
    monkeypatch,
):
    task_loader = importlib.import_module("anvil.task_loader")
    _clear_task_loader_caches(task_loader)
    calls = {"packages": 0}

    def fake_package_descriptors(*, package_name, source):
        calls["packages"] += 1
        if source == "build_once":
            return [
                _descriptor("alpha", source),
                _descriptor("beta", source),
                _descriptor("gamma", source),
            ]
        return []

    monkeypatch.setattr(
        task_loader, "_iter_package_task_descriptors", fake_package_descriptors
    )
    _clear_task_loader_caches(task_loader)

    execution = task_loader.resolve_tasks(
        task_specs=[
            {"name": "alpha"},
            {"name": "beta", "depends_on": ["alpha"]},
            {"name": "gamma", "depends_on": ["beta"]},
        ],
        provider_name="build_once",
    )

    assert [task.name for task in execution.ordered] == ["alpha", "beta", "gamma"]
    assert calls == {"packages": 2}


def test_discover_tasks_ignores_legacy_discovery_issues(monkeypatch):
    task_loader = importlib.import_module("anvil.task_loader")

    def fake_discovery(provider_name):
        return {"noop": (_descriptor("noop", "universal"),)}, ()

    monkeypatch.setattr(task_loader, "_provider_task_discovery", fake_discovery)

    discovery = task_loader.discover_tasks()

    assert [task.name for task in discovery.tasks] == ["noop"]
    assert discovery.issues == []
