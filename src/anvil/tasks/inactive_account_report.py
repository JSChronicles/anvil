from __future__ import annotations

import fnmatch
import logging
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from botocore.client import BaseClient
from botocore.exceptions import BotoCoreError, ClientError

from anvil.actions import ActionRecorder

__LOGGER__ = logging.getLogger(__name__)

DEFAULT_IGNORED_ROLE_PATTERNS = (
    "AWSControlTowerExecution",
    "OrganizationAccountAccessRole",
    "CloudCustodian",
    "cloud-custodian",
    "SecurityAudit",
    "ReadOnly",
)
DEFAULT_IGNORED_USER_AGENT_PATTERNS = (
    "cloud-custodian",
    "custodian",
    "boto3",
    "botocore",
)
OWNER_TAG_KEYS = (
    "Owner",
    "TechnicalOwner",
    "BusinessOwner",
    "Application",
    "App",
    "CostCenter",
    "Purpose",
)
SCORE_WEIGHTS = {
    "cost_score": 25,
    "activity_score": 30,
    "iam_usage_score": 30,
    "owner_score": 15,
}
NEUTRAL_SCORE = 50
ATHENA_POLL_INTERVAL_SECONDS = 2
ATHENA_QUERY_TIMEOUT_SECONDS = 300


class AthenaQueryError(RuntimeError):
    """Raised when an Athena query fails or does not complete in time."""


@dataclass(frozen=True, slots=True)
class AthenaOptions:
    """Athena source configuration for centralized CloudTrail events."""

    database: str
    table: str
    output_location: str
    workgroup: str


@dataclass(frozen=True, slots=True)
class InactiveAccountOptions:
    """Validated metadata options for inactive account reporting."""

    athena: AthenaOptions
    ignored_role_patterns: tuple[str, ...]
    ignored_user_agent_patterns: tuple[str, ...]


def load_options(metadata: dict[str, object]) -> InactiveAccountOptions:
    """Load and validate task metadata."""
    athena_options = get_athena_options(metadata)

    return InactiveAccountOptions(
        athena=athena_options,
        ignored_role_patterns=merge_patterns(
            DEFAULT_IGNORED_ROLE_PATTERNS,
            metadata.get("ignored_role_patterns"),
            "metadata.ignored_role_patterns",
        ),
        ignored_user_agent_patterns=merge_patterns(
            DEFAULT_IGNORED_USER_AGENT_PATTERNS,
            metadata.get("ignored_user_agent_patterns"),
            "metadata.ignored_user_agent_patterns",
        ),
    )


def get_athena_options(metadata: dict[str, object]) -> AthenaOptions:
    """Extract required Athena metadata."""
    raw_athena = metadata.get("athena")
    if not isinstance(raw_athena, dict):
        raise RuntimeError("inactive_account_report requires metadata.athena")

    database = require_string(raw_athena, "database", "metadata.athena.database")
    table = require_string(raw_athena, "table", "metadata.athena.table")
    output_location = require_string(
        raw_athena, "output_location", "metadata.athena.output_location"
    )

    raw_workgroup = raw_athena.get("workgroup", "primary")
    if not isinstance(raw_workgroup, str) or not raw_workgroup.strip():
        raise RuntimeError("metadata.athena.workgroup must be a non-empty string")

    return AthenaOptions(
        database=database,
        table=table,
        output_location=output_location,
        workgroup=raw_workgroup.strip(),
    )


def require_string(source: dict[object, object], key: str, metadata_path: str) -> str:
    """Return a required non-empty string from metadata."""
    value = source.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"inactive_account_report requires {metadata_path}")
    return value.strip()


def merge_patterns(
    defaults: tuple[str, ...], raw_patterns: object, metadata_path: str
) -> tuple[str, ...]:
    """Merge default and metadata-provided ignored activity patterns."""
    patterns = list(defaults)
    if raw_patterns is None:
        return tuple(patterns)
    if not isinstance(raw_patterns, list):
        raise RuntimeError(f"{metadata_path} must be a list[str]")

    for pattern in raw_patterns:
        if not isinstance(pattern, str):
            raise RuntimeError(f"{metadata_path} must contain only strings")
        stripped_pattern = pattern.strip()
        if stripped_pattern and stripped_pattern not in patterns:
            patterns.append(stripped_pattern)

    return tuple(patterns)


def get_cost_signal(session, account_id: str) -> dict[str, object]:
    """Collect Cost Explorer spend signal for the last 3 complete months."""
    start_date, end_date = get_last_complete_month_range(month_count=3)

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

    return {
        "avg_monthly_cost_3m": round_decimal(average_monthly_cost),
        "total_cost_3m": round_decimal(total_cost),
        "monthly_costs": monthly_costs,
        "cost_score": score_cost(average_monthly_cost),
        "warnings": [],
    }


def get_last_complete_month_range(month_count: int) -> tuple[date, date]:
    """Return the start and exclusive end dates for complete monthly billing periods."""
    today = datetime.now(UTC).date()
    end_date = today.replace(day=1)
    start_year = end_date.year
    start_month = end_date.month - month_count

    while start_month <= 0:
        start_month += 12
        start_year -= 1

    return date(start_year, start_month, 1), end_date


def get_cloudtrail_activity_signal(
    session, account_id: str, options: InactiveAccountOptions
) -> dict[str, object]:
    """Collect centralized CloudTrail activity signals through Athena."""
    try:
        athena_client = session.client("athena")
        console_rows = run_athena_query(
            athena_client,
            build_console_login_query(account_id, options.athena.table),
            options.athena.database,
            options.athena.output_location,
            options.athena.workgroup,
        )
        api_rows = run_athena_query(
            athena_client,
            build_meaningful_api_query(account_id, options.athena.table),
            options.athena.database,
            options.athena.output_location,
            options.athena.workgroup,
        )
        assume_role_rows = run_athena_query(
            athena_client,
            build_assume_role_query(account_id, options.athena.table),
            options.athena.database,
            options.athena.output_location,
            options.athena.workgroup,
        )
    except (BotoCoreError, ClientError, AthenaQueryError) as error:
        warning = (
            f"CloudTrail Athena activity unavailable for account {account_id}: {error}"
        )
        __LOGGER__.warning(warning)
        return {
            "last_console_login": None,
            "console_login_count_90d": None,
            "last_meaningful_api_call": None,
            "meaningful_api_call_count_90d": None,
            "last_any_assume_role": None,
            "any_assume_role_count_90d": None,
            "last_counted_assume_role": None,
            "counted_assume_role_count_90d": None,
            "ignored_assume_role_count_90d": None,
            "ignored_activity_reasons": [],
            "activity_score": NEUTRAL_SCORE,
            "iam_usage_score": NEUTRAL_SCORE,
            "warnings": [warning],
        }

    console_summary = first_row_summary(
        console_rows, "last_console_login", "console_login_count_90d"
    )
    api_summary = first_row_summary(
        api_rows, "last_meaningful_api_call", "meaningful_api_call_count_90d"
    )
    assume_role_summary = classify_assume_role_activity(
        assume_role_rows,
        options.ignored_role_patterns,
        options.ignored_user_agent_patterns,
    )

    last_console_login = normalize_timestamp(console_summary["last_value"])
    last_meaningful_api_call = normalize_timestamp(api_summary["last_value"])
    last_counted_assume_role = normalize_timestamp(
        assume_role_summary["last_counted_assume_role"]
    )

    return {
        "last_console_login": last_console_login,
        "console_login_count_90d": console_summary["count"],
        "last_meaningful_api_call": last_meaningful_api_call,
        "meaningful_api_call_count_90d": api_summary["count"],
        "last_any_assume_role": normalize_timestamp(
            assume_role_summary["last_any_assume_role"]
        ),
        "any_assume_role_count_90d": assume_role_summary["any_assume_role_count"],
        "last_counted_assume_role": last_counted_assume_role,
        "counted_assume_role_count_90d": assume_role_summary[
            "counted_assume_role_count"
        ],
        "ignored_assume_role_count_90d": assume_role_summary[
            "ignored_assume_role_count"
        ],
        "ignored_activity_reasons": assume_role_summary["ignored_activity_reasons"],
        "activity_score": score_activity(last_meaningful_api_call),
        "iam_usage_score": score_iam_usage(
            last_console_login, last_counted_assume_role
        ),
        "warnings": [],
    }


def run_athena_query(
    client: BaseClient, sql: str, database: str, output_location: str, workgroup: str
) -> list[dict[str, str | None]]:
    """Run an Athena query and return rows keyed by result headers."""
    response = client.start_query_execution(
        QueryString=sql,
        QueryExecutionContext={"Database": database},
        ResultConfiguration={"OutputLocation": output_location},
        WorkGroup=workgroup,
    )
    query_execution_id = response["QueryExecutionId"]
    deadline = time.monotonic() + ATHENA_QUERY_TIMEOUT_SECONDS

    while True:
        query_execution = client.get_query_execution(
            QueryExecutionId=query_execution_id
        )["QueryExecution"]
        status = query_execution["Status"]["State"]

        if status == "SUCCEEDED":
            break
        if status in {"FAILED", "CANCELLED"}:
            reason = query_execution["Status"].get("StateChangeReason", "unknown")
            raise AthenaQueryError(
                f"Athena query {query_execution_id} ended with {status}: {reason}"
            )
        if time.monotonic() >= deadline:
            raise AthenaQueryError(
                f"Athena query {query_execution_id} timed out after "
                f"{ATHENA_QUERY_TIMEOUT_SECONDS} seconds"
            )

        time.sleep(ATHENA_POLL_INTERVAL_SECONDS)

    paginator = client.get_paginator("get_query_results")
    headers: list[str] | None = None
    rows: list[dict[str, str | None]] = []

    for page in paginator.paginate(QueryExecutionId=query_execution_id):
        for result_row in page.get("ResultSet", {}).get("Rows", []):
            values = [cell.get("VarCharValue") for cell in result_row.get("Data", [])]
            if headers is None:
                headers = [value or "" for value in values]
                continue

            padded_values = values + [None] * (len(headers) - len(values))
            rows.append(dict(zip(headers, padded_values, strict=False)))

    return rows


def build_console_login_query(account_id: str, table: str) -> str:
    """Build Athena SQL for ConsoleLogin events over the last 90 days."""
    return f"""
SELECT
  CAST(max(eventtime) AS varchar) AS last_console_login,
  CAST(count(*) AS varchar) AS console_login_count_90d
FROM {quote_identifier(table)}
WHERE recipientaccountid = {sql_literal(account_id)}
  AND eventtime >= current_timestamp - interval '90' day
  AND eventsource = 'signin.amazonaws.com'
  AND eventname = 'ConsoleLogin'
""".strip()


def build_meaningful_api_query(account_id: str, table: str) -> str:
    """Build Athena SQL for meaningful non-read API activity over the last 90 days."""
    return f"""
SELECT
  CAST(max(eventtime) AS varchar) AS last_meaningful_api_call,
  CAST(count(*) AS varchar) AS meaningful_api_call_count_90d
FROM {quote_identifier(table)}
WHERE recipientaccountid = {sql_literal(account_id)}
  AND eventtime >= current_timestamp - interval '90' day
  AND eventsource NOT IN ('signin.amazonaws.com', 'sts.amazonaws.com')
  AND eventname <> 'GetCallerIdentity'
  AND (
    lower(CAST(readonly AS varchar)) = 'false'
    OR (
      readonly IS NULL
      AND NOT (
        lower(eventname) LIKE 'describe%'
        OR lower(eventname) LIKE 'list%'
        OR lower(eventname) LIKE 'get%'
      )
    )
  )
""".strip()


def build_assume_role_query(account_id: str, table: str) -> str:
    """Build Athena SQL for STS AssumeRole events over the last 90 days."""
    return f"""
SELECT
  json_extract_scalar(requestparameters, '$.roleArn') AS role_arn,
  useridentity.arn AS caller_arn,
  useragent AS user_agent,
  sourceipaddress AS source_ip,
  CAST(max(eventtime) AS varchar) AS last_event_time,
  CAST(count(*) AS varchar) AS event_count
FROM {quote_identifier(table)}
WHERE recipientaccountid = {sql_literal(account_id)}
  AND eventtime >= current_timestamp - interval '90' day
  AND eventsource = 'sts.amazonaws.com'
  AND eventname = 'AssumeRole'
GROUP BY
  json_extract_scalar(requestparameters, '$.roleArn'),
  useridentity.arn,
  useragent,
  sourceipaddress
""".strip()


def classify_assume_role_activity(
    rows: list[dict[str, str | None]],
    ignored_role_patterns: tuple[str, ...],
    ignored_user_agent_patterns: tuple[str, ...],
) -> dict[str, object]:
    """Split AssumeRole activity into counted and ignored activity."""
    last_any_assume_role: str | None = None
    last_counted_assume_role: str | None = None
    any_assume_role_count = 0
    counted_assume_role_count = 0
    ignored_assume_role_count = 0
    ignored_activity_reasons: set[str] = set()

    for row in rows:
        event_count = parse_int(row.get("event_count"))
        last_event_time = normalize_timestamp(row.get("last_event_time"))
        role_arn = row.get("role_arn")
        user_agent = row.get("user_agent")

        any_assume_role_count += event_count
        last_any_assume_role = latest_timestamp(last_any_assume_role, last_event_time)

        role_reason = matching_pattern(role_arn, ignored_role_patterns)
        user_agent_reason = matching_pattern(user_agent, ignored_user_agent_patterns)

        if role_reason or user_agent_reason:
            ignored_assume_role_count += event_count
            if role_reason:
                ignored_activity_reasons.add(role_reason)
            if user_agent_reason:
                ignored_activity_reasons.add(user_agent_reason)
            continue

        counted_assume_role_count += event_count
        last_counted_assume_role = latest_timestamp(
            last_counted_assume_role, last_event_time
        )

    return {
        "last_any_assume_role": last_any_assume_role,
        "any_assume_role_count": any_assume_role_count,
        "last_counted_assume_role": last_counted_assume_role,
        "counted_assume_role_count": counted_assume_role_count,
        "ignored_assume_role_count": ignored_assume_role_count,
        "ignored_activity_reasons": sorted(ignored_activity_reasons),
    }


def matching_pattern(value: str | None, patterns: tuple[str, ...]) -> str | None:
    """Return the first configured pattern matching a value."""
    if not value:
        return None

    normalized_value = value.lower()
    role_name = normalized_value.rsplit("/", maxsplit=1)[-1]

    for pattern in patterns:
        normalized_pattern = pattern.lower()
        if (
            fnmatch.fnmatchcase(normalized_value, normalized_pattern)
            or fnmatch.fnmatchcase(role_name, normalized_pattern)
            or fnmatch.fnmatchcase(normalized_value, f"*{normalized_pattern}*")
        ):
            return pattern

    return None


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


def score_cost(avg_monthly_cost: Decimal | float | int | None) -> int:
    """Score cost signal where higher means lower spend and more likely inactive."""
    if avg_monthly_cost is None:
        return NEUTRAL_SCORE

    cost = Decimal(str(avg_monthly_cost))
    if cost < Decimal("5"):
        return 100
    if cost < Decimal("10"):
        return 85
    if cost < Decimal("25"):
        return 60
    if cost < Decimal("100"):
        return 30
    return 0


def score_activity(last_meaningful_api_call: str | None) -> int:
    """Score CloudTrail API activity recency."""
    return score_recency(last_meaningful_api_call)


def score_iam_usage(
    last_console_login: str | None, last_counted_assume_role: str | None
) -> int:
    """Score counted IAM activity recency."""
    last_counted_activity = latest_timestamp(
        last_console_login, last_counted_assume_role
    )
    return score_recency(last_counted_activity)


def score_owner(owner_known: bool | None) -> int:
    """Score ownership metadata completeness."""
    if owner_known is None:
        return NEUTRAL_SCORE
    if owner_known:
        return 0
    return 100


def score_recency(timestamp_value: str | None) -> int:
    """Score a timestamp where older or absent activity is more inactive."""
    if timestamp_value is None:
        return 100

    parsed_timestamp = parse_timestamp(timestamp_value)
    if parsed_timestamp is None:
        return NEUTRAL_SCORE

    age = datetime.now(UTC) - parsed_timestamp
    if age >= timedelta(days=60):
        return 75
    if age >= timedelta(days=30):
        return 40
    return 0


def calculate_final_score(scores: dict[str, int], weights: dict[str, int]) -> int:
    """Calculate weighted final inactivity score."""
    total_weight = sum(weights.values())
    weighted_score = sum(scores[key] * weight for key, weight in weights.items())
    return round(weighted_score / total_weight)


def determine_status(final_score: int) -> str:
    """Map final score to inactive account status."""
    if final_score >= 75:
        return "LIKELY_INACTIVE"
    if final_score >= 45:
        return "POSSIBLY_INACTIVE"
    return "ACTIVE"


def build_recommendation(
    status: str, signals: dict[str, object], warnings: list[str]
) -> str:
    """Build a short account disposition recommendation."""
    if status == "ACTIVE":
        recommendation = "No action recommended."
    elif status == "POSSIBLY_INACTIVE":
        recommendation = "Owner review recommended."
    else:
        recommendation = "Candidate for owner review and possible quarantine."

    if warnings:
        recommendation = f"{recommendation} Telemetry is incomplete; verify manually."

    return recommendation


def first_row_summary(
    rows: list[dict[str, str | None]], last_key: str, count_key: str
) -> dict[str, object]:
    """Extract an aggregate timestamp/count query result."""
    if not rows:
        return {"last_value": None, "count": 0}

    return {
        "last_value": rows[0].get(last_key),
        "count": parse_int(rows[0].get(count_key)),
    }


def parse_int(value: object) -> int:
    """Parse an integer value returned by Athena."""
    if value is None:
        return 0
    try:
        return int(str(value))
    except ValueError:
        return 0


def round_decimal(value: Decimal) -> float:
    """Round a Decimal for JSON output."""
    return float(value.quantize(Decimal("0.01")))


def normalize_timestamp(value: str | None) -> str | None:
    """Normalize timestamps to ISO-8601 UTC strings when possible."""
    if not value:
        return None

    parsed_timestamp = parse_timestamp(value)
    if parsed_timestamp is None:
        return value

    return (
        parsed_timestamp.astimezone(UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def parse_timestamp(value: str) -> datetime | None:
    """Parse common Athena/CloudTrail timestamp strings."""
    normalized_value = value.strip()
    if not normalized_value:
        return None

    if normalized_value.endswith("Z"):
        normalized_value = normalized_value[:-1] + "+00:00"

    for timestamp_format in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(normalized_value, timestamp_format).replace(
                tzinfo=UTC
            )
        except ValueError:
            continue

    try:
        parsed_timestamp = datetime.fromisoformat(normalized_value)
    except ValueError:
        return None

    if parsed_timestamp.tzinfo is None:
        return parsed_timestamp.replace(tzinfo=UTC)
    return parsed_timestamp.astimezone(UTC)


def latest_timestamp(left: str | None, right: str | None) -> str | None:
    """Return the most recent normalized timestamp string."""
    if left is None:
        return right
    if right is None:
        return left

    left_timestamp = parse_timestamp(left)
    right_timestamp = parse_timestamp(right)
    if left_timestamp is None or right_timestamp is None:
        return max(left, right)

    if right_timestamp > left_timestamp:
        return normalize_timestamp(right)
    return normalize_timestamp(left)


def sql_literal(value: str) -> str:
    """Return a single-quoted SQL literal."""
    return "'" + value.replace("'", "''") + "'"


def quote_identifier(identifier: str) -> str:
    """Quote an Athena identifier, preserving dotted identifiers."""
    return ".".join(
        '"' + part.replace('"', '""') + '"' for part in identifier.split(".") if part
    )


def run(
    *,
    account_id: str,
    account_alias: str,
    session,
    dry_run: bool,
    metadata: dict[str, object],
    actions: ActionRecorder,
) -> dict[str, object]:
    """Assess whether an AWS account appears active or inactive."""
    options = load_options(metadata)
    region_name = session.region_name
    warnings: list[str] = []

    cost_signal = get_cost_signal(session, account_id)
    warnings.extend(cost_signal.pop("warnings", []))

    activity_signal = get_cloudtrail_activity_signal(session, account_id, options)
    warnings.extend(activity_signal.pop("warnings", []))

    owner_signal = get_owner_signal(session, account_id)
    warnings.extend(owner_signal.pop("warnings", []))

    signals = {**cost_signal, **activity_signal, **owner_signal}
    scores = {
        "cost_score": int(signals["cost_score"]),
        "activity_score": int(signals["activity_score"]),
        "iam_usage_score": int(signals["iam_usage_score"]),
        "owner_score": int(signals["owner_score"]),
    }
    final_score = calculate_final_score(scores, SCORE_WEIGHTS)
    status = determine_status(final_score)
    recommendation = build_recommendation(status, signals, warnings)

    actions.record(
        f"Assessed inactive account status for {account_id}: {status} ({final_score})"
    )
    __LOGGER__.info(
        f"Inactive account assessment complete for {account_alias} ({account_id}), "
        f"region={region_name}, dry_run={dry_run}, status={status}, "
        f"score={final_score}"
    )

    result: dict[str, object] = {
        "record_type": "inactive_account_assessment",
        "account_id": account_id,
        "account_alias": account_alias,
        "region": region_name,
        "status": status,
        "final_score": final_score,
        "scores": scores,
        "signals": {
            key: value
            for key, value in signals.items()
            if key
            not in {"cost_score", "activity_score", "iam_usage_score", "owner_score"}
        },
        "recommendation": recommendation,
    }
    if warnings:
        result["warnings"] = warnings

    return result
