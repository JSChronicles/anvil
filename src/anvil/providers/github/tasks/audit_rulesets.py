"""
Audit repository rulesets for the current GitHub repository target.
"""

from __future__ import annotations

import logging

from anvil.actions import ActionRecorder
from anvil.providers.github.tasks._rest import (
    DEFAULT_MAX_RESULTS,
    list_rest_items,
    metadata_bool,
    metadata_int,
    require_github_provider,
    require_repository_target,
)

__LOGGER__ = logging.getLogger(__name__)

TASK_NAME = "audit_rulesets"


def run(
    *,
    provider: str,
    execution_target_id: str,
    execution_target_name: str,
    execution_target_type: str,
    region: str,
    location: str,
    session,
    dry_run: bool,
    metadata: dict[str, object],
    actions: ActionRecorder,
) -> dict[str, object]:
    """List repository rulesets for a GitHub repository target."""

    require_github_provider(task_name=TASK_NAME, provider=provider)
    owner, repo = require_repository_target(
        task_name=TASK_NAME,
        execution_target_id=execution_target_id,
        execution_target_type=execution_target_type,
    )
    max_results = metadata_int(
        task_name=TASK_NAME,
        metadata=metadata,
        key="max_results",
        default=DEFAULT_MAX_RESULTS,
    )
    includes_parents = metadata_bool(
        task_name=TASK_NAME, metadata=metadata, key="includes_parents", default=True
    )
    params = {"includes_parents": includes_parents}
    rulesets = list_rest_items(
        session=session,
        path=f"/repos/{owner}/{repo}/rulesets",
        params=params,
        max_results=max_results,
    )

    __LOGGER__.info(
        f"Audited {len(rulesets)} GitHub ruleset(s) for repository "
        f"{execution_target_name} location={location or region}"
    )
    actions.record(
        f"Audited {len(rulesets)} GitHub ruleset(s) for repository "
        f"{execution_target_id} location {location or region}"
    )
    return {
        "ruleset_count": len(rulesets),
        "rulesets": rulesets,
        "includes_parents": includes_parents,
    }
