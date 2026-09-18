"""Combine inactive-account task results into an organization report."""

from __future__ import annotations

import logging

from anvil.actions import ActionRecorder
from anvil.providers.aws.tasks.inactive_account_resource_signal import score_resources

__LOGGER__ = logging.getLogger(__name__)
TASK_SCOPE = "configured_target"
NEUTRAL_SCORE = 50
SCORE_WEIGHTS = {
    "cost_score": 25,
    "activity_score": 30,
    "iam_usage_score": 30,
    "resource_score": 15,
}


def _mapping(value: object, dependency_name: str) -> dict[str, object]:
    """Validate a dependency mapping."""
    if not isinstance(value, dict):
        raise RuntimeError(
            f"inactive_account_report requires dependency_data.{dependency_name}"
        )
    return value


def _accounts(value: object) -> list[dict[str, object]]:
    """Validate account context dependency data."""
    if not isinstance(value, list):
        raise RuntimeError("inactive_account_report requires dependency_data.accounts")
    accounts: list[dict[str, object]] = []
    for account in value:
        if not isinstance(account, dict) or not isinstance(
            account.get("account_id"), str
        ):
            raise RuntimeError(
                "inactive_account_report dependency_data.accounts must contain "
                "account mappings with account_id"
            )
        accounts.append(account)
    return accounts


def _regional_results(value: object) -> list[dict[str, object]]:
    """Validate regional resource results from fan-in dependency data."""
    values = value if isinstance(value, list) else [value]
    if not all(isinstance(item, dict) for item in values):
        raise RuntimeError(
            "inactive_account_report dependency_data.regional_resources must contain "
            "regional result mappings"
        )
    return values


def calculate_final_score(scores: dict[str, int]) -> int:
    """Calculate the weighted inactivity score."""
    weighted = sum(scores[key] * weight for key, weight in SCORE_WEIGHTS.items())
    return round(weighted / sum(SCORE_WEIGHTS.values()))


def determine_status(final_score: int) -> str:
    """Map a final score to an account status."""
    if final_score >= 75:
        return "LIKELY_INACTIVE"
    if final_score >= 45:
        return "POSSIBLY_INACTIVE"
    return "ACTIVE"


def _aggregate_resources(
    account_id: str, regional_results: list[dict[str, object]]
) -> dict[str, object]:
    """Combine all regional resource signals for one AWS account."""
    selected = [
        result for result in regional_results if result.get("account_id") == account_id
    ]
    if not selected:
        warning = f"No regional resource results were received for account {account_id}"
        return {
            "regions_scanned": [],
            "resource_count": 0,
            "resource_counts": {},
            "resource_score": NEUTRAL_SCORE,
            "resource_count_complete": False,
            "resource_ownership": {
                "owner_counts": {},
                "recognized_owner_resource_count": 0,
                "resources_without_recognized_owner_count": 0,
                "owned_resources": [],
            },
            "warnings": [warning],
        }

    counts: dict[str, int] = {}
    owner_counts: dict[str, int] = {}
    owned_resources: list[dict[str, object]] = []
    recognized_owner_count = 0
    without_owner_count = 0
    resource_count_complete = True
    warnings: list[str] = []
    for result in selected:
        if result.get("resource_count_complete") is not True:
            resource_count_complete = False
        raw_counts = result.get("resource_counts", {})
        if isinstance(raw_counts, dict):
            for name, value in raw_counts.items():
                if isinstance(name, str) and isinstance(value, int):
                    counts[name] = counts.get(name, 0) + value
        raw_ownership = result.get("resource_ownership", {})
        if isinstance(raw_ownership, dict):
            raw_owner_counts = raw_ownership.get("owner_counts", {})
            if isinstance(raw_owner_counts, dict):
                for owner, value in raw_owner_counts.items():
                    if isinstance(owner, str) and isinstance(value, int):
                        owner_counts[owner] = owner_counts.get(owner, 0) + value
            raw_resources = raw_ownership.get("owned_resources", [])
            if isinstance(raw_resources, list):
                owned_resources.extend(
                    resource for resource in raw_resources if isinstance(resource, dict)
                )
            recognized = raw_ownership.get("recognized_owner_resource_count", 0)
            without_owner = raw_ownership.get(
                "resources_without_recognized_owner_count", 0
            )
            if isinstance(recognized, int):
                recognized_owner_count += recognized
            if isinstance(without_owner, int):
                without_owner_count += without_owner
        raw_warnings = result.get("warnings", [])
        if isinstance(raw_warnings, list):
            warnings.extend(str(warning) for warning in raw_warnings)

    total = sum(counts.values())
    return {
        "regions_scanned": sorted(
            str(result["region"]) for result in selected if "region" in result
        ),
        "resource_count": total,
        "resource_counts": dict(sorted(counts.items())),
        "resource_score": score_resources(
            total, [] if resource_count_complete else ["resource inventory incomplete"]
        ),
        "resource_count_complete": resource_count_complete,
        "resource_ownership": {
            "owner_counts": dict(sorted(owner_counts.items())),
            "recognized_owner_resource_count": recognized_owner_count,
            "resources_without_recognized_owner_count": without_owner_count,
            "owned_resources": sorted(
                owned_resources,
                key=lambda resource: str(resource.get("resource_arn", "")),
            ),
        },
        "warnings": warnings,
    }


def _signal(
    signals_by_account: dict[str, object], account_id: str, score_keys: tuple[str, ...]
) -> dict[str, object]:
    """Return a validated account signal or a neutral missing-data signal."""
    value = signals_by_account.get(account_id)
    if isinstance(value, dict):
        return value
    warning = f"No signal result was received for account {account_id}"
    return {**{key: NEUTRAL_SCORE for key in score_keys}, "warnings": [warning]}


def _score(signal: dict[str, object], key: str) -> int:
    """Return a validated score value from a producer signal."""
    value = signal.get(key, NEUTRAL_SCORE)
    if not isinstance(value, int):
        raise RuntimeError(f"inactive_account_report received a non-integer {key}")
    return value


def _warnings(signal: dict[str, object]) -> list[str]:
    """Return validated warning strings from a producer signal."""
    value = signal.get("warnings", [])
    if not isinstance(value, list):
        raise RuntimeError("inactive_account_report received non-list warnings")
    return [str(warning) for warning in value]


def _recommendation(
    status: str, warnings: list[str], resource_owner_counts: dict[str, int]
) -> str:
    """Build an actionable recommendation without scoring ownership tags."""
    if status == "ACTIVE":
        recommendation = "No action recommended."
    elif status == "POSSIBLY_INACTIVE":
        recommendation = "Review activity and resource purpose before taking action."
    else:
        recommendation = "Candidate for review and possible quarantine."
    owners = sorted(resource_owner_counts)
    if owners and status != "ACTIVE":
        recommendation += f" Confirm with resource owner(s): {', '.join(owners)}."
    if warnings:
        recommendation += " Telemetry is incomplete; verify manually."
    return recommendation


def build_report(
    *,
    accounts: list[dict[str, object]],
    costs: dict[str, object],
    activity: dict[str, object],
    regional_resources: list[dict[str, object]],
) -> dict[str, object]:
    """Build account assessments and organization-level summary counts."""
    assessments: list[dict[str, object]] = []
    status_counts = {"ACTIVE": 0, "POSSIBLY_INACTIVE": 0, "LIKELY_INACTIVE": 0}
    for account in accounts:
        account_id = str(account["account_id"])
        cost = _signal(costs, account_id, ("cost_score",))
        cloudtrail = _signal(
            activity, account_id, ("activity_score", "iam_usage_score")
        )
        resources = _aggregate_resources(account_id, regional_resources)
        scores = {
            "cost_score": _score(cost, "cost_score"),
            "activity_score": _score(cloudtrail, "activity_score"),
            "iam_usage_score": _score(cloudtrail, "iam_usage_score"),
            "resource_score": _score(resources, "resource_score"),
        }
        final_score = calculate_final_score(scores)
        status = determine_status(final_score)
        status_counts[status] += 1
        warnings = _warnings(account) + [
            warning
            for signal in (cost, cloudtrail, resources)
            for warning in _warnings(signal)
        ]
        ownership = resources["resource_ownership"]
        if not isinstance(ownership, dict):
            raise RuntimeError(
                "inactive_account_report received invalid resource ownership data"
            )
        owner_counts = ownership["owner_counts"]
        if not isinstance(owner_counts, dict) or not all(
            isinstance(owner, str) and isinstance(count, int)
            for owner, count in owner_counts.items()
        ):
            raise RuntimeError(
                "inactive_account_report received invalid resource owner counts"
            )
        assessments.append(
            {
                "account_id": account_id,
                "account_name": account.get("account_name", account_id),
                "status": status,
                "final_score": final_score,
                "scores": scores,
                "signals": {
                    "cost": {
                        key: value for key, value in cost.items() if key != "warnings"
                    },
                    "activity": {
                        key: value
                        for key, value in cloudtrail.items()
                        if key != "warnings"
                    },
                    "resources": {
                        key: value
                        for key, value in resources.items()
                        if key not in {"warnings", "resource_ownership"}
                    },
                },
                "ownership_context": {
                    "account_ownership_tags": account.get("ownership_tags", {}),
                    "account_tags": account.get("tags", {}),
                    "resource_ownership": ownership,
                },
                "recommendation": _recommendation(status, warnings, owner_counts),
                "warnings": warnings,
            }
        )
    return {
        "record_type": "inactive_account_report",
        "summary": {"account_count": len(assessments), **status_counts},
        "accounts": assessments,
    }


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
    """Build the final inactive-account report from upstream task results.

    This configured-target task performs no AWS API calls. It requires account
    context, Cost Explorer signals, CloudTrail signals, and fanned-in regional
    resource signals in ``dependency_data``. Ownership tags are report context
    only and never influence the final score.

    Args:
        provider: Provider name for the configured target.
        execution_target_id: Configured AWS account ID.
        execution_target_name: Friendly configured-target name.
        execution_target_type: Provider target type.
        region: Configured-target runtime region; not used for collection.
        session: Boto3 session; unused because this is a pure consumer.
        dry_run: Whether Anvil is running in dry-run mode.
        metadata: Static task metadata; unused by this task.
        dependency_data: Account, cost, activity, and regional resource results.
        actions: Action recorder provided by the engine.

    Returns:
        An organization summary and one scored assessment per AWS account.

    Raises:
        RuntimeError: If required dependency data has an invalid shape.
    """
    accounts = _accounts(dependency_data.get("accounts"))
    report = build_report(
        accounts=accounts,
        costs=_mapping(dependency_data.get("costs"), "costs"),
        activity=_mapping(dependency_data.get("activity"), "activity"),
        regional_resources=_regional_results(dependency_data.get("regional_resources")),
    )
    account_count = len(accounts)
    actions.record(f"Built inactive-account report for {account_count} AWS account(s)")
    __LOGGER__.info(
        f"Built inactive-account report for configured target {execution_target_id}"
    )
    return report
