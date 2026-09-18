"""Collect centralized CloudTrail activity signals through Athena."""

from __future__ import annotations

import fnmatch
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from botocore.client import BaseClient
from botocore.exceptions import BotoCoreError, ClientError

from anvil.actions import ActionRecorder

__LOGGER__ = logging.getLogger(__name__)
TASK_SCOPE = "configured_target"
NEUTRAL_SCORE = 50
ATHENA_POLL_INTERVAL_SECONDS = 2
ATHENA_QUERY_TIMEOUT_SECONDS = 300
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


class AthenaQueryError(RuntimeError):
    """Raised when an Athena query fails or times out."""


@dataclass(frozen=True, slots=True)
class AthenaOptions:
    """Validated centralized CloudTrail Athena configuration."""

    database: str
    table: str
    output_location: str
    workgroup: str
    ignored_role_patterns: tuple[str, ...]
    ignored_user_agent_patterns: tuple[str, ...]


def _required_string(source: dict[object, object], key: str, path: str) -> str:
    """Return a required non-empty configuration string."""
    value = source.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"inactive_account_cloudtrail_signal requires {path}")
    return value.strip()


def _patterns(defaults: tuple[str, ...], value: object, path: str) -> tuple[str, ...]:
    """Merge default patterns with a validated metadata list."""
    if value is None:
        return defaults
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RuntimeError(f"{path} must be a list[str]")
    additions = [item.strip() for item in value if item.strip()]
    return tuple(dict.fromkeys((*defaults, *additions)))


def _options(metadata: dict[str, object]) -> AthenaOptions:
    """Load and validate Athena task metadata."""
    raw = metadata.get("athena")
    if not isinstance(raw, dict):
        raise RuntimeError(
            "inactive_account_cloudtrail_signal requires metadata.athena"
        )
    workgroup = raw.get("workgroup", "primary")
    if not isinstance(workgroup, str) or not workgroup.strip():
        raise RuntimeError("metadata.athena.workgroup must be a non-empty string")
    return AthenaOptions(
        database=_required_string(raw, "database", "metadata.athena.database"),
        table=_required_string(raw, "table", "metadata.athena.table"),
        output_location=_required_string(
            raw, "output_location", "metadata.athena.output_location"
        ),
        workgroup=workgroup.strip(),
        ignored_role_patterns=_patterns(
            DEFAULT_IGNORED_ROLE_PATTERNS,
            metadata.get("ignored_role_patterns"),
            "metadata.ignored_role_patterns",
        ),
        ignored_user_agent_patterns=_patterns(
            DEFAULT_IGNORED_USER_AGENT_PATTERNS,
            metadata.get("ignored_user_agent_patterns"),
            "metadata.ignored_user_agent_patterns",
        ),
    )


def _account_ids(value: object) -> list[str]:
    """Extract account IDs from validated dependency context."""
    if not isinstance(value, list):
        raise RuntimeError(
            "inactive_account_cloudtrail_signal requires dependency_data.accounts"
        )
    account_ids: list[str] = []
    for account in value:
        if not isinstance(account, dict) or not isinstance(
            account.get("account_id"), str
        ):
            raise RuntimeError(
                "inactive_account_cloudtrail_signal dependency_data.accounts must "
                "contain account mappings with account_id"
            )
        account_ids.append(account["account_id"])
    return account_ids


def _run_query(
    client: BaseClient, sql: str, options: AthenaOptions
) -> list[dict[str, str | None]]:
    """Execute an Athena query and return rows keyed by column headers."""
    response = client.start_query_execution(
        QueryString=sql,
        QueryExecutionContext={"Database": options.database},
        ResultConfiguration={"OutputLocation": options.output_location},
        WorkGroup=options.workgroup,
    )
    query_id = response["QueryExecutionId"]
    deadline = time.monotonic() + ATHENA_QUERY_TIMEOUT_SECONDS
    while True:
        execution = client.get_query_execution(QueryExecutionId=query_id)[
            "QueryExecution"
        ]
        state = execution["Status"]["State"]
        if state == "SUCCEEDED":
            break
        if state in {"FAILED", "CANCELLED"}:
            reason = execution["Status"].get("StateChangeReason", "unknown")
            raise AthenaQueryError(
                f"Athena query {query_id} ended with {state}: {reason}"
            )
        if time.monotonic() >= deadline:
            raise AthenaQueryError(
                f"Athena query {query_id} timed out after "
                f"{ATHENA_QUERY_TIMEOUT_SECONDS} seconds"
            )
        time.sleep(ATHENA_POLL_INTERVAL_SECONDS)

    headers: list[str] | None = None
    rows: list[dict[str, str | None]] = []
    paginator = client.get_paginator("get_query_results")
    for page in paginator.paginate(QueryExecutionId=query_id):
        for raw_row in page.get("ResultSet", {}).get("Rows", []):
            values = [cell.get("VarCharValue") for cell in raw_row.get("Data", [])]
            if headers is None:
                headers = [value or "" for value in values]
                continue
            padded = values + [None] * (len(headers) - len(values))
            rows.append(dict(zip(headers, padded, strict=False)))
    return rows


def _quote_identifier(identifier: str) -> str:
    """Quote a possibly dotted Athena identifier."""
    parts = identifier.split(".")
    if not all(parts):
        raise RuntimeError("metadata.athena.table must be a valid identifier")
    return ".".join('"' + part.replace('"', '""') + '"' for part in parts)


def _account_filter(account_ids: list[str]) -> str:
    """Build a SQL IN list from provider-supplied numeric AWS account IDs."""
    if not account_ids or not all(value.isdigit() for value in account_ids):
        raise RuntimeError("AWS account IDs must contain only digits")
    return ", ".join(f"'{value}'" for value in account_ids)


def _queries(account_ids: list[str], table: str) -> tuple[str, str, str]:
    """Build aggregate console, API, and AssumeRole queries."""
    source = _quote_identifier(table)
    accounts = _account_filter(account_ids)
    console = f"""
SELECT recipientaccountid,
       CAST(max(eventtime) AS varchar) AS last_console_login,
       CAST(count(*) AS varchar) AS console_login_count_90d
FROM {source}
WHERE recipientaccountid IN ({accounts})
  AND eventtime >= current_timestamp - interval '90' day
  AND eventsource = 'signin.amazonaws.com'
  AND eventname = 'ConsoleLogin'
GROUP BY recipientaccountid
""".strip()
    api = f"""
SELECT recipientaccountid,
       CAST(max(eventtime) AS varchar) AS last_meaningful_api_call,
       CAST(count(*) AS varchar) AS meaningful_api_call_count_90d
FROM {source}
WHERE recipientaccountid IN ({accounts})
  AND eventtime >= current_timestamp - interval '90' day
  AND eventsource NOT IN ('signin.amazonaws.com', 'sts.amazonaws.com')
  AND eventname <> 'GetCallerIdentity'
  AND (lower(CAST(readonly AS varchar)) = 'false'
       OR (readonly IS NULL
           AND NOT (lower(eventname) LIKE 'describe%'
                    OR lower(eventname) LIKE 'list%'
                    OR lower(eventname) LIKE 'get%')))
GROUP BY recipientaccountid
""".strip()
    assume_role = f"""
SELECT recipientaccountid,
       json_extract_scalar(requestparameters, '$.roleArn') AS role_arn,
       useragent AS user_agent,
       CAST(max(eventtime) AS varchar) AS last_event_time,
       CAST(count(*) AS varchar) AS event_count
FROM {source}
WHERE recipientaccountid IN ({accounts})
  AND eventtime >= current_timestamp - interval '90' day
  AND eventsource = 'sts.amazonaws.com'
  AND eventname = 'AssumeRole'
GROUP BY recipientaccountid,
         json_extract_scalar(requestparameters, '$.roleArn'),
         useragent
""".strip()
    return console, api, assume_role


def _parse_int(value: object) -> int:
    """Parse an Athena integer value, returning zero when absent or malformed."""
    try:
        return int(str(value)) if value is not None else 0
    except ValueError:
        return 0


def _parse_timestamp(value: str | None) -> datetime | None:
    """Parse a CloudTrail/Athena timestamp into UTC."""
    if not value:
        return None
    normalized = value.strip().replace(" ", "T")
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _normalize_timestamp(value: str | None) -> str | None:
    """Normalize a timestamp to an ISO-8601 UTC string."""
    parsed = _parse_timestamp(value)
    if parsed is None:
        return value
    return parsed.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _latest(left: str | None, right: str | None) -> str | None:
    """Return the latest of two optional timestamp strings."""
    if left is None:
        return _normalize_timestamp(right)
    if right is None:
        return _normalize_timestamp(left)
    left_time = _parse_timestamp(left)
    right_time = _parse_timestamp(right)
    if left_time is None or right_time is None:
        return max(left, right)
    return _normalize_timestamp(right if right_time > left_time else left)


def _matching_pattern(value: str | None, patterns: tuple[str, ...]) -> str | None:
    """Return the first ignored-activity pattern matching a value."""
    if not value:
        return None
    normalized = value.lower()
    role_name = normalized.rsplit("/", maxsplit=1)[-1]
    for pattern in patterns:
        candidate = pattern.lower()
        if (
            fnmatch.fnmatchcase(normalized, candidate)
            or fnmatch.fnmatchcase(role_name, candidate)
            or fnmatch.fnmatchcase(normalized, f"*{candidate}*")
        ):
            return pattern
    return None


def _score_recency(value: str | None) -> int:
    """Score older activity as stronger evidence of inactivity."""
    if value is None:
        return 100
    parsed = _parse_timestamp(value)
    if parsed is None:
        return NEUTRAL_SCORE
    age = datetime.now(UTC) - parsed
    for days, score in (
        (90, 100),
        (75, 90),
        (60, 75),
        (45, 55),
        (30, 40),
        (14, 20),
        (7, 10),
    ):
        if age >= timedelta(days=days):
            return score
    return 0


def _build_signals(
    account_ids: list[str],
    console_rows: list[dict[str, str | None]],
    api_rows: list[dict[str, str | None]],
    assume_rows: list[dict[str, str | None]],
    options: AthenaOptions,
) -> dict[str, dict[str, object]]:
    """Combine grouped query rows into per-account activity signals."""
    console_by_account = {row.get("recipientaccountid"): row for row in console_rows}
    api_by_account = {row.get("recipientaccountid"): row for row in api_rows}
    assume_by_account: dict[str, list[dict[str, str | None]]] = {}
    for row in assume_rows:
        account_id = row.get("recipientaccountid")
        if account_id:
            assume_by_account.setdefault(account_id, []).append(row)

    signals: dict[str, dict[str, object]] = {}
    for account_id in account_ids:
        console = console_by_account.get(account_id, {})
        api = api_by_account.get(account_id, {})
        last_any: str | None = None
        last_counted: str | None = None
        any_count = counted_count = ignored_count = 0
        ignored_reasons: set[str] = set()
        for row in assume_by_account.get(account_id, []):
            count = _parse_int(row.get("event_count"))
            timestamp = _normalize_timestamp(row.get("last_event_time"))
            any_count += count
            last_any = _latest(last_any, timestamp)
            role_reason = _matching_pattern(
                row.get("role_arn"), options.ignored_role_patterns
            )
            agent_reason = _matching_pattern(
                row.get("user_agent"), options.ignored_user_agent_patterns
            )
            if role_reason or agent_reason:
                ignored_count += count
                ignored_reasons.update(
                    reason for reason in (role_reason, agent_reason) if reason
                )
            else:
                counted_count += count
                last_counted = _latest(last_counted, timestamp)

        last_console = _normalize_timestamp(console.get("last_console_login"))
        last_api = _normalize_timestamp(api.get("last_meaningful_api_call"))
        signals[account_id] = {
            "last_console_login": last_console,
            "console_login_count_90d": _parse_int(
                console.get("console_login_count_90d")
            ),
            "last_meaningful_api_call": last_api,
            "meaningful_api_call_count_90d": _parse_int(
                api.get("meaningful_api_call_count_90d")
            ),
            "last_any_assume_role": last_any,
            "any_assume_role_count_90d": any_count,
            "last_counted_assume_role": last_counted,
            "counted_assume_role_count_90d": counted_count,
            "ignored_assume_role_count_90d": ignored_count,
            "ignored_activity_reasons": sorted(ignored_reasons),
            "activity_score": _score_recency(last_api),
            "iam_usage_score": _score_recency(_latest(last_console, last_counted)),
            "warnings": [],
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
    """Collect centralized CloudTrail activity signals for AWS accounts.

    This configured-target task runs three Athena queries for the complete
    account set instead of repeating queries per account and region. It requires
    ``metadata.athena`` and ``dependency_data.accounts``.

    Args:
        provider: Provider name for the configured target.
        execution_target_id: Configured AWS account ID.
        execution_target_name: Friendly configured-target name.
        execution_target_type: Provider target type.
        region: Region used for the Athena client.
        session: Boto3 session for the configured target.
        dry_run: Whether Anvil is running in dry-run mode.
        metadata: Athena configuration and optional ignored activity patterns.
        dependency_data: Must contain account context as ``accounts``.
        actions: Action recorder provided by the engine.

    Returns:
        A mapping of account IDs to CloudTrail activity signals.

    Raises:
        RuntimeError: If metadata or dependency data is invalid.
    """
    account_ids = _account_ids(dependency_data.get("accounts"))
    if not account_ids:
        return {"signals_by_account": {}}
    options = _options(metadata)
    try:
        client = session.client("athena")
        console_sql, api_sql, assume_sql = _queries(account_ids, options.table)
        signals = _build_signals(
            account_ids,
            _run_query(client, console_sql, options),
            _run_query(client, api_sql, options),
            _run_query(client, assume_sql, options),
            options,
        )
    except (BotoCoreError, ClientError, AthenaQueryError) as error:
        warning = f"Centralized CloudTrail activity unavailable: {error}"
        __LOGGER__.warning(warning)
        signals = {
            account_id: {
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
            for account_id in account_ids
        }
    actions.record(
        f"Collected centralized CloudTrail signals for {len(signals)} AWS account(s)"
    )
    return {"signals_by_account": signals}
