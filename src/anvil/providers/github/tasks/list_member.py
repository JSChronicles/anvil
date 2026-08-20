"""List members of the current GitHub organization."""

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
    """List GitHub organization members, optionally restricted by login.

    Args:
        metadata: Optional ``members`` login array and ``max_results``.
        session: GitHub session scoped to an organization target.

    Returns:
        Matching members and unmatched logins.
    """
    organization = organization_for_task(
        task_name="list_member",
        provider=provider,
        execution_target_type=execution_target_type,
        session=session,
    )
    operation = getattr(organization, "get_members", None)
    if not callable(operation):
        raise RuntimeError("list_member requires GitHub organization.get_members()")
    maximum = metadata_int(metadata=metadata, key="max_results")
    members = [
        github_identity(item)
        for index, item in enumerate(operation())
        if index < maximum
    ]
    selectors = metadata_string_array(
        task_name="list_member", metadata=metadata, key="members"
    )
    if selectors is None:
        matched, unmatched = members, []
    else:
        by_login = {
            str(item["login"]).lower(): item
            for item in members
            if item.get("login") is not None
        }
        matched = [
            by_login[item.lower()] for item in selectors if item.lower() in by_login
        ]
        unmatched = [item for item in selectors if item.lower() not in by_login]
    __LOGGER__.info(
        f"Listed {len(matched)} GitHub member(s) for {execution_target_name}"
    )
    actions.record(
        f"Listed {len(matched)} GitHub member(s) for {execution_target_id} region {region}"
    )
    return {
        "member_count": len(matched),
        "members": matched,
        "unmatched_members": unmatched,
    }
