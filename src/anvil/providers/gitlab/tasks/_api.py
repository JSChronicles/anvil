"""Shared python-gitlab task helpers."""

from anvil.providers.tasks._task_helpers import (
    bounded,
    metadata_int,
    require_provider,
    require_target_type,
)


def project_for_task(
    *,
    task_name: str,
    provider: str,
    execution_target_id: str,
    execution_target_type: str,
    session,
):
    """Return the current concrete GitLab project."""
    require_provider(task_name=task_name, provider=provider, expected="gitlab")
    require_target_type(
        task_name=task_name,
        execution_target_type=execution_target_type,
        expected="project",
    )
    session_get_project = getattr(session, "get_project", None)
    if callable(session_get_project):
        return session_get_project()

    # Preserve compatibility with lightweight third-party and test sessions.
    projects = getattr(session.client, "projects", None)
    get_project = getattr(projects, "get", None)
    if not callable(get_project):
        raise RuntimeError(f"{task_name} requires python-gitlab projects.get()")
    return get_project(int(execution_target_id))


def list_manager(
    *, manager: object, metadata: dict[str, object], **parameters: object
) -> list[object]:
    """List a bounded python-gitlab manager collection."""
    operation = getattr(manager, "list", None)
    if not callable(operation):
        raise RuntimeError("python-gitlab manager does not expose list()")
    maximum = metadata_int(metadata=metadata, key="max_results")
    return bounded(operation(iterator=True, **parameters), max_results=maximum)


def list_vulnerability_alerts(
    *,
    task_name: str,
    report_type: str,
    provider: str,
    execution_target_id: str,
    execution_target_type: str,
    session,
    metadata: dict[str, object],
) -> list[object]:
    """List one GitLab vulnerability report type for GitHub-style alert tasks."""
    project = project_for_task(
        task_name=task_name,
        provider=provider,
        execution_target_id=execution_target_id,
        execution_target_type=execution_target_type,
        session=session,
    )
    parameters: dict[str, object] = {"report_type": report_type}
    for key in ("state", "severity"):
        value = metadata.get(key)
        if value is not None:
            if not isinstance(value, str) or not value.strip():
                raise RuntimeError(
                    f"{task_name} expects metadata.{key} to be a non-empty string"
                )
            parameters[key] = value.strip()
    return list_manager(
        manager=getattr(project, "vulnerabilities", None),
        metadata=metadata,
        **parameters,
    )
