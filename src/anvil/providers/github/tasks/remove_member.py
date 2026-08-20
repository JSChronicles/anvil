"""Remove members from the current GitHub organization."""

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
    """Remove one or more GitHub organization members by login.

    Args:
        metadata: Requires ``members`` as a non-empty login array.
        dry_run: Report planned removals without calling GitHub when true.

    Returns:
        Per-member statuses and summary counts.
    """
    organization = organization_for_task(
        task_name="remove_member",
        provider=provider,
        execution_target_type=execution_target_type,
        session=session,
    )
    selectors = metadata_string_array(
        task_name="remove_member", metadata=metadata, key="members", required=True
    )
    assert selectors is not None
    get_members = getattr(organization, "get_members", None)
    remove = getattr(organization, "remove_from_members", None)
    if not callable(get_members) or not callable(remove):
        raise RuntimeError(
            "remove_member requires GitHub organization membership operations"
        )
    available = {
        str(getattr(item, "login", "")).lower(): item for item in get_members()
    }
    results: list[dict[str, object]] = []
    for login in selectors:
        member = available.get(login.lower())
        if member is None:
            results.append({"id": login, "status": "not_found"})
            continue
        if dry_run:
            status = "planned"
            message = f"(dry-run) Would remove GitHub member {login} from {execution_target_id}"
        else:
            try:
                remove(member)
            except Exception as error:
                results.append(
                    {"id": login, "status": "failed", "error": type(error).__name__}
                )
                continue
            status = "removed"
            message = f"Removed GitHub member {login} from {execution_target_id}"
        __LOGGER__.info(message)
        actions.record(message)
        results.append({"id": login, "status": status})
    failed = sum(item["status"] == "failed" for item in results)
    result = {
        "requested_count": len(selectors),
        "planned_count": sum(item["status"] == "planned" for item in results),
        "removed_count": sum(item["status"] == "removed" for item in results),
        "failed_count": failed,
        "members": results,
    }
    if failed:
        raise TaskExecutionError(
            "remove_member failed for one or more GitHub members", partial_result=result
        )
    return result
