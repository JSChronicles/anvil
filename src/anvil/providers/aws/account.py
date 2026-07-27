from __future__ import annotations

import datetime
import logging
import threading
from dataclasses import dataclass, field
from enum import StrEnum

import boto3
from boto3.session import Session

from anvil.execution_context import ExecutionContext
from anvil.providers.aws.session import AssumedRoleCredentials, SessionFactory

__LOGGER__ = logging.getLogger(__name__)

MINIMUM_ASSUMED_CREDENTIAL_REFRESH_WINDOW = datetime.timedelta(minutes=5)
ASSUMED_CREDENTIAL_REFRESH_BUFFER = datetime.timedelta(minutes=2)


class AccountAccessStrategy(StrEnum):
    """Credential path used to access a resolved AWS account."""

    BASE_SESSION = "base_session"
    ASSUME_ROLE = "assume_role"
    DIRECT_PROFILE = "direct_profile"


@dataclass(slots=True)
class _AssumedCredentialState:
    credentials: AssumedRoleCredentials
    refresh_window: datetime.timedelta = MINIMUM_ASSUMED_CREDENTIAL_REFRESH_WINDOW
    refresh_count: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


class Account:
    """Resolved AWS account and its credential/session state."""

    def __init__(
        self,
        *,
        account_id: str,
        account_alias: str,
        is_management: bool,
        access_strategy: AccountAccessStrategy,
        role_name: str | None,
        base_session: boto3.Session,
        context: ExecutionContext,
        regions: list[str],
        session_factory: SessionFactory,
    ) -> None:
        self.account_id = account_id
        self.account_alias = account_alias
        self.is_management = is_management
        self.access_strategy = access_strategy
        self.role_name = role_name
        self._base_session: Session = base_session
        self._context = context
        self._regions = regions
        self._session_factory = session_factory

    def _get_assumed_role_credentials(self) -> AssumedRoleCredentials:
        """Assume the configured member-account role."""

        source_region = self._regions[0]
        worker_session = self._session_factory.get_worker_session(
            profile_name=self._base_session.profile_name, region_name=source_region
        )

        if self.role_name is None:
            raise ValueError("Assume-role account execution requires role_name")

        return self._session_factory.assume_role_credentials(
            session=worker_session, account_id=self.account_id, role_name=self.role_name
        )

    @staticmethod
    def _assumed_credentials_should_refresh(
        credentials: AssumedRoleCredentials,
        *,
        refresh_window: datetime.timedelta = MINIMUM_ASSUMED_CREDENTIAL_REFRESH_WINDOW,
        now: datetime.datetime | None = None,
    ) -> bool:
        expiration = credentials.expiration
        if not isinstance(expiration, datetime.datetime):
            return False

        if expiration.tzinfo is None:
            expiration = expiration.replace(tzinfo=datetime.UTC)

        current_time = now or datetime.datetime.now(datetime.UTC)
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=datetime.UTC)

        return expiration <= current_time + refresh_window

    @staticmethod
    def _refresh_window_for_region_duration(
        region_duration_seconds: float,
    ) -> datetime.timedelta:
        if region_duration_seconds <= 0:
            return MINIMUM_ASSUMED_CREDENTIAL_REFRESH_WINDOW

        return max(
            MINIMUM_ASSUMED_CREDENTIAL_REFRESH_WINDOW,
            datetime.timedelta(seconds=region_duration_seconds)
            + ASSUMED_CREDENTIAL_REFRESH_BUFFER,
        )

    def _update_assumed_credential_refresh_window(
        self,
        *,
        assumed_credential_state: _AssumedCredentialState | None,
        region_duration_seconds: float,
    ) -> None:
        if assumed_credential_state is None:
            return

        observed_window = self._refresh_window_for_region_duration(
            region_duration_seconds
        )
        with assumed_credential_state.lock:
            if observed_window > assumed_credential_state.refresh_window:
                assumed_credential_state.refresh_window = observed_window

    def _get_valid_assumed_role_credentials(
        self, state: _AssumedCredentialState
    ) -> AssumedRoleCredentials:
        with state.lock:
            if self._assumed_credentials_should_refresh(
                state.credentials, refresh_window=state.refresh_window
            ):
                __LOGGER__.info(
                    f"Refreshing assumed-role credentials for account "
                    f"{self.account_alias} ({self.account_id})"
                )
                state.credentials = self._get_assumed_role_credentials()
                state.refresh_count += 1

            return state.credentials

    def _validate_direct_account_access(self) -> None:
        source_region = self._regions[0]
        worker_session = self._session_factory.get_worker_session(
            profile_name=self._base_session.profile_name, region_name=source_region
        )

        caller_account_id = worker_session.client("sts").get_caller_identity()[
            "Account"
        ]
        if caller_account_id != self.account_id:
            raise ValueError(
                f"Direct execution credentials resolve to account '{caller_account_id}', "
                f"not target account '{self.account_id}'"
            )

    def _get_region_session(
        self, *, region: str, assumed_credential_state: _AssumedCredentialState | None
    ) -> boto3.Session:
        """Build the execution session for one account-region pair."""

        if self.access_strategy is not AccountAccessStrategy.ASSUME_ROLE:
            return self._session_factory.get_worker_session(
                profile_name=self._base_session.profile_name, region_name=region
            )

        if assumed_credential_state is None:
            raise ValueError(
                "Expected assumed credentials for assume-role account execution"
            )

        assumed_credentials = self._get_valid_assumed_role_credentials(
            assumed_credential_state
        )
        return self._session_factory.create_session_from_credentials(
            credentials=assumed_credentials, region_name=region
        )
