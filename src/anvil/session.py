"""
AWS session and STS role-assumption utilities.

This module is responsible for:
- creating base boto3 sessions
- managing thread-local worker sessions
- assuming IAM roles into member accounts
- constructing region-scoped sessions from assumed credentials

It must not:
- perform account or org logic
- manage concurrency
- parse CLI arguments
"""

from __future__ import annotations

import datetime
import logging
import threading
from dataclasses import dataclass

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

__LOGGER__ = logging.getLogger(__name__)

# Shared boto3 client configuration
BOTO_CONFIG = Config(max_pool_connections=40)


@dataclass(frozen=True, slots=True)
class AssumedRoleCredentials:
    """
    Temporary AWS credentials returned from STS AssumeRole.

    These credentials are region-agnostic and can be reused to construct
    boto3 sessions for any AWS region.
    """

    access_key_id: str
    secret_access_key: str
    session_token: str
    expiration: datetime.datetime | None = None


def _cacheable_client_arg(value: object) -> object:
    if isinstance(value, str | int | float | bool | type(None)):
        return value
    if isinstance(value, tuple | list):
        return tuple(_cacheable_client_arg(item) for item in value)
    if isinstance(value, dict):
        items = [
            (_cacheable_client_arg(key), _cacheable_client_arg(item))
            for key, item in value.items()
        ]
        return tuple(sorted(items, key=repr))

    return ("object", id(value))


def _client_cache_key(
    service_name: str, args: tuple[object, ...], kwargs: dict[str, object]
) -> tuple[str, tuple[object, ...], tuple[tuple[str, object], ...]]:
    return (
        service_name,
        tuple(_cacheable_client_arg(value) for value in args),
        tuple(
            sorted(
                (key, _cacheable_client_arg(value)) for key, value in kwargs.items()
            )
        ),
    )


class CachedClientSession:
    """
    Boto3 session wrapper that lazily caches clients for one execution scope.

    Anvil creates one wrapper per account-region execution. This lets multiple
    tasks in the same account-region reuse clients without sharing clients
    across accounts or regions.
    """

    def __init__(self, *, session: boto3.Session) -> None:
        self._session = session
        self._clients: dict[
            tuple[str, tuple[object, ...], tuple[tuple[str, object], ...]], object
        ] = {}

    def client(self, service_name: str, *args: object, **kwargs: object) -> object:
        key = _client_cache_key(service_name=service_name, args=args, kwargs=kwargs)
        client = self._clients.get(key)
        if client is None:
            client = self._session.client(service_name, *args, **kwargs)
            self._clients[key] = client

        return client

    def __getattr__(self, name: str) -> object:
        return getattr(self._session, name)


class SessionFactory:
    """
    Factory for boto3 sessions and STS-assumed credentials.

    Responsibilities:
    - create base sessions for org-level discovery and control-plane calls
    - cache worker sessions per thread and per (profile, region) pair
    - assume roles and return reusable temporary credentials
    - create region-scoped sessions from those credentials
    """

    def __init__(self, *, boto_config: Config | None = None) -> None:
        self._boto_config = boto_config or BOTO_CONFIG
        self._thread_local = threading.local()

    def create_base_session(
        self, *, profile_name: str | None, region_name: str
    ) -> boto3.Session:
        """
        Create the base boto3 session used for Organizations and STS calls.
        """

        if profile_name:
            __LOGGER__.debug(f"Creating base session using profile '{profile_name}'")
        else:
            __LOGGER__.debug("Creating base session using default credential chain")

        return boto3.Session(
            **self._build_profile_session_kwargs(
                profile_name=profile_name, region_name=region_name
            )
        )

    def get_worker_session(
        self, *, profile_name: str | None, region_name: str
    ) -> boto3.Session:
        """
        Return a thread-local boto3 session for worker execution.

        Sessions are cached per-thread AND per (profile, region) pair
        to prevent credential bleed between organizations and to avoid
        recreating worker sessions unnecessarily.
        """

        key = (profile_name, region_name)
        cache = self._get_worker_session_cache()

        session = cache.get(key)
        if session is None:
            __LOGGER__.debug(
                f"Creating new thread-local worker session "
                f"(profile={profile_name}, region={region_name})"
            )
            session = boto3.Session(
                **self._build_profile_session_kwargs(
                    profile_name=profile_name, region_name=region_name
                )
            )
            cache[key] = session

        return session

    def assume_role_credentials(
        self,
        *,
        session: boto3.Session,
        account_id: str,
        role_name: str,
        role_session_name: str = "multi-org-account-access",
    ) -> AssumedRoleCredentials:
        """
        Assume an IAM role in a target AWS account and return temporary credentials.

        This separates credential acquisition from session construction so the
        same assumed credentials can later be reused across multiple regions.
        """

        role_arn = f"arn:aws:iam::{account_id}:role/{role_name}"

        __LOGGER__.debug(f"Assuming role {role_arn}")

        sts_client = session.client("sts", config=self._boto_config)

        try:
            response = sts_client.assume_role(
                RoleArn=role_arn, RoleSessionName=role_session_name
            )
        except ClientError as error:
            __LOGGER__.error(f"Failed to assume role {role_arn}: {error}")
            raise

        raw_credentials = response["Credentials"]

        return AssumedRoleCredentials(
            access_key_id=raw_credentials["AccessKeyId"],
            secret_access_key=raw_credentials["SecretAccessKey"],
            session_token=raw_credentials["SessionToken"],
            expiration=raw_credentials.get("Expiration"),
        )

    def create_session_from_credentials(
        self, *, credentials: AssumedRoleCredentials, region_name: str
    ) -> boto3.Session:
        """
        Create a boto3 session for a specific region from assumed-role credentials.
        """

        __LOGGER__.debug(
            f"Creating region-scoped session from assumed credentials "
            f"(region={region_name})"
        )

        return boto3.Session(
            aws_access_key_id=credentials.access_key_id,
            aws_secret_access_key=credentials.secret_access_key,
            aws_session_token=credentials.session_token,
            region_name=region_name,
        )

    def create_cached_client_session(
        self, *, session: boto3.Session
    ) -> CachedClientSession:
        """
        Wrap a region-scoped boto3 session with lazy client caching.
        """

        return CachedClientSession(session=session)

    def _get_worker_session_cache(self) -> dict[tuple[str | None, str], boto3.Session]:
        """
        Return the per-thread worker session cache.
        """

        cache = getattr(self._thread_local, "worker_sessions", None)
        if cache is None:
            cache = {}
            self._thread_local.worker_sessions = cache

        return cache

    @staticmethod
    def _build_profile_session_kwargs(
        *, profile_name: str | None, region_name: str
    ) -> dict[str, str]:
        """
        Build boto3.Session keyword arguments.

        Avoids passing profile_name=None explicitly.
        """

        session_kwargs: dict[str, str] = {"region_name": region_name}

        if profile_name:
            session_kwargs["profile_name"] = profile_name

        return session_kwargs
