from __future__ import annotations

from dataclasses import dataclass
from types import ModuleType

from anvil.providers.pagerduty.auth import PagerDutyAuthSettings
from anvil.providers.pagerduty.errors import (
    PagerDutyClientError,
    PagerDutyDependencyError,
)

PAGERDUTY_EXTRA_REMEDIATION = (
    "Install PagerDuty support with 'uv sync --extra pagerduty' for a source "
    "checkout or 'pip install \"anvil[pagerduty]\"' for an installed package."
)
PAGERDUTY_RATE_LIMIT_RETRIES = 3
PAGERDUTY_MAX_HTTP_ATTEMPTS = 4


@dataclass(slots=True)
class PagerDutySession:
    """Task-facing PagerDuty REST session for one account."""

    account_id: str
    region_name: str
    client: object
    api_url: str
    auth_source: str
    auth_type: str

    def close(self) -> None:
        """Close the underlying persistent HTTP client."""

        close = getattr(self.client, "close", None)
        if callable(close):
            try:
                close()
            except Exception as error:
                raise PagerDutyClientError(
                    "PagerDuty REST client cleanup failed."
                ) from error


class PagerDutySessionFactory:
    """Create PagerDuty clients lazily from resolved provider settings."""

    def validate_settings(self, *, settings: PagerDutyAuthSettings) -> None:
        """Validate SDK availability and client construction without API calls."""

        session = self.create_session(
            account_id=settings.subdomain or "pagerduty-account",
            region_name="global",
            settings=settings,
        )
        session.close()

    def create_session(
        self, *, account_id: str, region_name: str, settings: PagerDutyAuthSettings
    ) -> PagerDutySession:
        """Create a bounded-retry PagerDuty REST API session."""

        pagerduty_module = self._load_pagerduty()
        client_type = getattr(pagerduty_module, "RestApiV2Client", None)
        if not callable(client_type):
            raise PagerDutyDependencyError(
                "PagerDuty optional dependency does not expose RestApiV2Client. "
                f"{PAGERDUTY_EXTRA_REMEDIATION}"
            )

        token = settings.require_token()
        client: object | None = None
        try:
            client = client_type(
                token,
                auth_type=settings.auth_type,
                base_url=settings.api_url,
                default_from=settings.from_email,
            )
            retry = dict(getattr(client, "retry", {}))
            retry[429] = PAGERDUTY_RATE_LIMIT_RETRIES
            client.retry = retry
            client.max_http_attempts = PAGERDUTY_MAX_HTTP_ATTEMPTS
        except Exception as error:
            if client is not None:
                self._close_partially_constructed_client(client=client)
            raise PagerDutyClientError(
                "PagerDuty provider could not initialize a REST API client for "
                f"{settings.api_url}. Check the configured endpoint, authentication "
                f"type, and token. SDK error type: {type(error).__name__}."
            ) from error

        return PagerDutySession(
            account_id=account_id,
            region_name=region_name,
            client=client,
            api_url=settings.api_url,
            auth_source=settings.source,
            auth_type=settings.auth_type,
        )

    @staticmethod
    def _close_partially_constructed_client(*, client: object) -> None:
        """Close a client whose post-construction configuration failed."""

        close = getattr(client, "close", None)
        if not callable(close):
            return
        try:
            close()
        except Exception as close_error:
            raise PagerDutyClientError(
                "PagerDuty REST client setup failed and the partially constructed "
                "client could not be closed."
            ) from close_error

    @staticmethod
    def _load_pagerduty() -> ModuleType:
        try:
            import pagerduty
        except ImportError as error:
            raise PagerDutyDependencyError(
                "PagerDuty provider requires optional dependency 'pagerduty'. "
                f"{PAGERDUTY_EXTRA_REMEDIATION}"
            ) from error
        return pagerduty
