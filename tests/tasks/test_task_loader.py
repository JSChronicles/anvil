import pytest

from anvil.task_loader import (
    TaskConfigError,
    TaskDescriptor,
    discover_tasks,
    list_tasks,
    resolve_tasks,
)


def _run(**kwargs):
    return None


def _descriptor(name: str) -> TaskDescriptor:
    return TaskDescriptor(name=name, load=lambda: _run, source="aws")


def _mock_provider_tasks(monkeypatch, names: list[str]) -> None:
    def fake_index(provider_name: str):
        return {name: [_descriptor(name)] for name in names}

    monkeypatch.setattr("anvil.task_loader.provider_task_descriptor_index", fake_index)
    monkeypatch.setattr(
        "anvil.task_loader._provider_task_descriptor_index",
        lambda provider_name: {name: (_descriptor(name),) for name in names},
    )
    resolve_tasks.__globals__["_resolve_tasks_cached"].cache_clear()
    resolve_tasks.__globals__["_load_provider_task_callable"].cache_clear()


def test_resolve_tasks_no_dependencies(monkeypatch):
    _mock_provider_tasks(monkeypatch, ["a", "b"])

    execution = resolve_tasks(task_specs=[{"name": "a"}, {"name": "b"}])
    assert [task.name for task in execution.ordered] == ["a", "b"]


def test_resolve_tasks_dependency_order(monkeypatch):
    _mock_provider_tasks(monkeypatch, ["a", "b"])

    execution = resolve_tasks(
        task_specs=[{"name": "b", "depends_on": ["a"]}, {"name": "a"}]
    )

    assert [task.name for task in execution.ordered] == ["a", "b"]


def test_resolve_tasks_cycle(monkeypatch):
    _mock_provider_tasks(monkeypatch, ["a", "b"])

    with pytest.raises(TaskConfigError):
        resolve_tasks(
            task_specs=[
                {"name": "a", "depends_on": ["b"]},
                {"name": "b", "depends_on": ["a"]},
            ]
        )


def test_resolve_tasks_rejects_duplicate_configured_task_names(monkeypatch):
    _mock_provider_tasks(monkeypatch, ["a"])

    with pytest.raises(TaskConfigError, match="Duplicate task name detected: 'a'"):
        resolve_tasks(task_specs=[{"name": "a"}, {"name": "a"}])


def test_resolve_tasks_reports_missing_task_usefully():
    with pytest.raises(TaskConfigError) as exc_info:
        resolve_tasks(task_specs=[{"name": "missing_task_for_test"}])

    error = str(exc_info.value)
    assert "missing_task_for_test" in error
    assert "anvil.providers.tasks" in error
    assert "anvil.providers.aws.tasks" in error


def test_discover_tasks_does_not_load_provider_task_callables(monkeypatch):
    import anvil._loader_utils as loader_utils

    def fail_load(task_name):
        raise AssertionError(f"task should not load during discovery: {task_name}")

    monkeypatch.setattr(loader_utils, "entry_points", lambda *, group: [])
    monkeypatch.setattr("anvil.task_loader._load_package_task", fail_load)

    discover_tasks()


def test_list_tasks_includes_provider_tasks():
    import anvil.task_loader as task_loader

    task_loader._clear_task_caches()

    tasks = list_tasks()

    assert isinstance(tasks, list)
    assert all(isinstance(task, TaskDescriptor) for task in tasks)

    assert any(task.name == "noop" and task.source == "universal" for task in tasks)
    assert any(
        task.name == "remove_iam_user" and task.source == "aws" for task in tasks
    )


def test_list_tasks_sorted_by_source_then_name():
    import anvil.task_loader as task_loader

    task_loader._clear_task_caches()

    tasks = list_tasks()

    pairs = [(task.source, task.name) for task in tasks]
    assert pairs == sorted(pairs)


def test_discover_tasks_includes_provider_tasks():
    import anvil.task_loader as task_loader

    task_loader._clear_task_caches()

    tasks = discover_tasks().tasks
    names = {task.name for task in tasks}

    assert "noop" in names

    noop = next(task for task in tasks if task.name == "noop")
    assert noop.source == "universal"
    assert callable(noop.load)


