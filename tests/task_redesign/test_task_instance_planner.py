from __future__ import annotations

import importlib

import pytest

from anvil.providers.base import ExecutionTarget
from anvil.task_loader import ResolvedTask, TaskScope


def _task(
    task_id: str,
    scope: TaskScope,
    *,
    depends_on: list[str] | None = None,
    calls: list[str] | None = None,
) -> ResolvedTask:
    def run(**kwargs):
        if calls is not None:
            calls.append(task_id)

    return ResolvedTask(
        id=task_id, name=task_id, run=run, depends_on=depends_on or [], scope=scope
    )


def _target(target_id: str, regions: list[str]) -> ExecutionTarget:
    return ExecutionTarget(
        id=target_id, name=target_id, type="resource", provider="fake", regions=regions
    )


def _planner_api():
    module = importlib.import_module("anvil.task_planner")
    planner = getattr(module, "plan_task_instances", None)
    error_type = getattr(module, "TaskPlanningError", None)
    assert callable(planner)
    assert isinstance(error_type, type)
    return planner, error_type


def _coordinates(instance) -> tuple[str, str]:
    return instance.key.execution_target_id, instance.key.region


def _dependency_coordinates(instance) -> list[tuple[str, str]]:
    return [
        (dependency.execution_target_id, dependency.region)
        for dependency in instance.dependencies
    ]


@pytest.mark.parametrize(
    ("producer_scope", "consumer_scope", "expected"),
    [
        (
            TaskScope.REGION,
            TaskScope.REGION,
            {
                ("target-b", "region-b2"): [("target-b", "region-b2")],
                ("target-b", "region-b1"): [("target-b", "region-b1")],
                ("target-a", "region-a1"): [("target-a", "region-a1")],
            },
        ),
        (
            TaskScope.TARGET,
            TaskScope.REGION,
            {
                ("target-b", "region-b2"): [("target-b", "region-b2")],
                ("target-b", "region-b1"): [("target-b", "region-b2")],
                ("target-a", "region-a1"): [("target-a", "region-a1")],
            },
        ),
        (
            TaskScope.CONFIGURED_TARGET,
            TaskScope.REGION,
            {
                ("target-b", "region-b2"): [("configured", "home-region")],
                ("target-b", "region-b1"): [("configured", "home-region")],
                ("target-a", "region-a1"): [("configured", "home-region")],
            },
        ),
        (
            TaskScope.REGION,
            TaskScope.TARGET,
            {
                ("target-b", "region-b2"): [
                    ("target-b", "region-b2"),
                    ("target-b", "region-b1"),
                ],
                ("target-a", "region-a1"): [("target-a", "region-a1")],
            },
        ),
        (
            TaskScope.TARGET,
            TaskScope.TARGET,
            {
                ("target-b", "region-b2"): [("target-b", "region-b2")],
                ("target-a", "region-a1"): [("target-a", "region-a1")],
            },
        ),
        (
            TaskScope.CONFIGURED_TARGET,
            TaskScope.TARGET,
            {
                ("target-b", "region-b2"): [("configured", "home-region")],
                ("target-a", "region-a1"): [("configured", "home-region")],
            },
        ),
        (
            TaskScope.REGION,
            TaskScope.CONFIGURED_TARGET,
            {
                ("configured", "home-region"): [
                    ("target-b", "region-b2"),
                    ("target-b", "region-b1"),
                    ("target-a", "region-a1"),
                ]
            },
        ),
        (
            TaskScope.TARGET,
            TaskScope.CONFIGURED_TARGET,
            {
                ("configured", "home-region"): [
                    ("target-b", "region-b2"),
                    ("target-a", "region-a1"),
                ]
            },
        ),
        (
            TaskScope.CONFIGURED_TARGET,
            TaskScope.CONFIGURED_TARGET,
            {("configured", "home-region"): [("configured", "home-region")]},
        ),
    ],
)
def test_scope_relationship_matrix(
    producer_scope: TaskScope,
    consumer_scope: TaskScope,
    expected: dict[tuple[str, str], list[tuple[str, str]]],
) -> None:
    planner, _error_type = _planner_api()
    plan = planner(
        tasks=[
            _task("producer", producer_scope),
            _task("consumer", consumer_scope, depends_on=["producer"]),
        ],
        execution_targets=[
            _target("target-b", ["region-b2", "region-b1"]),
            _target("target-a", ["region-a1"]),
        ],
        configured_target=_target("configured", ["home-region"]),
    )

    consumers = [
        instance for instance in plan.instances if instance.task.id == "consumer"
    ]

    assert {
        _coordinates(instance): _dependency_coordinates(instance)
        for instance in consumers
    } == expected


def test_planner_is_deterministic_and_never_executes_tasks() -> None:
    planner, _error_type = _planner_api()
    calls: list[str] = []
    tasks = [
        _task("inventory", TaskScope.REGION, calls=calls),
        _task(
            "summarize",
            TaskScope.CONFIGURED_TARGET,
            depends_on=["inventory"],
            calls=calls,
        ),
    ]
    targets = [
        _target("target-b", ["region-b2", "region-b1"]),
        _target("target-a", ["region-a1"]),
    ]
    configured_target = _target("configured", ["home-region"])

    first = planner(
        tasks=tasks, execution_targets=targets, configured_target=configured_target
    )
    second = planner(
        tasks=tasks, execution_targets=targets, configured_target=configured_target
    )

    assert first == second
    assert not calls
    assert [
        (instance.task.id, *_coordinates(instance)) for instance in first.instances
    ] == [
        ("inventory", "target-b", "region-b2"),
        ("inventory", "target-b", "region-b1"),
        ("inventory", "target-a", "region-a1"),
        ("summarize", "configured", "home-region"),
    ]


def test_planner_builds_stable_fan_out_adjacency() -> None:
    planner, _error_type = _planner_api()
    plan = planner(
        tasks=[
            _task("configured", TaskScope.CONFIGURED_TARGET),
            _task("regional", TaskScope.REGION, depends_on=["configured"]),
        ],
        execution_targets=[
            _target("target-b", ["region-b2", "region-b1"]),
            _target("target-a", ["region-a1"]),
        ],
        configured_target=_target("configured-owner", ["home-region"]),
    )
    producer = next(
        instance for instance in plan.instances if instance.task.id == "configured"
    )

    assert [
        (child.task_id, child.execution_target_id, child.region)
        for child in plan.adjacency[producer.key]
    ] == [
        ("regional", "target-b", "region-b2"),
        ("regional", "target-b", "region-b1"),
        ("regional", "target-a", "region-a1"),
    ]


@pytest.mark.parametrize(
    ("targets", "configured_target", "match"),
    [
        (
            [_target("duplicate", ["region-a"]), _target("duplicate", ["region-b"])],
            None,
            "duplicate execution target",
        ),
        ([_target("target", [])], None, "at least one region"),
        ([_target("target", ["region", "region"])], None, "duplicate region"),
    ],
)
def test_planner_rejects_invalid_execution_topology(
    targets: list[ExecutionTarget],
    configured_target: ExecutionTarget | None,
    match: str,
) -> None:
    planner, error_type = _planner_api()

    with pytest.raises(error_type, match=match):
        planner(
            tasks=[_task("regional", TaskScope.REGION)],
            execution_targets=targets,
            configured_target=configured_target,
        )


def test_planner_requires_concrete_configured_target_identity() -> None:
    planner, error_type = _planner_api()

    with pytest.raises(error_type, match="configured-target.*identity"):
        planner(
            tasks=[_task("configured", TaskScope.CONFIGURED_TARGET)],
            execution_targets=[_target("target", ["region"])],
            configured_target=None,
        )


def test_planner_rejects_unknown_dependency_ids_without_implicit_edges() -> None:
    planner, error_type = _planner_api()

    with pytest.raises(error_type, match="unknown task ID 'missing'"):
        planner(
            tasks=[_task("consumer", TaskScope.REGION, depends_on=["missing"])],
            execution_targets=[_target("target", ["region"])],
            configured_target=None,
        )
