"""
Return metadata for the current GCP project.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from anvil.actions import ActionRecorder

__LOGGER__ = logging.getLogger(__name__)


def _get_value(project: object, key: str) -> object:
    """Read a value from a Google SDK model or mapping."""

    if isinstance(project, Mapping):
        return project.get(key)
    return getattr(project, key, None)


def _get_text_value(project: object, key: str) -> str | None:
    """Read a text value from a Google SDK model or mapping."""

    value = _get_value(project, key)
    return value if isinstance(value, str) else None


def _get_state(project: object) -> str | None:
    """Return a JSON-serializable project lifecycle state when available."""

    value = _get_value(project, "state")
    if value is None:
        value = _get_value(project, "lifecycle_state")
    if isinstance(value, str):
        return value
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return name
    if isinstance(value, int):
        return str(value)
    return None


def _project_info(
    project: object, *, project_id: str, region: str
) -> dict[str, object]:
    """Build structured project result data."""

    result: dict[str, object] = {
        "project_id": _get_text_value(project, "project_id") or project_id,
        "region": region,
        "project_name": _get_text_value(project, "name"),
        "display_name": _get_text_value(project, "display_name"),
    }
    state = _get_state(project)
    parent = _get_text_value(project, "parent")
    if state is not None:
        result["state"] = state
    if parent is not None:
        result["parent"] = parent
    return result


def run(
    *,
    provider: str,
    execution_target_id: str,
    execution_target_name: str,
    execution_target_type: str,
    region: str,
    session,
    dry_run: bool,
    metadata: dict[str, object],
    dependency_data: dict[str, object],
    actions: ActionRecorder,
) -> dict[str, object]:
    """Return metadata for the current GCP project.

    This is a read-only GCP task. It fetches the current project from Google
    Cloud Resource Manager and ignores task metadata.

    Args:
        provider: Provider name for the current execution target.
        execution_target_id: Current GCP project ID.
        execution_target_name: Current GCP project display name or ID.
        execution_target_type: Provider target type.
        region: Current Anvil execution region value.
        session: GCP session scoped to the project and region.
        dry_run: Whether execution is running in dry-run mode.
        metadata: Arbitrary config metadata for the task.
        dependency_data: Runtime data selected from declared task dependencies.
        actions: Action recorder provided by the engine.

    Returns:
        Structured project metadata for the current GCP project.

    Raises:
        RuntimeError: If the task is used outside GCP project execution or if
            the Google Cloud Resource Manager SDK dependency is unavailable.
    """

    if provider != "gcp":
        raise RuntimeError("get_project_info requires the gcp provider")
    if execution_target_type != "project":
        raise RuntimeError("get_project_info requires a GCP project execution target")

    project_id = getattr(session, "project_id", execution_target_id)
    credentials = getattr(session, "credentials", None)
    if not isinstance(project_id, str) or not project_id.strip():
        raise RuntimeError("get_project_info requires a GCP project ID")
    if credentials is None:
        raise RuntimeError("get_project_info requires GCP session credentials")

    try:
        from google.cloud import resourcemanager_v3
    except ImportError as error:
        raise RuntimeError(
            "get_project_info requires optional dependency "
            "'google-cloud-resource-manager'. Install with 'anvil[gcp]'."
        ) from error

    normalized_project_id = project_id.strip()
    client = resourcemanager_v3.ProjectsClient(credentials=credentials)
    project = client.get_project(name=f"projects/{normalized_project_id}")
    result = _project_info(project, project_id=normalized_project_id, region=region)

    dry_run_suffix = " during dry-run" if dry_run else ""
    __LOGGER__.info(
        f"Read GCP project metadata for {execution_target_name} "
        f"({normalized_project_id}) region={region}{dry_run_suffix}; "
        "no mutations are performed by this read-only task"
    )
    actions.record(
        f"Read GCP project metadata for project {normalized_project_id} region {region}"
    )

    return result
