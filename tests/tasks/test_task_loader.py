import importlib
import types

import pytest

from anvil.task_loader import (
    TaskConfigError,
    TaskDescriptor,
    discover_tasks,
    list_tasks,
    resolve_tasks,
)


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

    assert isinstance(tasks, list)
    assert all(isinstance(task, TaskDescriptor) for task in tasks)

    assert any(task.name == "noop" and task.source == "stock" for task in tasks)
    assert any(
        task.name == "remove_iam_user" and task.source == "stock" for task in tasks
    )


def test_list_tasks_sorted_by_source_then_name():
    tasks = list_tasks()

    pairs = [(task.source, task.name) for task in tasks]
    assert pairs == sorted(pairs)


def test_discover_tasks_includes_stock_tasks():
    tasks = discover_tasks().tasks
    names = {task.name for task in tasks}

    assert "noop" in names

    noop = next(task for task in tasks if task.name == "noop")
    assert noop.source == "stock"
    assert callable(noop.load)
