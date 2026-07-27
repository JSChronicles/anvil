import pytest
import sys
from types import ModuleType

from anvil._components import (
    ComponentCatalog,
    ComponentDescriptor,
    ComponentOrigin,
    ComponentSource,
)
from anvil.task_loader import (
    TaskConfigError,
    TaskDescriptor,
    TaskScope,
    discover_tasks,
    list_tasks,
    resolve_tasks,
)


def _run(**kwargs):
    return None


def _descriptor(name: str) -> TaskDescriptor:
    return TaskDescriptor(
        name=name,
        load=lambda: _run,
        source=ComponentSource(
            origin=ComponentOrigin.STOCK,
            package="tests.tasks",
            label="aws",
            provider="aws",
        ),
    )


def _resolve(task_specs):
    return resolve_tasks(
        task_specs=task_specs,
        provider_name="aws",
        supported_task_scopes=frozenset({"region"}),
    )


def _mock_provider_tasks(monkeypatch, names: list[str]) -> None:
    catalog = ComponentCatalog.build(_descriptor(name) for name in names)

    def fake_index(provider_name: str):
        return {
            name: list(descriptors) for name, descriptors in catalog.inventory.items()
        }

    monkeypatch.setattr("anvil.task_loader.provider_task_descriptor_index", fake_index)
    monkeypatch.setattr(
        "anvil.task_loader._provider_task_catalog", lambda provider_name: catalog
    )
    resolve_tasks.__globals__["_resolve_tasks_cached"].cache_clear()
    resolve_tasks.__globals__["_load_provider_task_callable"].cache_clear()


def _mock_scoped_tasks(monkeypatch, scopes: dict[str, object]) -> None:
    runs = {}
    for name, scope in scopes.items():
        module_name = f"tests.fake_task_{name}_{scope}"
        module = ModuleType(module_name)
        if scope is not None:
            module.TASK_SCOPE = scope

        def run(**kwargs):
            return kwargs

        run.__module__ = module_name
        module.run = run
        monkeypatch.setitem(sys.modules, module_name, module)
        runs[name] = run

    monkeypatch.setattr(
        "anvil.task_loader._load_provider_task_callable",
        lambda *, provider_name, task_name: runs[task_name],
    )
    resolve_tasks.__globals__["_resolve_tasks_cached"].cache_clear()


def test_resolve_tasks_no_dependencies(monkeypatch):
    _mock_provider_tasks(monkeypatch, ["a", "b"])

    execution = _resolve([{"name": "a"}, {"name": "b"}])
    assert [task.name for task in execution.ordered] == ["a", "b"]
    assert all(task.scope is TaskScope.REGION for task in execution.ordered)


def test_resolve_tasks_loads_explicit_region_and_target_scopes(monkeypatch):
    _mock_scoped_tasks(
        monkeypatch, {"regional": TaskScope.REGION, "target_wide": "target"}
    )

    execution = resolve_tasks(
        task_specs=[{"name": "regional"}, {"name": "target_wide"}],
        provider_name="azure",
        supported_task_scopes=frozenset({"region", "target"}),
    )

    assert [task.scope for task in execution.ordered] == [
        TaskScope.REGION,
        TaskScope.TARGET,
    ]


def test_resolve_tasks_rejects_invalid_task_scope(monkeypatch):
    _mock_scoped_tasks(monkeypatch, {"bad": "subscription"})

    with pytest.raises(TaskConfigError, match="invalid TASK_SCOPE"):
        resolve_tasks(
            task_specs=[{"name": "bad"}],
            provider_name="azure",
            supported_task_scopes=frozenset({"region", "target"}),
        )


def test_resolve_tasks_rejects_scope_unsupported_by_provider(monkeypatch):
    _mock_scoped_tasks(monkeypatch, {"target_wide": "target"})

    with pytest.raises(TaskConfigError, match="provider 'aws' does not support"):
        resolve_tasks(
            task_specs=[{"name": "target_wide"}],
            provider_name="aws",
            supported_task_scopes=frozenset({"region"}),
        )


def test_region_task_may_depend_on_target_task(monkeypatch):
    _mock_scoped_tasks(monkeypatch, {"target_wide": "target", "regional": "region"})

    execution = resolve_tasks(
        task_specs=[
            {"name": "regional", "depends_on": ["target_wide"]},
            {"name": "target_wide"},
        ],
        provider_name="azure",
        supported_task_scopes=frozenset({"region", "target"}),
    )

    assert [task.name for task in execution.ordered] == ["target_wide", "regional"]


def test_target_task_cannot_depend_on_region_task(monkeypatch):
    _mock_scoped_tasks(monkeypatch, {"target_wide": "target", "regional": "region"})

    with pytest.raises(TaskConfigError, match="execute before regional fan-out"):
        resolve_tasks(
            task_specs=[
                {"name": "target_wide", "depends_on": ["regional"]},
                {"name": "regional"},
            ],
            provider_name="azure",
            supported_task_scopes=frozenset({"region", "target"}),
        )


def test_resolve_tasks_dependency_order(monkeypatch):
    _mock_provider_tasks(monkeypatch, ["a", "b"])

    execution = _resolve([{"name": "b", "depends_on": ["a"]}, {"name": "a"}])

    assert [task.name for task in execution.ordered] == ["a", "b"]


def test_resolve_tasks_cycle(monkeypatch):
    _mock_provider_tasks(monkeypatch, ["a", "b"])

    with pytest.raises(TaskConfigError):
        _resolve(
            [{"name": "a", "depends_on": ["b"]}, {"name": "b", "depends_on": ["a"]}]
        )


def test_resolve_tasks_rejects_duplicate_configured_task_names(monkeypatch):
    _mock_provider_tasks(monkeypatch, ["a"])

    with pytest.raises(TaskConfigError, match="Duplicate task name detected: 'a'"):
        _resolve([{"name": "a"}, {"name": "a"}])


def test_resolve_tasks_reports_missing_task_usefully():
    with pytest.raises(TaskConfigError) as exc_info:
        _resolve([{"name": "missing_task_for_test"}])

    error = str(exc_info.value)
    assert "missing_task_for_test" in error
    assert "anvil.providers.tasks" in error
    assert "anvil.providers.aws.tasks" in error


def test_discover_tasks_does_not_load_provider_task_callables(monkeypatch):
    def fail_load(task_name):
        raise AssertionError(f"task should not load during discovery: {task_name}")

    monkeypatch.setattr("anvil.task_loader._load_package_task", fail_load)

    discover_tasks()


def test_list_tasks_includes_provider_tasks():
    import anvil.task_loader as task_loader

    task_loader._clear_task_caches()

    tasks = list_tasks()

    assert isinstance(tasks, list)
    assert all(isinstance(task, ComponentDescriptor) for task in tasks)

    assert any(
        task.name == "noop" and str(task.source) == "universal" for task in tasks
    )
    assert any(
        task.name == "remove_iam_user" and str(task.source) == "aws" for task in tasks
    )


def test_list_tasks_sorted_by_source_then_name():
    import anvil.task_loader as task_loader

    task_loader._clear_task_caches()

    tasks = list_tasks()

    pairs = [(str(task.source), task.name) for task in tasks]
    assert pairs == sorted(pairs)


def test_discover_tasks_includes_provider_tasks():
    import anvil.task_loader as task_loader

    task_loader._clear_task_caches()

    tasks = discover_tasks().tasks
    names = {task.name for task in tasks}

    assert "noop" in names

    noop = next(task for task in tasks if task.name == "noop")
    assert str(noop.source) == "universal"
    assert callable(noop.load)
