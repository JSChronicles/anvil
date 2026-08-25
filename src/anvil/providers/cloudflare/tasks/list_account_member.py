"""List members of the current Cloudflare account."""

import logging

from anvil.actions import ActionRecorder
from anvil.providers.tasks._task_helpers import (
    bounded,
    mapping_identifier,
    metadata_int,
    metadata_string_array,
    require_provider,
    require_target_type,
)

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
    """List Cloudflare account members, optionally restricted by member IDs.

    Args:
        metadata: Optional ``members`` array and ``max_results`` limit.
        session: Cloudflare session scoped to the current account.

    Returns:
        Matching member records and unmatched member IDs.
    """

    require_provider(
        task_name="list_account_member", provider=provider, expected="cloudflare"
    )
    require_target_type(
        task_name="list_account_member",
        execution_target_type=execution_target_type,
        expected="account",
    )
    selectors = metadata_string_array(
        task_name="list_account_member", metadata=metadata, key="members"
    )
    maximum = metadata_int(metadata=metadata, key="max_results")
    accounts = getattr(session.client, "accounts", None)
    members_resource = getattr(accounts, "members", None)
    operation = getattr(members_resource, "list", None)
    if not callable(operation):
        raise RuntimeError(
            "list_account_member requires Cloudflare accounts.members.list()"
        )
    members = bounded(
        operation(account_id=execution_target_id, per_page=50), max_results=maximum
    )
    if selectors is None:
        matched = members
        unmatched: list[str] = []
    else:
        by_id = {
            identifier: member
            for member in members
            if (identifier := mapping_identifier(member)) is not None
        }
        matched = [by_id[item] for item in selectors if item in by_id]
        unmatched = [item for item in selectors if item not in by_id]
    __LOGGER__.info(
        f"Listed {len(matched)} Cloudflare account member(s) for {execution_target_name}"
    )
    actions.record(
        f"Listed {len(matched)} Cloudflare account member(s) for "
        f"{execution_target_id} region {region}"
    )
    return {
        "member_count": len(matched),
        "members": matched,
        "unmatched_members": unmatched,
    }
