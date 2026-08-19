"""Search repository blobs in the current GitLab project."""

import logging
from anvil.actions import ActionRecorder
from anvil.providers.gitlab.tasks._api import project_for_task
from anvil.providers.tasks._task_helpers import bounded, metadata_int, metadata_string

__LOGGER__ = logging.getLogger(__name__)


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
    """Search GitLab project code.

    Args:
        metadata: Requires ``query``; optional ``max_results`` bounds matches.
        session: GitLab session scoped to the current project.

    Returns:
        Match count, query, and JSON-serializable matches.
    """
    query = metadata_string(
        task_name="search_code", metadata=metadata, key="query", required=True
    )
    assert query is not None
    project = project_for_task(
        task_name="search_code",
        provider=provider,
        execution_target_id=execution_target_id,
        execution_target_type=execution_target_type,
        session=session,
    )
    maximum = metadata_int(metadata=metadata, key="max_results")
    matches = bounded(
        project.search("blobs", query, iterator=True), max_results=maximum
    )
    __LOGGER__.info(
        f"Found {len(matches)} GitLab code match(es) for {execution_target_name}"
    )
    actions.record(f"Searched GitLab code for {execution_target_id} region {region}")
    return {"query": query, "match_count": len(matches), "matches": matches}
