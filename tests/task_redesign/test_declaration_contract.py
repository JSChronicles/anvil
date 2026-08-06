from __future__ import annotations

import sys
from types import ModuleType

import pytest

from anvil.task_loader import TaskConfigError, TaskScope, resolve_tasks
from anvil.validators import validate_config_schema


def _config(tasks: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema_version": 2,
        "targets": [
            {
                "name": "contract-target",
                "provider": {"name": "aws", "mode": "accounts"},
                "include": ["111111111111"],
                "regions": ["us-east-1"],
                "tasks": tasks,
            }
        ],
    }


def _install_task_modules(
    monkeypatch: pytest.MonkeyPatch, scopes: dict[str, str]
) -> None:
    runs = {}
    for task_name, scope in scopes.items():
        module_name = f"tests.task_redesign.fake_{task_name}"
        module = ModuleType(module_name)
        module.TASK_SCOPE = scope

        def run(**kwargs):
            return kwargs

        run.__module__ = module_name
        module.run = run
        monkeypatch.setitem(sys.modules, module_name, module)
        runs[task_name] = run

    monkeypatch.setattr(
        "anvil.task_loader._load_provider_task_callable",
        lambda *, provider_name, task_name: runs[task_name],
    )
    resolve_tasks.__globals__["_resolve_tasks_cached"].cache_clear()


def test_schema_v2_accepts_complete_redesigned_task_declaration() -> None:
    validate_config_schema(
        config=_config(
            [
                {
                    "id": "restore_guardrails",
                    "name": "reconcile_config_guardrails",
                    "depends_on": ["detach_guardrails"],
                    "always_run": True,
                    "metadata": {"attachment_state": "present"},
                    "dependency_data": {
                        "attachments": {
                            "task_id": "detach_guardrails",
                            "path": "result.attachments",
                        }
                    },
                },
                {
                    "id": "detach_guardrails",
                    "name": "reconcile_config_guardrails",
                    "metadata": {"attachment_state": "absent"},
                },
            ]
        )
    )


@pytest.mark.parametrize(
    ("invalid_id", "expected_detail"), [("", "non-empty"), (None, "string")]
)
def test_schema_v2_rejects_empty_or_null_explicit_id(
    invalid_id: object, expected_detail: str
) -> None:
    with pytest.raises(ValueError) as error:
        validate_config_schema(config=_config([{"id": invalid_id, "name": "noop"}]))

    assert "id" in str(error.value)
    assert expected_detail in str(error.value)


def test_schema_documents_every_task_property_with_examples() -> None:
    import json
    from importlib.resources import files

    schema = json.loads(
        files("anvil.schemas")
        .joinpath("common.schema.v2.json")
        .read_text(encoding="utf-8")
    )
    properties = schema["$defs"]["taskEntry"]["properties"]

    assert set(properties) == {
        "id",
        "name",
        "metadata",
        "depends_on",
        "always_run",
        "dependency_data",
    }
    for property_name, declaration in properties.items():
        assert declaration.get("description"), property_name
        assert declaration.get("examples"), property_name

    nested_declarations = {
        "depends_on.items": properties["depends_on"]["items"],
        "dependency_data.propertyNames": properties["dependency_data"]["propertyNames"],
        "dependencyDataReference": schema["$defs"]["dependencyDataReference"],
        **{
            f"dependencyDataReference.{property_name}": declaration
            for property_name, declaration in schema["$defs"][
                "dependencyDataReference"
            ]["properties"].items()
        },
    }
    for property_name, declaration in nested_declarations.items():
        assert declaration.get("description"), property_name
        assert declaration.get("examples"), property_name


def test_schema_rejects_yaml_task_scope_override() -> None:
    with pytest.raises(ValueError, match=r"scope"):
        validate_config_schema(
            config=_config([{"name": "noop", "scope": "configured_target"}])
        )


def test_repeated_component_names_resolve_by_explicit_invocation_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_task_modules(monkeypatch, {"reconcile": "region"})

    execution = resolve_tasks(
        task_specs=[
            {"id": "detach", "name": "reconcile"},
            {"id": "restore", "name": "reconcile", "depends_on": ["detach"]},
        ],
        provider_name="aws",
        supported_task_scopes=frozenset({"region"}),
    )

    assert [(task.id, task.name) for task in execution.ordered] == [
        ("detach", "reconcile"),
        ("restore", "reconcile"),
    ]
    assert execution.ordered[1].depends_on == ["detach"]


def test_repeated_component_names_require_explicit_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_task_modules(monkeypatch, {"reconcile": "region"})

    with pytest.raises(TaskConfigError, match=r"explicit.*unique.*ID"):
        resolve_tasks(
            task_specs=[{"name": "reconcile"}, {"name": "reconcile"}],
            provider_name="aws",
            supported_task_scopes=frozenset({"region"}),
        )


def test_omitted_id_defaults_to_component_name(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_task_modules(monkeypatch, {"inventory": "region"})

    execution = resolve_tasks(
        task_specs=[{"name": "inventory"}],
        provider_name="aws",
        supported_task_scopes=frozenset({"region"}),
    )

    assert execution.ordered[0].id == "inventory"
    assert execution.ordered[0].name == "inventory"


def test_effective_task_ids_must_be_unique(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_task_modules(monkeypatch, {"inventory": "region", "cleanup": "region"})

    with pytest.raises(TaskConfigError, match=r"Duplicate task ID.*shared"):
        resolve_tasks(
            task_specs=[
                {"id": "shared", "name": "inventory"},
                {"id": "shared", "name": "cleanup"},
            ],
            provider_name="aws",
            supported_task_scopes=frozenset({"region"}),
        )


def test_every_repeated_component_occurrence_requires_explicit_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_task_modules(monkeypatch, {"reconcile": "region"})

    with pytest.raises(TaskConfigError, match=r"every occurrence.*explicit.*ID"):
        resolve_tasks(
            task_specs=[{"name": "reconcile"}, {"id": "second", "name": "reconcile"}],
            provider_name="aws",
            supported_task_scopes=frozenset({"region"}),
        )


def test_dependencies_do_not_fall_back_to_component_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_task_modules(monkeypatch, {"producer": "region", "consumer": "region"})

    with pytest.raises(TaskConfigError, match=r"unknown task ID.*producer"):
        resolve_tasks(
            task_specs=[
                {"id": "producer_invocation", "name": "producer"},
                {"name": "consumer", "depends_on": ["producer"]},
            ],
            provider_name="aws",
            supported_task_scopes=frozenset({"region"}),
        )


def test_dependency_cycles_are_reported_by_invocation_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_task_modules(
        monkeypatch, {"component_a": "region", "component_b": "region"}
    )

    with pytest.raises(TaskConfigError, match=r"Cycle.*invocation_a.*invocation_b"):
        resolve_tasks(
            task_specs=[
                {
                    "id": "invocation_a",
                    "name": "component_a",
                    "depends_on": ["invocation_b"],
                },
                {
                    "id": "invocation_b",
                    "name": "component_b",
                    "depends_on": ["invocation_a"],
                },
            ],
            provider_name="aws",
            supported_task_scopes=frozenset({"region"}),
        )


def test_dependency_data_requires_direct_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_task_modules(monkeypatch, {"producer": "region", "consumer": "region"})

    with pytest.raises(TaskConfigError, match=r"direct.*depends_on"):
        resolve_tasks(
            task_specs=[
                {"name": "producer"},
                {
                    "name": "consumer",
                    "dependency_data": {
                        "payload": {"task_id": "producer", "path": "result.value"}
                    },
                },
            ],
            provider_name="aws",
            supported_task_scopes=frozenset({"region"}),
        )


@pytest.mark.parametrize(
    "dependency_data",
    [
        {"": {"task_id": "producer"}},
        {"payload": {"task_id": ""}},
        {"payload": {"task_id": "producer", "path": ""}},
        {"payload": {"task_id": "producer", "path": ".result"}},
        {"payload": {"task_id": "producer", "path": "result..value"}},
        {"payload": {"task_id": "producer", "path": "result[0]"}},
        {"payload": {"task_id": "producer", "path": "result", "unknown": True}},
    ],
)
def test_schema_rejects_invalid_dependency_data_references(
    dependency_data: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match=r"dependency_data|path|task_id"):
        validate_config_schema(
            config=_config(
                [
                    {"name": "producer"},
                    {
                        "name": "consumer",
                        "depends_on": ["producer"],
                        "dependency_data": dependency_data,
                    },
                ]
            )
        )


def test_always_run_requires_at_least_one_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_task_modules(monkeypatch, {"cleanup": "region"})

    with pytest.raises(TaskConfigError, match=r"always_run.*depend"):
        resolve_tasks(
            task_specs=[{"name": "cleanup", "always_run": True}],
            provider_name="aws",
            supported_task_scopes=frozenset({"region"}),
        )


def test_configured_target_is_a_module_declared_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_task_modules(monkeypatch, {"organization_task": "configured_target"})

    execution = resolve_tasks(
        task_specs=[{"name": "organization_task"}],
        provider_name="aws",
        supported_task_scopes=frozenset({"configured_target", "target", "region"}),
    )

    assert execution.ordered[0].scope is TaskScope.CONFIGURED_TARGET


def test_task_metadata_and_dependency_data_are_preserved_and_isolated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_task_modules(monkeypatch, {"producer": "region", "consumer": "region"})
    specs = [
        {"name": "producer"},
        {
            "name": "consumer",
            "depends_on": ["producer"],
            "always_run": True,
            "metadata": {"nested": {"items": ["configured"]}},
            "dependency_data": {
                "payload": {"task_id": "producer", "path": "result.value"}
            },
        },
    ]

    first = resolve_tasks(
        task_specs=specs,
        provider_name="aws",
        supported_task_scopes=frozenset({"region"}),
    )
    first.ordered[1].metadata["nested"]["items"].append("mutated")
    first.ordered[1].dependency_data["payload"]["path"] = "result.changed"

    second = resolve_tasks(
        task_specs=specs,
        provider_name="aws",
        supported_task_scopes=frozenset({"region"}),
    )

    assert second.ordered[1].always_run
    assert second.ordered[1].metadata == {"nested": {"items": ["configured"]}}
    assert second.ordered[1].dependency_data == {
        "payload": {"task_id": "producer", "path": "result.value"}
    }


def test_resolution_cache_distinguishes_invocation_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_task_modules(monkeypatch, {"inventory": "region"})
    cached_resolver = resolve_tasks.__globals__["_resolve_tasks_cached"]

    resolve_tasks(
        task_specs=[{"id": "first", "name": "inventory"}],
        provider_name="aws",
        supported_task_scopes=frozenset({"region"}),
    )
    after_first = cached_resolver.cache_info()
    resolve_tasks(
        task_specs=[{"id": "second", "name": "inventory"}],
        provider_name="aws",
        supported_task_scopes=frozenset({"region"}),
    )
    after_second = cached_resolver.cache_info()

    assert after_second.misses == after_first.misses + 1


def test_resolution_cache_normalizes_mapping_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_task_modules(monkeypatch, {"inventory": "region"})
    cached_resolver = resolve_tasks.__globals__["_resolve_tasks_cached"]

    first = resolve_tasks(
        task_specs=[
            {
                "name": "inventory",
                "metadata": {"alpha": 1, "nested": {"left": True, "right": False}},
            }
        ],
        provider_name="aws",
        supported_task_scopes=frozenset({"region"}),
    )
    after_first = cached_resolver.cache_info()
    second = resolve_tasks(
        task_specs=[
            {
                "name": "inventory",
                "metadata": {"nested": {"right": False, "left": True}, "alpha": 1},
            }
        ],
        provider_name="aws",
        supported_task_scopes=frozenset({"region"}),
    )
    after_second = cached_resolver.cache_info()

    assert after_second.hits == after_first.hits + 1
    assert first.ordered[0].metadata == second.ordered[0].metadata


def test_resolution_cache_uses_effective_id_for_implicit_and_explicit_forms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_task_modules(monkeypatch, {"inventory": "region"})
    cached_resolver = resolve_tasks.__globals__["_resolve_tasks_cached"]

    implicit = resolve_tasks(
        task_specs=[{"name": "inventory"}],
        provider_name="aws",
        supported_task_scopes=frozenset({"region"}),
    )
    after_implicit = cached_resolver.cache_info()
    explicit = resolve_tasks(
        task_specs=[{"id": "inventory", "name": "inventory"}],
        provider_name="aws",
        supported_task_scopes=frozenset({"region"}),
    )
    after_explicit = cached_resolver.cache_info()

    assert after_explicit.hits == after_implicit.hits + 1
    assert implicit.ordered[0].id == explicit.ordered[0].id == "inventory"
