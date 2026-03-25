from __future__ import annotations

import importlib


def test_repeated_identical_task_specs_reuse_cached_resolution(monkeypatch):
    task_loader = importlib.import_module("anvil.task_loader")
    task_loader._resolve_tasks_cached.cache_clear()
    task_loader._load_task_callable.cache_clear()

    load_calls: list[str] = []

    def fake_load(task_name: str):
        load_calls.append(task_name)

        def run(**kwargs):
            return task_name

        return run

    monkeypatch.setattr(task_loader, "_load_task_callable", fake_load)

    task_specs = [{"name": "alpha"}, {"name": "beta", "depends_on": ["alpha"]}]

    first = task_loader.resolve_tasks(task_specs=task_specs)
    second = task_loader.resolve_tasks(task_specs=task_specs)

    assert load_calls == ["alpha", "beta"]
    assert [task.name for task in first.ordered] == ["alpha", "beta"]
    assert [task.name for task in second.ordered] == ["alpha", "beta"]
    assert first is not second
    assert first.ordered is not second.ordered


def test_different_task_order_is_not_treated_as_same_cache_key(monkeypatch):
    task_loader = importlib.import_module("anvil.task_loader")
    task_loader._resolve_tasks_cached.cache_clear()
    task_loader._load_task_callable.cache_clear()

    load_calls: list[str] = []

    def fake_load(task_name: str):
        load_calls.append(task_name)

        def run(**kwargs):
            return task_name

        return run

    monkeypatch.setattr(task_loader, "_load_task_callable", fake_load)

    first_specs = [{"name": "alpha"}, {"name": "beta"}]
    second_specs = [{"name": "beta"}, {"name": "alpha"}]

    first = task_loader.resolve_tasks(task_specs=first_specs)
    second = task_loader.resolve_tasks(task_specs=second_specs)

    assert [task.name for task in first.ordered] == ["alpha", "beta"]
    assert [task.name for task in second.ordered] == ["beta", "alpha"]
    assert load_calls == ["alpha", "beta", "beta", "alpha"]


def test_returned_values_do_not_expose_shared_mutable_cached_state(monkeypatch):
    task_loader = importlib.import_module("anvil.task_loader")
    task_loader._resolve_tasks_cached.cache_clear()
    task_loader._load_task_callable.cache_clear()

    def fake_load(task_name: str):
        def run(**kwargs):
            return task_name

        return run

    monkeypatch.setattr(task_loader, "_load_task_callable", fake_load)

    task_specs = [{"name": "alpha"}, {"name": "beta", "depends_on": ["alpha"]}]

    first = task_loader.resolve_tasks(task_specs=task_specs)
    first.ordered[1].depends_on.append("extra")
    first.adjacency["alpha"].append("extra")
    first.ordered.append(first.ordered[0])

    second = task_loader.resolve_tasks(task_specs=task_specs)

    assert [task.name for task in second.ordered] == ["alpha", "beta"]
    assert second.ordered[1].depends_on == ["alpha"]
    assert second.adjacency["alpha"] == ["beta"]
    assert second.adjacency.get("beta", []) == []
    assert "extra" not in second.adjacency["alpha"]
