"""List members of one or more GitHub organization teams."""

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
    """List members of selected GitHub organization teams.

    Args:
        metadata: Requires a non-empty ``teams`` slug/ID array. An optional
            ``members`` login array filters the results, and ``max_results``
            bounds the combined result count.
        session: GitHub session scoped to an organization target.

    Returns:
        Matching team memberships and unmatched team/member selectors.
    """
    organization = organization_for_task(
        task_name="list_team_member",
        provider=provider,
        execution_target_type=execution_target_type,
        session=session,
    )
    team_selectors = metadata_string_array(
        task_name="list_team_member", metadata=metadata, key="teams", required=True
    )
    member_selectors = metadata_string_array(
        task_name="list_team_member", metadata=metadata, key="members"
    )
    assert team_selectors is not None
    get_teams = getattr(organization, "get_teams", None)
    if not callable(get_teams):
        raise RuntimeError("list_team_member requires GitHub organization.get_teams()")
    available = {
        str(value).lower(): team
        for team in get_teams()
        for value in (getattr(team, "id", None), getattr(team, "slug", None))
        if value is not None
    }
    maximum = metadata_int(
        task_name="list_team_member", metadata=metadata, key="max_results"
    )
    selected_members = (
        {item.lower() for item in member_selectors}
        if member_selectors is not None
        else None
    )
    memberships: list[dict[str, object]] = []
    unmatched_teams: list[str] = []
    matched_member_logins: set[str] = set()
    for selector in team_selectors:
        team = available.get(selector.lower())
        if team is None:
            unmatched_teams.append(selector)
            continue
        get_members = getattr(team, "get_members", None)
        if not callable(get_members):
            raise RuntimeError("list_team_member requires GitHub team.get_members()")
        team_identity = github_identity(team)
        for member in get_members():
            identity = github_identity(member)
            login = str(identity.get("login", "")).lower()
            if selected_members is not None and login not in selected_members:
                continue
            matched_member_logins.add(login)
            memberships.append({"team": team_identity, "member": identity})
            if len(memberships) >= maximum:
                break
        if len(memberships) >= maximum:
            break
    unmatched_members = (
        [item for item in member_selectors if item.lower() not in matched_member_logins]
        if member_selectors is not None
        else []
    )
    message = (
        f"Listed {len(memberships)} GitHub team membership(s) for "
        f"{execution_target_name}"
    )
    __LOGGER__.info(message)
    actions.record(
        f"Listed {len(memberships)} GitHub team membership(s) for "
        f"{execution_target_id} region {region}"
    )
    return {
        "membership_count": len(memberships),
        "memberships": memberships,
        "unmatched_teams": unmatched_teams,
        "unmatched_members": unmatched_members,
    }
