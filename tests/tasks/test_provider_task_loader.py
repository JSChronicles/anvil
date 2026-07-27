from __future__ import annotations

import importlib
import inspect

import pytest

from anvil._components import ComponentCatalog, ComponentOrigin, ComponentSource
from anvil.task_loader import TaskConfigError, TaskDescriptor

SUPPORTED_TASK_SCOPES = frozenset({"region", "target"})


def _run_for(name: str):
    def run(**kwargs):
        return name

    return run


def _descriptor(name: str, source: str) -> TaskDescriptor:
    return TaskDescriptor(
        name=name,
        load=lambda: _run_for(f"{source}:{name}"),
        source=ComponentSource(
            origin=ComponentOrigin.STOCK,
            package="tests.tasks",
            label=source,
            provider=None if source == "universal" else source,
        ),
    )


def _clear_task_loader_caches(task_loader) -> None:
    task_loader._clear_task_caches()
    task_loader._resolve_tasks_cached.cache_clear()


def test_real_aws_descriptor_index_includes_moved_aws_tasks():
    task_loader = importlib.import_module("anvil.task_loader")
    _clear_task_loader_caches(task_loader)

    index = task_loader.provider_task_descriptor_index(provider_name="aws")

    assert "count_vpc" in index
    assert [str(descriptor.source) for descriptor in index["count_vpc"]] == ["aws"]


def test_real_aws_tasks_use_provider_neutral_signature():
    task_loader = importlib.import_module("anvil.task_loader")
    _clear_task_loader_caches(task_loader)

    index = task_loader.provider_task_descriptor_index(provider_name="aws")
    required_parameters = {
        "provider",
        "execution_target_id",
        "execution_target_name",
        "execution_target_type",
        "region",
        "session",
        "dry_run",
        "metadata",
        "actions",
    }

    for descriptors in index.values():
        for descriptor in descriptors:
            if str(descriptor.source) != "aws":
                continue

            parameters = set(inspect.signature(descriptor.load()).parameters)
            assert required_parameters <= parameters
            assert "account_id" not in parameters
            assert "account_alias" not in parameters


def test_real_azure_descriptor_index_includes_azure_tasks_only_for_azure():
    task_loader = importlib.import_module("anvil.task_loader")
    _clear_task_loader_caches(task_loader)

    aws_index = task_loader.provider_task_descriptor_index(provider_name="aws")
    azure_index = task_loader.provider_task_descriptor_index(provider_name="azure")
    gcp_index = task_loader.provider_task_descriptor_index(provider_name="gcp")
    github_index = task_loader.provider_task_descriptor_index(provider_name="github")

    assert "count_resource_groups" in azure_index
    assert [
        str(descriptor.source) for descriptor in azure_index["count_resource_groups"]
    ] == ["azure"]
    assert "count_resource_groups" not in aws_index
    assert "count_resource_groups" not in gcp_index
    assert "count_resource_groups" not in github_index


def test_real_gcp_descriptor_index_includes_gcp_tasks_only_for_gcp():
    task_loader = importlib.import_module("anvil.task_loader")
    _clear_task_loader_caches(task_loader)

    aws_index = task_loader.provider_task_descriptor_index(provider_name="aws")
    azure_index = task_loader.provider_task_descriptor_index(provider_name="azure")
    gcp_index = task_loader.provider_task_descriptor_index(provider_name="gcp")
    github_index = task_loader.provider_task_descriptor_index(provider_name="github")

    assert "get_project_info" in gcp_index
    assert [str(descriptor.source) for descriptor in gcp_index["get_project_info"]] == [
        "gcp"
    ]
    assert "get_project_info" not in aws_index
    assert "get_project_info" not in azure_index
    assert "get_project_info" not in github_index


def test_universal_noop_resolves_for_all_providers():
    task_loader = importlib.import_module("anvil.task_loader")
    _clear_task_loader_caches(task_loader)

    for provider_name in ("aws", "azure", "gcp", "github"):
        execution = task_loader.resolve_tasks(
            task_specs=[{"name": "noop"}],
            provider_name=provider_name,
            supported_task_scopes=SUPPORTED_TASK_SCOPES,
        )
        assert execution.ordered[0].name == "noop"
        assert execution.ordered[0].run.__module__ in {"anvil.providers.tasks.noop"}


def test_aws_only_tasks_do_not_resolve_for_azure_or_gcp():
    task_loader = importlib.import_module("anvil.task_loader")
    _clear_task_loader_caches(task_loader)

    for provider_name in ("azure", "gcp", "github"):
        with pytest.raises(TaskConfigError, match="count_vpc"):
            task_loader.resolve_tasks(
                task_specs=[{"name": "count_vpc"}],
                provider_name=provider_name,
                supported_task_scopes=SUPPORTED_TASK_SCOPES,
            )


def test_duplicate_universal_and_provider_task_name_is_ambiguous(monkeypatch):
    task_loader = importlib.import_module("anvil.task_loader")
    _clear_task_loader_caches(task_loader)

    catalog = ComponentCatalog.build(
        [_descriptor("shared", "universal"), _descriptor("shared", "aws")]
    )
    monkeypatch.setattr(
        task_loader, "_provider_task_catalog", lambda provider_name: catalog
    )

    index = task_loader.provider_task_descriptor_index(provider_name="aws")

    assert [str(descriptor.source) for descriptor in index["shared"]] == [
        "aws",
        "universal",
    ]
    with pytest.raises(TaskConfigError, match="ambiguous.*aws.*universal"):
        task_loader.resolve_tasks(
            task_specs=[{"name": "shared"}],
            provider_name="aws",
            supported_task_scopes=SUPPORTED_TASK_SCOPES,
        )


def test_provider_descriptor_index_adds_provider_package_tasks(monkeypatch):
    task_loader = importlib.import_module("anvil.task_loader")
    _clear_task_loader_caches(task_loader)

    def fake_package_descriptors(*, package_name, source_label, provider_name):
        if source_label == "example":
            return [_descriptor("aws_only", source_label)]
        return []

    monkeypatch.setattr(
        task_loader, "_iter_package_task_descriptors", fake_package_descriptors
    )
    _clear_task_loader_caches(task_loader)

    execution = task_loader.resolve_tasks(
        task_specs=[{"name": "aws_only"}],
        provider_name="example",
        supported_task_scopes=SUPPORTED_TASK_SCOPES,
    )

    assert execution.ordered[0].name == "aws_only"
    assert execution.ordered[0].run() == "example:aws_only"


def test_resolve_tasks_accepts_non_aws_provider_name(monkeypatch):
    task_loader = importlib.import_module("anvil.task_loader")
    _clear_task_loader_caches(task_loader)

    def fake_package_descriptors(*, package_name, source_label, provider_name):
        if source_label == "universal":
            return [_descriptor("shared_task", source_label)]
        return []

    monkeypatch.setattr(
        task_loader, "_iter_package_task_descriptors", fake_package_descriptors
    )
    _clear_task_loader_caches(task_loader)

    execution = task_loader.resolve_tasks(
        task_specs=[{"name": "shared_task"}],
        provider_name="future",
        supported_task_scopes=SUPPORTED_TASK_SCOPES,
    )

    assert execution.ordered[0].run() == "universal:shared_task"


def test_provider_descriptor_index_builds_once_for_multiple_configured_tasks(
    monkeypatch,
):
    task_loader = importlib.import_module("anvil.task_loader")
    _clear_task_loader_caches(task_loader)
    calls = {"packages": 0}

    def fake_package_descriptors(*, package_name, source_label, provider_name):
        calls["packages"] += 1
        if source_label == "build_once":
            return [
                _descriptor("alpha", source_label),
                _descriptor("beta", source_label),
                _descriptor("gamma", source_label),
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
        supported_task_scopes=SUPPORTED_TASK_SCOPES,
    )

    assert [task.name for task in execution.ordered] == ["alpha", "beta", "gamma"]
    assert calls == {"packages": 2}


def test_discover_tasks_includes_github_provider_list(monkeypatch):
    task_loader = importlib.import_module("anvil.task_loader")
    seen: list[str] = []

    def fake_catalog(provider_name):
        seen.append(provider_name)
        return ComponentCatalog.build([])

    monkeypatch.setattr(task_loader, "_provider_task_catalog", fake_catalog)

    task_loader.discover_tasks()

    assert seen == ["aws", "azure", "gcp", "github"]


def test_discover_tasks_scans_universal_sources_once(monkeypatch):
    task_loader = importlib.import_module("anvil.task_loader")
    package_sources: list[str] = []

    def fake_package_descriptors(*, package_name, source_label, provider_name):
        package_sources.append(source_label)
        return []

    monkeypatch.setattr(
        task_loader, "_iter_package_task_descriptors", fake_package_descriptors
    )
    monkeypatch.setattr(
        task_loader, "_iter_plugin_task_descriptors", lambda **kwargs: ([], [])
    )
    _clear_task_loader_caches(task_loader)

    task_loader.discover_tasks()

    assert package_sources.count("universal") == 1
    assert sorted(source for source in package_sources if source != "universal") == [
        "aws",
        "azure",
        "gcp",
        "github",
    ]


def test_repeated_list_tasks_reuses_provider_discovery_snapshot(monkeypatch):
    from anvil import provider_loader

    task_loader = importlib.import_module("anvil.task_loader")
    entry_point_scans = 0

    def fake_entry_points(*, group):
        nonlocal entry_point_scans
        entry_point_scans += 1
        return []

    monkeypatch.setattr(provider_loader, "entry_points", fake_entry_points)
    provider_loader._clear_provider_caches()
    _clear_task_loader_caches(task_loader)

    task_loader.list_tasks()
    task_loader.list_tasks()

    assert entry_point_scans == 1
