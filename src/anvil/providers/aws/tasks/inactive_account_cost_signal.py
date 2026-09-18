"""Collect organization-wide Cost Explorer signals for inactive accounts."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from botocore.client import BaseClient
from botocore.exceptions import BotoCoreError, ClientError

from anvil.actions import ActionRecorder

__LOGGER__ = logging.getLogger(__name__)
TASK_SCOPE = "configured_target"
NEUTRAL_SCORE = 50


def _accounts(value: object) -> list[dict[str, object]]:
    """Validate and normalize account context supplied by a dependency."""
    if not isinstance(value, list):
        raise RuntimeError(
            "inactive_account_cost_signal requires dependency_data.accounts"
        )
    accounts: list[dict[str, object]] = []
    for account in value:
        if not isinstance(account, dict) or not isinstance(
            account.get("account_id"), str
        ):
            raise RuntimeError(
                "inactive_account_cost_signal dependency_data.accounts must contain "
                "account mappings with account_id"
            )
        accounts.append(account)
    return accounts


def _month_range(month_count: int) -> tuple[date, date]:
    """Return the start and exclusive end of complete monthly billing periods."""
    end_date = datetime.now(UTC).date().replace(day=1)
    start_year = end_date.year
    start_month = end_date.month - month_count
    while start_month <= 0:
        start_month += 12
        start_year -= 1
    return date(start_year, start_month, 1), end_date


def _decimal(value: object) -> Decimal:
    """Parse a Cost Explorer decimal, returning zero for malformed values."""
    try:
        return Decimal(str(value))
    except InvalidOperation, ValueError:
        return Decimal("0")


def _strictly_decreasing(values: list[Decimal]) -> bool:
    """Return whether every value is lower than the preceding value."""
    return len(values) >= 2 and all(
        current < previous for previous, current in zip(values, values[1:])
    )


def _score_cost(values: list[Decimal]) -> tuple[int, int, str]:
    """Return inactivity score, trend adjustment, and trend label."""
    if not values:
        return NEUTRAL_SCORE, 0, "unknown"
    average = sum(values, Decimal("0")) / Decimal(len(values))
    if average == 0:
        base = 100
    elif average < 5:
        base = 95
    elif average < 10:
        base = 85
    elif average < 15:
        base = 75
    elif average < 25:
        base = 60
    elif average < 50:
        base = 45
    elif average < 100:
        base = 30
    elif average < 250:
        base = 15
    else:
        base = 0

    latest = values[-1]
    if latest >= average:
        adjustment = 0
        trend = "above_average" if latest > average else "at_average"
    elif _strictly_decreasing(values):
        adjustment = 25
        trend = "decreasing"
    elif latest == 0:
        adjustment = 20
        trend = "below_average"
    elif latest / average <= Decimal("0.50"):
        adjustment = 15
        trend = "below_average"
    elif latest / average <= Decimal("0.75"):
        adjustment = 10
        trend = "below_average"
    else:
        adjustment = 5
        trend = "below_average"
    return min(100, base + adjustment), adjustment, trend


def _round(value: Decimal) -> float:
    """Round a Decimal to a JSON-friendly currency value."""
    return float(value.quantize(Decimal("0.01")))


def collect_cost_signals(
    ce_client: BaseClient, accounts: list[dict[str, object]], month_count: int = 3
) -> dict[str, dict[str, object]]:
    """Collect grouped monthly spend and build one signal per account."""
    start_date, end_date = _month_range(month_count)
    monthly: dict[str, dict[str, Decimal]] = {
        str(account["account_id"]): {} for account in accounts
    }
    request: dict[str, object] = {
        "TimePeriod": {"Start": start_date.isoformat(), "End": end_date.isoformat()},
        "Granularity": "MONTHLY",
        "Metrics": ["UnblendedCost"],
        "GroupBy": [{"Type": "DIMENSION", "Key": "LINKED_ACCOUNT"}],
    }
    while True:
        response = ce_client.get_cost_and_usage(**request)
        for period in response.get("ResultsByTime", []):
            month = str(period.get("TimePeriod", {}).get("Start", ""))[:7]
            if month:
                for account_months in monthly.values():
                    account_months.setdefault(month, Decimal("0"))
            for group in period.get("Groups", []):
                keys = group.get("Keys", [])
                if not keys:
                    continue
                account_id = str(keys[0])
                if account_id in monthly:
                    amount = (
                        group.get("Metrics", {})
                        .get("UnblendedCost", {})
                        .get("Amount", "0")
                    )
                    monthly[account_id][month] = _decimal(amount)
        next_token = response.get("NextPageToken")
        if not next_token:
            break
        request["NextPageToken"] = next_token

    signals: dict[str, dict[str, object]] = {}
    for account_id, month_values in monthly.items():
        ordered = sorted(month_values.items())
        values = [value for _month, value in ordered]
        total = sum(values, Decimal("0"))
        average = total / Decimal(len(values)) if values else Decimal("0")
        score, adjustment, trend = _score_cost(values)
        warnings = []
        if len(values) < month_count:
            warnings.append(
                f"Cost Explorer returned {len(values)} of {month_count} expected "
                f"monthly periods for account {account_id}"
            )
        signals[account_id] = {
            "avg_monthly_cost_3m": _round(average),
            "total_cost_3m": _round(total),
            "monthly_costs": [
                {"month": month, "cost": _round(value)} for month, value in ordered
            ],
            "cost_score": score,
            "cost_score_adjustment": adjustment,
            "cost_trend": trend,
            "warnings": warnings,
        }
    return signals


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
    """Collect three complete months of Cost Explorer signals by AWS account.

    This read-only configured-target task queries the payer/management account
    once. It requires ``dependency_data.accounts`` from
    ``inactive_account_context``.

    Args:
        provider: Provider name for the configured target.
        execution_target_id: Configured AWS account ID.
        execution_target_name: Friendly configured-target name.
        execution_target_type: Provider target type.
        region: Region used to construct the Cost Explorer client.
        session: Boto3 session for the configured target.
        dry_run: Whether Anvil is running in dry-run mode.
        metadata: Static task metadata; unused by this task.
        dependency_data: Must contain the account context list as ``accounts``.
        actions: Action recorder provided by the engine.

    Returns:
        A mapping of account IDs to cost signals.
    """
    accounts = _accounts(dependency_data.get("accounts"))
    try:
        signals = collect_cost_signals(session.client("ce"), accounts)
    except (BotoCoreError, ClientError) as error:
        warning = f"Cost Explorer unavailable: {error}"
        __LOGGER__.warning(warning)
        signals = {
            str(account["account_id"]): {
                "avg_monthly_cost_3m": None,
                "total_cost_3m": None,
                "monthly_costs": [],
                "cost_score": NEUTRAL_SCORE,
                "cost_score_adjustment": 0,
                "cost_trend": "unknown",
                "warnings": [warning],
            }
            for account in accounts
        }
    actions.record(f"Collected cost signals for {len(signals)} AWS account(s)")
    return {"signals_by_account": signals}
