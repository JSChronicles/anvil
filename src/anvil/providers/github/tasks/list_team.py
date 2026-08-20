"""List teams in the current GitHub organization."""

import logging
from anvil.actions import ActionRecorder
from anvil.providers.github.tasks._organization import (
    github_identity,
    organization_for_task,
)
from anvil.providers.tasks._task_helpers import metadata_int, metadata_string_array

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
    """List GitHub organization teams, optionally restricted by slug or ID.

    Args:
        metadata: Optional ``teams`` array and ``max_results``.
        session: GitHub session scoped to an organization target.

    Returns:
        Matching teams and unmatched selectors.
    """
    organization = organization_for_task(
        task_name="list_team",
        provider=provider,
        execution_target_type=execution_target_type,
        session=session,
    )
    operation = getattr(organization, "get_teams", None)
    if not callable(operation):
        raise RuntimeError("list_team requires GitHub organization.get_teams()")
    maximum = metadata_int(metadata=metadata, key="max_results")
    teams = [
        github_identity(item)
        for index, item in enumerate(operation())
        if index < maximum
    ]
    selectors = metadata_string_array(
        task_name="list_team", metadata=metadata, key="teams"
    )
    if selectors is None:
        matched, unmatched = teams, []
    else:
        index = {
            str(value).lower(): team
            for team in teams
            for value in (team.get("id"), team.get("slug"))
            if value is not None
        }
        matched = [index[item.lower()] for item in selectors if item.lower() in index]
        unmatched = [item for item in selectors if item.lower() not in index]
    actions.record(
        f"Listed {len(matched)} GitHub team(s) for {execution_target_id} region {region}"
    )
    __LOGGER__.info(f"Listed {len(matched)} GitHub team(s) for {execution_target_name}")
    return {"team_count": len(matched), "teams": matched, "unmatched_teams": unmatched}
