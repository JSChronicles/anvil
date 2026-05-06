from __future__ import annotations

import logging

from botocore.exceptions import BotoCoreError, ClientError

from anvil.actions import ActionRecorder

__LOGGER__ = logging.getLogger(__name__)

NEUTRAL_SCORE = 50
OWNER_TAG_KEYS = (
    "Owner",
    "TechnicalOwner",
    "BusinessOwner",
    "Application",
    "App",
    "CostCenter",
    "Purpose",
)


def get_owner_signal(session, account_id: str) -> dict[str, object]:
    """Use AWS Organizations tags to determine whether ownership metadata exists."""
    try:
        org_client = session.client("organizations")
        paginator = org_client.get_paginator("list_tags_for_resource")
        tags: list[dict[str, str]] = []

        for page in paginator.paginate(ResourceId=account_id):
            tags.extend(page.get("Tags", []))
    except (BotoCoreError, ClientError) as error:
        warning = f"Organizations account tags unavailable for {account_id}: {error}"
        __LOGGER__.warning(warning)
        return {
            "owner_known": None,
            "owner_tags_found": [],
            "owner_score": NEUTRAL_SCORE,
            "warnings": [warning],
        }

    owner_tags_found = [
        {"key": tag["Key"], "value": tag["Value"]}
        for tag in tags
        if tag.get("Key") in OWNER_TAG_KEYS and str(tag.get("Value", "")).strip()
    ]
    owner_known = bool(owner_tags_found)

    return {
        "owner_known": owner_known,
        "owner_tags_found": owner_tags_found,
        "owner_score": score_owner(owner_known),
        "warnings": [],
    }


def score_owner(owner_known: bool | None) -> int:
    """Score ownership metadata completeness."""
    if owner_known is None:
        return NEUTRAL_SCORE
    if owner_known:
        return 0
    return 100


def run(
    *,
    account_id: str,
    account_alias: str,
    session,
    dry_run: bool,
    metadata: dict[str, object],
    actions: ActionRecorder,
) -> dict[str, object]:
    """Collect only the inactive-account Organizations owner tag signal."""
    region_name = session.region_name
    signal = get_owner_signal(session, account_id)
    warnings = signal.pop("warnings", [])

    actions.record(f"Collected inactive-account owner signal for {account_id}")
    __LOGGER__.info(
        f"Collected inactive-account owner signal for {account_alias} ({account_id}), "
        f"region={region_name}, dry_run={dry_run}"
    )

    result: dict[str, object] = {
        "record_type": "inactive_account_owner_signal",
        "account_id": account_id,
        "account_alias": account_alias,
        "region": region_name,
        "signals": signal,
    }
    if warnings:
        result["warnings"] = warnings

    return result
