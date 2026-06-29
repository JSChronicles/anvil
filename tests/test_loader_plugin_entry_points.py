from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from anvil import cli
from anvil import processor_loader, task_loader


def _write_plugin_distribution(
    *,
    root: Path,
    distribution_name: str,
    package_name: str,
    entry_point_group: str,
    entry_point_name: str,
    module_name: str,
    module_body: str,
) -> None:
    package_dir = root / package_name
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    (package_dir / f"{module_name}.py").write_text(module_body, encoding="utf-8")

    dist_info_dir = root / f"{distribution_name.replace('-', '_')}-1.0.dist-info"
    dist_info_dir.mkdir()
    (dist_info_dir / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {distribution_name}\nVersion: 1.0\n",
        encoding="utf-8",
    )
    (dist_info_dir / "entry_points.txt").write_text(
        f"[{entry_point_group}]\n{entry_point_name} = {package_name}\n",
        encoding="utf-8",
    )


def _write_entry_point_distribution(
    *,
    root: Path,
    distribution_name: str,
    entry_point_group: str,
    entry_point_name: str,
    entry_point_value: str,
) -> None:
    dist_info_dir = root / f"{distribution_name.replace('-', '_')}-1.0.dist-info"
    dist_info_dir.mkdir()
    (dist_info_dir / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {distribution_name}\nVersion: 1.0\n",
        encoding="utf-8",
    )
    (dist_info_dir / "entry_points.txt").write_text(
        f"[{entry_point_group}]\n{entry_point_name} = {entry_point_value}\n",
        encoding="utf-8",
    )


def _write_duplicate_task_entry_points_distribution(
    *,
    root: Path,
    distribution_name: str,
    entry_point_group: str,
    task_name: str,
) -> None:
    package_names = [
        f"{distribution_name.replace('-', '_')}_first",
        f"{distribution_name.replace('-', '_')}_second",
    ]
    for package_name in package_names:
        package_dir = root / package_name
        package_dir.mkdir()
        (package_dir / "__init__.py").write_text("", encoding="utf-8")
        (package_dir / f"{task_name}.py").write_text(
            "def run(**kwargs):\n    return None\n",
            encoding="utf-8",
        )

    dist_info_dir = root / f"{distribution_name.replace('-', '_')}-1.0.dist-info"
    dist_info_dir.mkdir()
    (dist_info_dir / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {distribution_name}\nVersion: 1.0\n",
        encoding="utf-8",
    )
    (dist_info_dir / "entry_points.txt").write_text(
        (
            f"[{entry_point_group}]\n"
            f"first = {package_names[0]}\n"
            f"second = {package_names[1]}\n"
        ),
        encoding="utf-8",
    )


def _clear_task_loader_caches() -> None:
    task_loader._clear_task_caches()
    task_loader._resolve_tasks_cached.cache_clear()


def test_resolve_tasks_ignores_legacy_task_plugin_entry_point(monkeypatch, tmp_path):
    _write_plugin_distribution(
        root=tmp_path,
        distribution_name="anvil-test-task-plugin",
        package_name="anvil_test_task_plugin",
        entry_point_group="anvil.tasks",
        entry_point_name="test-task-plugin",
        module_name="real_plugin_task",
        module_body='def run(**kwargs):\n    return {"source": "plugin-task"}\n',
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    _clear_task_loader_caches()

    with pytest.raises(task_loader.TaskConfigError, match="provider package"):
        task_loader.resolve_tasks(task_specs=[{"name": "real_plugin_task"}])


def test_resolve_tasks_does_not_import_legacy_task_plugin_entry_point(
    monkeypatch, tmp_path
):
    _write_plugin_distribution(
        root=tmp_path,
        distribution_name="anvil-test-broken-task-plugin",
        package_name="anvil_test_broken_task_plugin",
        entry_point_group="anvil.tasks",
        entry_point_name="test-broken-task-plugin",
        module_name="broken_plugin_task",
        module_body="import missing_dependency\n\ndef run(**kwargs):\n    return None\n",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    _clear_task_loader_caches()

    with pytest.raises(task_loader.TaskConfigError) as exc_info:
        task_loader.resolve_tasks(task_specs=[{"name": "broken_plugin_task"}])

    error = str(exc_info.value)
    assert "provider package" in error
    assert "missing_dependency" not in error


def test_discover_tasks_ignores_legacy_task_plugin_entry_point(monkeypatch, tmp_path):
    _write_plugin_distribution(
        root=tmp_path,
        distribution_name="anvil-test-task-discovery-plugin",
        package_name="anvil_test_task_discovery_plugin",
        entry_point_group="anvil.tasks",
        entry_point_name="test-task-discovery-plugin",
        module_name="discoverable_plugin_task",
        module_body='def run(**kwargs):\n    return {"source": "plugin-task"}\n',
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    descriptors = task_loader.discover_tasks().tasks

    assert "discoverable_plugin_task" not in {task.name for task in descriptors}


def test_universal_provider_plugin_task_resolves_for_all_providers(
    monkeypatch, tmp_path
):
    _write_plugin_distribution(
        root=tmp_path,
        distribution_name="anvil-test-universal-task-plugin",
        package_name="anvil_test_universal_task_plugin",
        entry_point_group=task_loader.UNIVERSAL_TASK_ENTRY_POINT_GROUP,
        entry_point_name="test-universal-task-plugin",
        module_name="universal_plugin_task",
        module_body='def run(**kwargs):\n    return {"source": "universal-plugin"}\n',
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    _clear_task_loader_caches()

    for provider_name in ("aws", "azure", "gcp"):
        execution = task_loader.resolve_tasks(
            task_specs=[{"name": "universal_plugin_task"}],
            provider_name=provider_name,
        )

        assert execution.ordered[0].run() == {"source": "universal-plugin"}


@pytest.mark.parametrize(
    ("provider_name", "entry_point_group", "task_name"),
    [
        ("aws", "anvil.providers.aws.tasks", "aws_plugin_task"),
        ("azure", "anvil.providers.azure.tasks", "azure_plugin_task"),
        ("gcp", "anvil.providers.gcp.tasks", "gcp_plugin_task"),
    ],
)
def test_provider_specific_plugin_task_resolves_only_for_own_provider(
    monkeypatch, tmp_path, provider_name, entry_point_group, task_name
):
    _write_plugin_distribution(
        root=tmp_path,
        distribution_name=f"anvil-test-{provider_name}-task-plugin",
        package_name=f"anvil_test_{provider_name}_task_plugin",
        entry_point_group=entry_point_group,
        entry_point_name=f"test-{provider_name}-task-plugin",
        module_name=task_name,
        module_body=f'def run(**kwargs):\n    return "{provider_name}-plugin"\n',
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    _clear_task_loader_caches()

    execution = task_loader.resolve_tasks(
        task_specs=[{"name": task_name}],
        provider_name=provider_name,
    )
    assert execution.ordered[0].run() == f"{provider_name}-plugin"

    other_providers = {"aws", "azure", "gcp"} - {provider_name}
    for other_provider in other_providers:
        with pytest.raises(task_loader.TaskConfigError, match="not available"):
            task_loader.resolve_tasks(
                task_specs=[{"name": task_name}],
                provider_name=other_provider,
            )


def test_duplicate_plugin_and_builtin_task_name_is_ambiguous(monkeypatch, tmp_path):
    _write_plugin_distribution(
        root=tmp_path,
        distribution_name="anvil-test-duplicate-aws-task-plugin",
        package_name="anvil_test_duplicate_aws_task_plugin",
        entry_point_group="anvil.providers.aws.tasks",
        entry_point_name="test-duplicate-aws-task-plugin",
        module_name="count_vpc",
        module_body='def run(**kwargs):\n    return {"source": "plugin"}\n',
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    _clear_task_loader_caches()

    with pytest.raises(task_loader.TaskConfigError) as exc_info:
        task_loader.resolve_tasks(
            task_specs=[{"name": "count_vpc"}],
            provider_name="aws",
        )

    error = str(exc_info.value)
    assert "ambiguous for provider 'aws'" in error
    assert "aws" in error
    assert "aws plugin: anvil-test-duplicate-aws-task-plugin" in error


def test_duplicate_universal_and_provider_plugin_names_are_ambiguous(
    monkeypatch, tmp_path
):
    _write_plugin_distribution(
        root=tmp_path,
        distribution_name="anvil-test-shared-universal-plugin",
        package_name="anvil_test_shared_universal_plugin",
        entry_point_group=task_loader.UNIVERSAL_TASK_ENTRY_POINT_GROUP,
        entry_point_name="test-shared-universal-plugin",
        module_name="shared_plugin_task",
        module_body='def run(**kwargs):\n    return "universal"\n',
    )
    _write_plugin_distribution(
        root=tmp_path,
        distribution_name="anvil-test-shared-aws-plugin",
        package_name="anvil_test_shared_aws_plugin",
        entry_point_group="anvil.providers.aws.tasks",
        entry_point_name="test-shared-aws-plugin",
        module_name="shared_plugin_task",
        module_body='def run(**kwargs):\n    return "aws"\n',
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    _clear_task_loader_caches()

    with pytest.raises(task_loader.TaskConfigError) as exc_info:
        task_loader.resolve_tasks(
            task_specs=[{"name": "shared_plugin_task"}],
            provider_name="aws",
        )

    error = str(exc_info.value)
    assert "ambiguous for provider 'aws'" in error
    assert "universal plugin: anvil-test-shared-universal-plugin" in error
    assert "aws plugin: anvil-test-shared-aws-plugin" in error

    azure_execution = task_loader.resolve_tasks(
        task_specs=[{"name": "shared_plugin_task"}],
        provider_name="azure",
    )
    assert azure_execution.ordered[0].run() == "universal"


def test_duplicate_same_distribution_plugin_names_fail_full_task_validation(
    monkeypatch, tmp_path
):
    _write_duplicate_task_entry_points_distribution(
        root=tmp_path,
        distribution_name="anvil-test-duplicate-universal-plugin",
        entry_point_group=task_loader.UNIVERSAL_TASK_ENTRY_POINT_GROUP,
        task_name="duplicated_plugin_task",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    _clear_task_loader_caches()

    with pytest.raises(task_loader.TaskConfigError, match="ambiguous"):
        task_loader.resolve_tasks(
            task_specs=[{"name": "duplicated_plugin_task"}],
            provider_name="aws",
        )

    with pytest.raises(ValueError, match="duplicate task name: duplicated_plugin_task"):
        cli._validate_selected_tasks([])


def test_duplicate_same_distribution_plugin_names_fail_selected_task_validation(
    monkeypatch, tmp_path
):
    _write_duplicate_task_entry_points_distribution(
        root=tmp_path,
        distribution_name="anvil-test-selected-duplicate-plugin",
        entry_point_group=task_loader.UNIVERSAL_TASK_ENTRY_POINT_GROUP,
        task_name="selected_duplicate_plugin_task",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    _clear_task_loader_caches()

    with pytest.raises(
        ValueError, match="duplicate task name: selected_duplicate_plugin_task"
    ):
        cli._validate_selected_tasks(["selected_duplicate_plugin_task"])


def test_plugin_task_import_error_is_reported_during_task_validation(
    monkeypatch, tmp_path
):
    _write_plugin_distribution(
        root=tmp_path,
        distribution_name="anvil-test-broken-provider-task-plugin",
        package_name="anvil_test_broken_provider_task_plugin",
        entry_point_group=task_loader.UNIVERSAL_TASK_ENTRY_POINT_GROUP,
        entry_point_name="test-broken-provider-task-plugin",
        module_name="broken_provider_plugin_task",
        module_body="import missing_dependency\n\ndef run(**kwargs):\n    return None\n",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    _clear_task_loader_caches()

    with pytest.raises(ValueError) as exc_info:
        cli._validate_selected_tasks(["broken_provider_plugin_task"])

    error = str(exc_info.value)
    assert "broken_provider_plugin_task" in error
    assert "universal plugin: anvil-test-broken-provider-task-plugin" in error
    assert "missing_dependency" in error


def test_plugin_package_discovery_error_is_reported_during_task_validation(
    monkeypatch, tmp_path
):
    _write_entry_point_distribution(
        root=tmp_path,
        distribution_name="anvil-test-missing-provider-task-plugin",
        entry_point_group=task_loader.UNIVERSAL_TASK_ENTRY_POINT_GROUP,
        entry_point_name="test-missing-provider-task-plugin",
        entry_point_value="missing_provider_task_plugin",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    _clear_task_loader_caches()

    with pytest.raises(ValueError) as exc_info:
        cli._validate_selected_tasks([])

    error = str(exc_info.value)
    assert "test-missing-provider-task-plugin" in error
    assert "universal plugin: anvil-test-missing-provider-task-plugin" in error
    assert "package import failed" in error


def test_discover_processors_includes_real_plugin_entry_point(monkeypatch, tmp_path):
    _write_plugin_distribution(
        root=tmp_path,
        distribution_name="anvil-test-processor-plugin",
        package_name="anvil_test_processor_plugin",
        entry_point_group=processor_loader.PROCESSOR_ENTRY_POINT_GROUP,
        entry_point_name="test-processor-plugin",
        module_name="real_plugin_processor",
        module_body=(
            "def run(*, context, output, metadata):\n"
            '    return {"output": output, "metadata": metadata}\n'
        ),
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    processor_loader.load_processor_callable.cache_clear()

    descriptors = processor_loader.discover_processors().processors
    descriptor = next(
        processor
        for processor in descriptors
        if processor.name == "real_plugin_processor"
    )

    assert descriptor.source == "plugin: anvil-test-processor-plugin"
    assert descriptor.load()(
        context=None, output="report.md", metadata={"ok": True}
    ) == {"output": "report.md", "metadata": {"ok": True}}
