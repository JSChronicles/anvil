"""Remove teams from the current GitHub organization."""

import logging
from anvil.actions import ActionRecorder
from anvil.providers.github.tasks._organization import organization_for_task
from anvil.providers.tasks._task_helpers import metadata_string_array
from anvil.task_errors import TaskExecutionError

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
    """Delete one or more GitHub organization teams by slug or ID.

    Args:
        metadata: Requires ``teams`` as a non-empty slug/ID array.
        dry_run: Report planned deletions without calling GitHub when true.

    Returns:
        Per-team statuses and summary counts.
    """
    organization = organization_for_task(
        task_name="remove_team",
        provider=provider,
        execution_target_type=execution_target_type,
        session=session,
    )
    selectors = metadata_string_array(
        task_name="remove_team", metadata=metadata, key="teams", required=True
    )
    assert selectors is not None
    get_teams = getattr(organization, "get_teams", None)
    if not callable(get_teams):
        raise RuntimeError("remove_team requires GitHub organization.get_teams()")
    available = {
        str(value).lower(): team
        for team in get_teams()
        for value in (getattr(team, "id", None), getattr(team, "slug", None))
        if value is not None
    }
    results: list[dict[str, object]] = []
    for selector in selectors:
        team = available.get(selector.lower())
        if team is None:
            results.append({"id": selector, "status": "not_found"})
            continue
        if dry_run:
            status = "planned"
            message = f"(dry-run) Would remove GitHub team {selector} from {execution_target_id}"
        else:
            delete = getattr(team, "delete", None)
            if not callable(delete):
                raise RuntimeError("remove_team requires GitHub team.delete()")
            try:
                delete()
            except Exception as error:
                results.append(
                    {"id": selector, "status": "failed", "error": type(error).__name__}
                )
                continue
            status = "removed"
            message = f"Removed GitHub team {selector} from {execution_target_id}"
        __LOGGER__.info(message)
        actions.record(message)
        results.append({"id": selector, "status": status})
    failed = sum(item["status"] == "failed" for item in results)
    result = {
        "requested_count": len(selectors),
        "planned_count": sum(item["status"] == "planned" for item in results),
        "removed_count": sum(item["status"] == "removed" for item in results),
        "failed_count": failed,
        "teams": results,
    }
    if failed:
        raise TaskExecutionError(
            "remove_team failed for one or more GitHub teams", partial_result=result
        )
    return result
