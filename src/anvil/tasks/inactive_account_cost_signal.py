from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from botocore.exceptions import BotoCoreError, ClientError

from anvil.actions import ActionRecorder

__LOGGER__ = logging.getLogger(__name__)

NEUTRAL_SCORE = 50


def get_cost_signal(session, account_id: str) -> dict[str, object]:
    """Collect Cost Explorer spend signal for the last 3 complete months."""
    month_count = 3
    start_date, end_date = get_last_complete_month_range(month_count=month_count)

    try:
        ce_client = session.client("ce")
        response = ce_client.get_cost_and_usage(
            TimePeriod={"Start": start_date.isoformat(), "End": end_date.isoformat()},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            Filter={"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": [account_id]}},
        )
    except (BotoCoreError, ClientError) as error:
        warning = f"Cost Explorer unavailable for account {account_id}: {error}"
        __LOGGER__.warning(warning)
        return {
            "avg_monthly_cost_3m": None,
            "total_cost_3m": None,
            "monthly_costs": [],
            "cost_score": NEUTRAL_SCORE,
            "warnings": [warning],
        }

    monthly_costs: list[dict[str, object]] = []
    total_cost = Decimal("0")

    for period in response.get("ResultsByTime", []):
        amount = period.get("Total", {}).get("UnblendedCost", {}).get("Amount", "0")
        try:
            monthly_cost = Decimal(str(amount))
        except InvalidOperation:
            monthly_cost = Decimal("0")

        total_cost += monthly_cost
        month_label = str(period.get("TimePeriod", {}).get("Start", ""))[:7]
        monthly_costs.append(
            {"month": month_label, "cost": round_decimal(monthly_cost)}
        )

    average_monthly_cost = (
        total_cost / Decimal(len(monthly_costs)) if monthly_costs else Decimal("0")
    )
    warnings: list[str] = []
    if len(monthly_costs) < month_count:
        warning = (
            f"Cost Explorer returned {len(monthly_costs)} of {month_count} expected "
            f"monthly periods for account {account_id}; cost telemetry may be partial."
        )
        __LOGGER__.warning(warning)
        warnings.append(warning)

    return {
        "avg_monthly_cost_3m": round_decimal(average_monthly_cost),
        "total_cost_3m": round_decimal(total_cost),
        "monthly_costs": monthly_costs,
        "cost_score": score_cost(average_monthly_cost),
        "warnings": warnings,
    }


def get_last_complete_month_range(month_count: int) -> tuple[date, date]:
    """Return start and exclusive end dates for complete monthly billing periods."""
    today = datetime.now(UTC).date()
    end_date = today.replace(day=1)
    start_year = end_date.year
    start_month = end_date.month - month_count

    while start_month <= 0:
        start_month += 12
        start_year -= 1

    return date(start_year, start_month, 1), end_date


def score_cost(avg_monthly_cost: Decimal | float | int | None) -> int:
    """Score cost signal where higher means lower spend and more likely inactive."""
    if avg_monthly_cost is None:
        return NEUTRAL_SCORE

    cost = Decimal(str(avg_monthly_cost))
    if cost == Decimal("0"):
        return 100
    if cost < Decimal("5"):
        return 95
    if cost < Decimal("10"):
        return 85
    if cost < Decimal("15"):
        return 75
    if cost < Decimal("25"):
        return 60
    if cost < Decimal("50"):
        return 45
    if cost < Decimal("100"):
        return 30
    if cost < Decimal("250"):
        return 15
    return 0


def round_decimal(value: Decimal) -> float:
    """Round a Decimal for JSON output."""
    return float(value.quantize(Decimal("0.01")))


def run(
    *,
    account_id: str,
    account_alias: str,
    session,
    dry_run: bool,
    metadata: dict[str, object],
    actions: ActionRecorder,
) -> dict[str, object]:
    """Collect only the inactive-account Cost Explorer signal."""
    region_name = session.region_name
    signal = get_cost_signal(session, account_id)
    warnings = signal.pop("warnings", [])

    actions.record(f"Collected inactive-account cost signal for {account_id}")
    __LOGGER__.info(
        f"Collected inactive-account cost signal for {account_alias} ({account_id}), "
        f"region={region_name}, dry_run={dry_run}"
    )

    result: dict[str, object] = {
        "record_type": "inactive_account_cost_signal",
        "account_id": account_id,
        "account_alias": account_alias,
        "region": region_name,
        "signals": signal,
    }
    if warnings:
        result["warnings"] = warnings

    return result
