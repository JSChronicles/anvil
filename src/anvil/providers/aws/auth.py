from __future__ import annotations

import datetime
import logging
import os
import time
from configparser import ConfigParser
from enum import Enum
from pathlib import Path

import boto3
from botocore.exceptions import (
    ClientError,
    NoCredentialsError,
    ProfileNotFound,
    TokenRetrievalError,
    UnauthorizedSSOTokenError,
)

from anvil.results import AuthResult, ExecutionStatus

__LOGGER__ = logging.getLogger(__name__)


class AuthSource(str, Enum):
    SSO = "sso"
    PROFILE_STATIC = "profile_static"
    PROFILE_ASSUME_ROLE = "profile_assume_role"
    ENVIRONMENT = "environment"
    OIDC = "oidc"
    UNKNOWN = "unknown"


def infer_auth_source(profile: str | None) -> AuthSource:
    if profile is None:
        if os.getenv("AWS_WEB_IDENTITY_TOKEN_FILE") and os.getenv("AWS_ROLE_ARN"):
            return AuthSource.OIDC
        if os.getenv("AWS_ACCESS_KEY_ID"):
            return AuthSource.ENVIRONMENT
        return AuthSource.UNKNOWN

    parser = ConfigParser()
    parser.read(Path.home() / ".aws" / "config")

    section = f"profile {profile}"
    if not parser.has_section(section):
        return AuthSource.UNKNOWN

    keys: set[str] = set(parser.options(section))

    if "sso_start_url" in keys or "sso_session" in keys:
        return AuthSource.SSO
    if "role_arn" in keys and "source_profile" in keys:
        return AuthSource.PROFILE_ASSUME_ROLE
    if "aws_access_key_id" in keys:
        return AuthSource.PROFILE_STATIC

    return AuthSource.UNKNOWN


def _map_exception(
    exc: Exception, *, auth_source: AuthSource, profile: str | None
) -> tuple[str, str | None]:
    if isinstance(exc, ProfileNotFound):
        return "AWS profile not found.", "Fix your AWS profile configuration."

    if isinstance(exc, NoCredentialsError):
        if auth_source is AuthSource.SSO and profile:
            return "No AWS credentials available.", f"aws sso login --profile {profile}"
        if auth_source is AuthSource.ENVIRONMENT:
            return (
                "No AWS credentials available.",
                "Export AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY.",
            )
        return "No AWS credentials available.", None

    if isinstance(exc, UnauthorizedSSOTokenError | TokenRetrievalError):
        return (
            "AWS SSO session is invalid or expired.",
            f"aws sso login --profile {profile}" if profile else None,
        )

    if isinstance(exc, ClientError):
        error = exc.response.get("Error", {})
        code = error.get("Code")
        message = error.get("Message", str(exc))

        if code in {"ExpiredToken", "ExpiredTokenException"}:
            return (
                "AWS credentials have expired.",
                f"aws sso login --profile {profile}"
                if auth_source is AuthSource.SSO and profile
                else None,
            )

        if code in {"AccessDenied", "AccessDeniedException"}:
            return "Access denied when calling AWS.", "Verify IAM permissions."

        return message, None

    return "Unexpected error during authentication.", None


def auth_check(
    *, target_name: str, profile: str | None, auth_source: AuthSource
) -> AuthResult:
    __LOGGER__.info(
        f"Running auth check for target={target_name} profile={profile} auth_source={auth_source}"
    )

    started_perf: int | float = time.perf_counter()
    started_at: str = datetime.datetime.now(datetime.UTC).isoformat()

    try:
        session = boto3.Session(profile_name=profile)
        session.client("sts").get_caller_identity()

        ended_perf: int | float = time.perf_counter()
        ended_at: str = datetime.datetime.now(datetime.UTC).isoformat()

        return AuthResult(
            target_name=target_name,
            status=ExecutionStatus.SUCCESS,
            source=auth_source.value,
            started_at=started_at,
            ended_at=ended_at,
            duration_seconds=ended_perf - started_perf,
            message="Authenticated successfully.",
        )

    except Exception as exc:
        ended_perf: int | float = time.perf_counter()
        ended_at: str = datetime.datetime.now(datetime.UTC).isoformat()

        message, remediation = _map_exception(
            exc, auth_source=auth_source, profile=profile
        )

        return AuthResult(
            target_name=target_name,
            status=ExecutionStatus.ERROR,
            source=auth_source.value,
            started_at=started_at,
            ended_at=ended_at,
            duration_seconds=ended_perf - started_perf,
            message=message,
            remediation=remediation,
        )
