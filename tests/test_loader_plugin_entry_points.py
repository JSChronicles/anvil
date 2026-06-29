from __future__ import annotations

import importlib
from pathlib import Path

import pytest

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
    task_loader._clear_task_caches()
    task_loader._resolve_tasks_cached.cache_clear()

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
    task_loader._clear_task_caches()
    task_loader._resolve_tasks_cached.cache_clear()

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
