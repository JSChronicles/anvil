import json

from anvil.descriptors import ConfigBranch
from anvil.task_loader import resolve_tasks


def render_graph(*, targets, output_json: bool) -> None:
    for target in targets:
        execution = resolve_tasks(task_specs=target.tasks)

        if output_json:
            _render_json(
                target_name=target.name,
                config_branch=target.config_branch,
                execution=execution,
            )
        else:
            _render_tree(target.name, execution)


def _render_tree(target_name: str, execution) -> None:
    print(f"Execution Graph ({target_name})")
    print("-" * (18 + len(target_name)))

    adjacency_map = execution.adjacency

    child_nodes: set[str] = set()
    for _, child_list in adjacency_map.items():
        for child_task in child_list:
            child_nodes.add(child_task)

    root_tasks: list[str] = [
        resolved_task.name
        for resolved_task in execution.ordered
        if resolved_task.name not in child_nodes
    ]

    for root_task in root_tasks:
        _print_node_recursive(
            task_name=root_task, adjacency_map=adjacency_map, prefix=""
        )


def _print_node_recursive(
    *, task_name: str, adjacency_map: dict[str, list[str]], prefix: str
) -> None:
    print(f"{prefix}{task_name}")

    child_tasks = adjacency_map.get(task_name, [])

    for index, child_task in enumerate(child_tasks):
        is_last_child = index == len(child_tasks) - 1

        branch_symbol = "└── " if is_last_child else "├── "
        next_prefix = prefix + ("    " if is_last_child else "│   ")

        print(f"{prefix}{branch_symbol}", end="")

        _print_node_recursive(
            task_name=child_task, adjacency_map=adjacency_map, prefix=next_prefix
        )


def _render_json(*, target_name: str, config_branch: ConfigBranch, execution) -> None:
    target_key = (
        "account_group"
        if config_branch is ConfigBranch.ACCOUNTS
        else "organization"
    )

    payload = {
        target_key: target_name,
        "tasks": [
            {"name": resolved_task.name, "depends_on": resolved_task.depends_on}
            for resolved_task in execution.ordered
        ],
    }

    print(json.dumps(payload, indent=2))
