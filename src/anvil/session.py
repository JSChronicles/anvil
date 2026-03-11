"""
AWS session and STS role-assumption utilities.

This module is responsible for:
- creating base boto3 sessions
- managing thread-local worker sessions
- assuming IAM roles into member accounts

It must not:
- perform account or org logic
- manage concurrency
- parse CLI arguments
"""

import logging
import threading

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

__LOGGER__ = logging.getLogger(__name__)

# Thread-local storage for worker sessions
_THREAD_LOCAL = threading.local()

# Shared boto3 client configuration
BOTO_CONFIG = Config(max_pool_connections=40)


def create_base_session(*, profile_name: str | None, region_name: str) -> boto3.Session:
    """
    Create the base boto3 session used for Organizations and STS calls.
    """

    session_kwargs = {"region_name": region_name}

    if profile_name:
        session_kwargs["profile_name"] = profile_name
        __LOGGER__.debug(f"Creating base session using profile '{profile_name}'")
    else:
        __LOGGER__.debug("Creating base session using default credential chain")

    return boto3.Session(**session_kwargs)


def get_worker_session(*, profile_name: str | None, region_name: str) -> boto3.Session:
    """
    Return a thread-local boto3 session for worker execution.

    Sessions are cached per-thread AND per (profile, region) pair
    to prevent credential bleed between organizations.
    """

    key = (profile_name, region_name)

    if getattr(_THREAD_LOCAL, "session_key", None) != key:
        __LOGGER__.debug(
            f"Creating new thread-local worker session "
            f"(profile={profile_name}, region={region_name})"
        )
        _THREAD_LOCAL.session = boto3.Session(
            profile_name=profile_name, region_name=region_name
        )
        _THREAD_LOCAL.session_key = key

    return _THREAD_LOCAL.session


def assume_role(
    *,
    session: boto3.Session,
    account_id: str,
    role_name: str,
    role_session_name: str = "multi-org-account-access",
) -> boto3.Session:
    """
    Assume an IAM role in a target AWS account and return a new session.
    """

    role_arn = f"arn:aws:iam::{account_id}:role/{role_name}"

    __LOGGER__.debug(f"Assuming role {role_arn}")

    sts_client = session.client("sts", config=BOTO_CONFIG)

    try:
        response = sts_client.assume_role(
            RoleArn=role_arn, RoleSessionName=role_session_name
        )
    except ClientError as error:
        __LOGGER__.error(f"Failed to assume role {role_arn}: {error}")
        raise

    credentials = response["Credentials"]

    return boto3.Session(
        aws_access_key_id=credentials["AccessKeyId"],
        aws_secret_access_key=credentials["SecretAccessKey"],
        aws_session_token=credentials["SessionToken"],
        region_name=session.region_name,
    )
