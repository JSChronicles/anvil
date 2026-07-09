from __future__ import annotations

import importlib

from anvil.task_loader import TaskDescriptor


def _patch_provider_tasks(monkeypatch, task_loader, load_calls: list[str]) -> None:
    def descriptor(task_name: str) -> TaskDescriptor:
        def load():
            load_calls.append(task_name)

            def run(**kwargs):
                return task_name

            return run

        return TaskDescriptor(name=task_name, load=load, source="aws")

    monkeypatch.setattr(
        task_loader,
        "_provider_task_descriptor_index",
        lambda provider_name: {
            "alpha": (descriptor("alpha"),),
            "beta": (descriptor("beta"),),
        },
    )
    task_loader._load_provider_task_callable.cache_clear()
    task_loader._resolve_tasks_cached.cache_clear()


def test_repeated_identical_task_specs_reuse_cached_resolution(monkeypatch):
    task_loader = importlib.import_module("anvil.task_loader")
    task_loader._resolve_tasks_cached.cache_clear()
    task_loader._clear_task_caches()

    load_calls: list[str] = []
    _patch_provider_tasks(monkeypatch, task_loader, load_calls)

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
    task_loader._clear_task_caches()

    load_calls: list[str] = []
    _patch_provider_tasks(monkeypatch, task_loader, load_calls)

    first_specs = [{"name": "alpha"}, {"name": "beta"}]
    second_specs = [{"name": "beta"}, {"name": "alpha"}]

    first = task_loader.resolve_tasks(task_specs=first_specs)
    second = task_loader.resolve_tasks(task_specs=second_specs)

    assert [task.name for task in first.ordered] == ["alpha", "beta"]
    assert [task.name for task in second.ordered] == ["beta", "alpha"]
    assert load_calls == ["alpha", "beta"]


def test_returned_values_do_not_expose_shared_mutable_cached_state(monkeypatch):
    task_loader = importlib.import_module("anvil.task_loader")
    task_loader._resolve_tasks_cached.cache_clear()
    task_loader._clear_task_caches()

    _patch_provider_tasks(monkeypatch, task_loader, [])

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
