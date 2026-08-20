"""Remove members from one or more GitHub organization teams."""

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
    """Remove selected members from selected GitHub organization teams.

    Args:
        metadata: Requires non-empty ``teams`` slug/ID and ``members`` login
            arrays.
        dry_run: Report planned removals without calling GitHub when true.

    Returns:
        Per-team/member statuses and summary counts.
    """
    organization = organization_for_task(
        task_name="remove_team_member",
        provider=provider,
        execution_target_type=execution_target_type,
        session=session,
    )
    team_selectors = metadata_string_array(
        task_name="remove_team_member", metadata=metadata, key="teams", required=True
    )
    member_selectors = metadata_string_array(
        task_name="remove_team_member", metadata=metadata, key="members", required=True
    )
    assert team_selectors is not None and member_selectors is not None
    get_teams = getattr(organization, "get_teams", None)
    if not callable(get_teams):
        raise RuntimeError(
            "remove_team_member requires GitHub organization.get_teams()"
        )
    available = {
        str(value).lower(): team
        for team in get_teams()
        for value in (getattr(team, "id", None), getattr(team, "slug", None))
        if value is not None
    }
    results: list[dict[str, object]] = []
    for team_selector in team_selectors:
        team = available.get(team_selector.lower())
        if team is None:
            results.extend(
                {"team": team_selector, "member": login, "status": "team_not_found"}
                for login in member_selectors
            )
            continue
        get_members = getattr(team, "get_members", None)
        if not callable(get_members):
            raise RuntimeError("remove_team_member requires GitHub team.get_members()")
        members = {
            str(getattr(member, "login", "")).lower(): member
            for member in get_members()
        }
        for login in member_selectors:
            member = members.get(login.lower())
            if member is None:
                results.append(
                    {
                        "team": team_selector,
                        "member": login,
                        "status": "member_not_found",
                    }
                )
                continue
            if dry_run:
                status = "planned"
                message = (
                    f"(dry-run) Would remove GitHub member {login} from team "
                    f"{team_selector} in {execution_target_id}"
                )
            else:
                remove = getattr(team, "remove_membership", None)
                if not callable(remove):
                    raise RuntimeError(
                        "remove_team_member requires GitHub team.remove_membership()"
                    )
                try:
                    remove(member)
                except Exception as error:
                    results.append(
                        {
                            "team": team_selector,
                            "member": login,
                            "status": "failed",
                            "error": type(error).__name__,
                        }
                    )
                    continue
                status = "removed"
                message = (
                    f"Removed GitHub member {login} from team {team_selector} "
                    f"in {execution_target_id}"
                )
            __LOGGER__.info(message)
            actions.record(message)
            results.append({"team": team_selector, "member": login, "status": status})
    failed = sum(item["status"] == "failed" for item in results)
    result = {
        "requested_count": len(team_selectors) * len(member_selectors),
        "planned_count": sum(item["status"] == "planned" for item in results),
        "removed_count": sum(item["status"] == "removed" for item in results),
        "failed_count": failed,
        "memberships": results,
    }
    if failed:
        raise TaskExecutionError(
            "remove_team_member failed for one or more GitHub memberships",
            partial_result=result,
        )
    return result
