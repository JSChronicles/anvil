from __future__ import annotations

import os
from dataclasses import dataclass, field

from anvil.providers.datadog.config import DatadogTargetSettings


DATADOG_EXTRA_REMEDIATION = (
    "Install Datadog dependencies with 'uv sync --extra datadog' for a source "
    "checkout or 'pip install \"anvil[datadog]\"' for an installed package."
)


class DatadogProviderError(RuntimeError):
    """Base error for actionable Datadog provider failures."""


class DatadogDependencyError(DatadogProviderError):
    """Raised when the optional Datadog SDK is unavailable."""


class DatadogCredentialError(DatadogProviderError):
    """Raised when configured Datadog credential sources are incomplete."""


class DatadogAuthenticationError(DatadogProviderError):
    """Raised when Datadog rejects or cannot validate a key pair."""


@dataclass(frozen=True, slots=True)
class DatadogAuthSettings:
    """Resolved Datadog endpoint and key-pair settings."""

    target: DatadogTargetSettings
    api_key: str = field(repr=False)
    app_key: str = field(repr=False)

    @property
    def source(self) -> str:
        """Return the operator-facing authentication source."""

        return f"environment:{self.target.api_key_env}+{self.target.app_key_env}"


@dataclass(frozen=True, slots=True)
class DatadogSession:
    """Datadog runtime session for one organization and global coordinate."""

    target_id: str
    region_name: str
    site: str
    auth_source: str
    client: object = field(repr=False)
    _redaction_values: tuple[str, ...] = field(default=(), repr=False)

    def close(self) -> None:
        """Release resources owned by the generated Datadog API client."""

        close = getattr(self.client, "close", None)
        if not callable(close):
            return
        try:
            close()
        except Exception as error:
            raise DatadogProviderError(
                "Datadog API client cleanup failed: "
                f"{redacted_error_message(error, secrets=self._redaction_values)}"
            ) from error


class DatadogSessionFactory:
    """Resolve credentials and construct Datadog SDK clients lazily."""

    def resolve_auth_settings(
        self, *, settings: DatadogTargetSettings
    ) -> DatadogAuthSettings:
        """Resolve a complete Datadog key pair from environment variables.

        Args:
            settings: Validated site and credential-source configuration.

        Returns:
            Resolved authentication settings.

        Raises:
            DatadogCredentialError: If either configured key is unavailable.
        """

        api_key = os.environ.get(settings.api_key_env)
        app_key = os.environ.get(settings.app_key_env)
        missing_credentials = [
            f"{environment_name} ({key_label})"
            for environment_name, key_label, value in (
                (settings.api_key_env, "API key", api_key),
                (settings.app_key_env, "application key", app_key),
            )
            if value is None or not value.strip()
        ]
        if missing_credentials:
            raise DatadogCredentialError(
                "Datadog credential environment variables are missing or empty: "
                f"{', '.join(missing_credentials)}."
            )
        assert api_key is not None
        assert app_key is not None
        return DatadogAuthSettings(
            target=settings, api_key=api_key.strip(), app_key=app_key.strip()
        )

    def validate_auth(self, *, settings: DatadogTargetSettings) -> str:
        """Validate both configured keys through Datadog's lightweight probe.

        Args:
            settings: Validated site and credential-source configuration.

        Returns:
            Operator-facing authentication source.

        Raises:
            DatadogProviderError: If dependencies, credentials, or authentication
                are invalid.
        """

        auth_settings = self.resolve_auth_settings(settings=settings)
        client = self._create_api_client(auth_settings=auth_settings)
        validation_error: DatadogProviderError | None = None
        try:
            try:
                from datadog_api_client.v2.api.key_management_api import (
                    KeyManagementApi,
                )
            except ImportError as error:
                raise DatadogDependencyError(
                    "Datadog authentication requires optional dependency "
                    f"'datadog-api-client'. {DATADOG_EXTRA_REMEDIATION}"
                ) from error

            response = KeyManagementApi(client).validate_api_key()
            status = getattr(response, "status", None)
            status_value = getattr(status, "value", status)
            if status_value != "ok":
                raise DatadogAuthenticationError(
                    "Datadog authentication validation returned an unexpected "
                    f"status for site '{settings.site}'."
                )
        except DatadogProviderError as error:
            validation_error = error
        except Exception as error:
            validation_error = DatadogAuthenticationError(
                "Datadog authentication validation failed for site "
                f"'{settings.site}': "
                f"{redacted_error_message(error, secrets=(auth_settings.api_key, auth_settings.app_key))}"
            )

        try:
            close = getattr(client, "close", None)
            if callable(close):
                close()
        except Exception as error:
            close_message = redacted_error_message(
                error, secrets=(auth_settings.api_key, auth_settings.app_key)
            )
            if validation_error is None:
                raise DatadogProviderError(
                    "Datadog authentication probe could not close its API client: "
                    f"{close_message}"
                ) from error
            validation_error.add_note(
                f"Datadog authentication probe cleanup also failed: {close_message}"
            )

        if validation_error is not None:
            raise validation_error

        return auth_settings.source

    def create_session(
        self, *, target_id: str, region_name: str, settings: DatadogTargetSettings
    ) -> DatadogSession:
        """Create one Datadog runtime session.

        Args:
            target_id: Logical Anvil identity for the key-bound organization.
            region_name: Provider-neutral execution coordinate.
            settings: Validated site and credential-source configuration.

        Returns:
            A session containing the configured generated API client.

        Raises:
            DatadogProviderError: If the client cannot be configured.
        """

        auth_settings = self.resolve_auth_settings(settings=settings)
        client = self._create_api_client(auth_settings=auth_settings)
        return DatadogSession(
            target_id=target_id,
            region_name=region_name,
            site=settings.site,
            auth_source=auth_settings.source,
            client=client,
            _redaction_values=(auth_settings.api_key, auth_settings.app_key),
        )

    @staticmethod
    def _create_api_client(*, auth_settings: DatadogAuthSettings) -> object:
        """Build the generated Datadog client without import-time SDK coupling."""

        try:
            from datadog_api_client import ApiClient, Configuration
        except ImportError as error:
            raise DatadogDependencyError(
                "Datadog provider requires optional dependency "
                f"'datadog-api-client'. {DATADOG_EXTRA_REMEDIATION}"
            ) from error

        try:
            configuration = Configuration()
            configuration.server_variables["site"] = auth_settings.target.site
            configuration.api_key["apiKeyAuth"] = auth_settings.api_key
            configuration.api_key["appKeyAuth"] = auth_settings.app_key
            return ApiClient(configuration)
        except Exception as error:
            raise DatadogProviderError(
                "Datadog provider could not configure an API client for site "
                f"'{auth_settings.target.site}': "
                f"{redacted_error_message(error, secrets=(auth_settings.api_key, auth_settings.app_key))}"
            ) from error


def redacted_error_message(
    error: Exception, *, secrets: tuple[str | None, ...] = ()
) -> str:
    """Return concise SDK error context without response headers or credentials."""

    status = getattr(error, "status", None)
    reason = getattr(error, "reason", None)
    if status is not None and reason:
        message = f"HTTP {status}: {reason}"
    elif status is not None:
        message = f"HTTP {status}"
    else:
        message = str(error).strip() or type(error).__name__
    for secret in secrets:
        if secret and secret.strip():
            message = message.replace(secret.strip(), "<redacted>")
    return message
