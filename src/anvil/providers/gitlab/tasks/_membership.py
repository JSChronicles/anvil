"""Shared GitLab membership task helpers."""

from anvil.providers.tasks._task_helpers import bounded, metadata_int, require_provider


def resource_for_task(
    *,
    task_name: str,
    provider: str,
    execution_target_id: str,
    execution_target_type: str,
    session,
):
    """Return the current GitLab group or project resource."""

    require_provider(task_name=task_name, provider=provider, expected="gitlab")
    manager_name = {"group": "groups", "project": "projects"}.get(execution_target_type)
    if manager_name is None:
        raise RuntimeError(f"{task_name} requires a GitLab group or project target")
    manager = getattr(session.client, manager_name, None)
    operation = getattr(manager, "get", None)
    if not callable(operation):
        raise RuntimeError(f"{task_name} requires python-gitlab {manager_name}.get()")
    return operation(int(execution_target_id))


def member_manager(resource: object) -> object:
    """Return the inherited-aware member manager when available."""

    return getattr(resource, "members_all", None) or getattr(resource, "members", None)


def list_members(*, resource: object, metadata: dict[str, object]) -> list[object]:
    """List bounded GitLab group or project membership records."""

    manager = member_manager(resource)
    operation = getattr(manager, "list", None)
    if not callable(operation):
        raise RuntimeError("GitLab resource does not expose a member list operation")
    return bounded(
        operation(iterator=True),
        max_results=metadata_int(metadata=metadata, key="max_results"),
    )
