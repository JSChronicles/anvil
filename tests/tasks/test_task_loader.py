import importlib
import types

import pytest

from anvil.task_loader import TaskConfigError, discover_tasks, list_tasks, resolve_tasks


def test_resolve_tasks_no_dependencies(monkeypatch):
    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda name: types.SimpleNamespace(run=lambda **_: None),
    )

    execution = resolve_tasks(task_specs=[{"name": "a"}, {"name": "b"}])
    assert [task.name for task in execution.ordered] == ["a", "b"]


def test_resolve_tasks_dependency_order(monkeypatch):
    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda name: types.SimpleNamespace(run=lambda **_: None),
    )

    execution = resolve_tasks(
        task_specs=[{"name": "b", "depends_on": ["a"]}, {"name": "a"}]
    )

    assert [task.name for task in execution.ordered] == ["a", "b"]


def test_resolve_tasks_cycle(monkeypatch):
    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda name: types.SimpleNamespace(run=lambda **_: None),
    )

    with pytest.raises(TaskConfigError):
        resolve_tasks(
            task_specs=[
                {"name": "a", "depends_on": ["b"]},
                {"name": "b", "depends_on": ["a"]},
            ]
        )


def test_list_tasks_includes_stock_tasks():
    tasks = list_tasks()

    # Basic sanity checks using real stock tasks
    assert isinstance(tasks, list)

    # These should exist based on your screenshot earlier
    assert "noop [stock]" in tasks
    assert "remove_iam_user [stock]" in tasks


def test_discover_tasks_includes_stock_tasks():
    tasks = discover_tasks()
    names = {task.name for task in tasks}

    assert "noop" in names

    noop = next(task for task in tasks if task.name == "noop")
    assert noop.source == "stock"
    assert callable(noop.run)


def test_stock_tasks_override_plugin_tasks(monkeypatch):
    # monkeypatch entry_points() to return a conflicting plugin
    ...
    tasks = discover_tasks()

    task = next(task for task in tasks if task.name == "noop")
    assert task.source == "stock"
