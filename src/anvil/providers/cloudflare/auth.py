"""Cloudflare credential resolution without SDK imports or network calls."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from anvil.providers.base import secret_fingerprint

DEFAULT_API_TOKEN_ENV = "CLOUDFLARE_API_TOKEN"
DEFAULT_API_KEY_ENV = "CLOUDFLARE_API_KEY"
DEFAULT_API_EMAIL_ENV = "CLOUDFLARE_EMAIL"
DEFAULT_BASE_URL_ENV = "CLOUDFLARE_BASE_URL"
CLOUDFLARE_CREDENTIAL_REMEDIATION = (
    "Set CLOUDFLARE_API_TOKEN, or set both CLOUDFLARE_API_KEY and "
    "CLOUDFLARE_EMAIL for legacy global-key authentication. Zone discovery "
    "requires Zone Read access; account discovery requires credentials accepted "
    "by Cloudflare's account-listing endpoint."
)


class CloudflareCredentialError(RuntimeError):
    """Raised when Cloudflare credential configuration cannot be resolved."""


@dataclass(frozen=True, slots=True)
class CloudflareAuthSettings:
    """Resolved Cloudflare credentials and endpoint settings."""

    source: str
    api_token: str | None = field(default=None, repr=False)
    api_key: str | None = field(default=None, repr=False)
    api_email: str | None = field(default=None, repr=False)
    base_url: str | None = None

    def cache_identity(self) -> tuple[object, ...]:
        """Return a stable identity that never includes raw credentials."""

        return (
            self.source,
            secret_fingerprint(self.api_token),
            secret_fingerprint(self.api_key),
            secret_fingerprint(self.api_email),
            self.base_url,
        )


def validate_auth_options(*, provider_options: Mapping[str, object]) -> None:
    """Validate explicit Cloudflare credential option combinations.

    Args:
        provider_options: Provider-owned target options.

    Raises:
        ValueError: If token and legacy options are mixed or incomplete.
    """

    token_env = _string_option(provider_options, "api_token_env")
    key_env = _string_option(provider_options, "api_key_env")
    email_env = _string_option(provider_options, "api_email_env")

    if token_env is not None and (key_env is not None or email_env is not None):
        raise ValueError(
            "Cloudflare provider.options.api_token_env cannot be combined with "
            "legacy api_key_env or api_email_env"
        )
    if (key_env is None) != (email_env is None):
        raise ValueError(
            "Cloudflare legacy authentication requires both "
            "provider.options.api_key_env and provider.options.api_email_env"
        )


def resolve_auth_settings(
    *, provider_options: Mapping[str, object]
) -> CloudflareAuthSettings:
    """Resolve Cloudflare credentials from configured or standard environment names.

    Args:
        provider_options: Provider-owned target options.

    Returns:
        Resolved credentials and endpoint configuration.

    Raises:
        RuntimeError: If required credential environment variables are absent.
        ValueError: If explicit credential options are incompatible.
    """

    validate_auth_options(provider_options=provider_options)
    base_url = _string_option(provider_options, "base_url") or _optional_env(
        DEFAULT_BASE_URL_ENV
    )
    token_env = _string_option(provider_options, "api_token_env")
    if token_env is not None:
        return CloudflareAuthSettings(
            source=f"api_token:{token_env}",
            api_token=_required_env(token_env, credential="API token"),
            base_url=base_url,
        )

    key_env = _string_option(provider_options, "api_key_env")
    email_env = _string_option(provider_options, "api_email_env")
    if key_env is not None and email_env is not None:
        return CloudflareAuthSettings(
            source=f"global_api_key:{key_env}+{email_env}",
            api_key=_required_env(key_env, credential="Global API key"),
            api_email=_required_env(email_env, credential="API email"),
            base_url=base_url,
        )

    api_token = _optional_env(DEFAULT_API_TOKEN_ENV)
    if api_token is not None:
        return CloudflareAuthSettings(
            source=f"api_token:{DEFAULT_API_TOKEN_ENV}",
            api_token=api_token,
            base_url=base_url,
        )

    api_key = _optional_env(DEFAULT_API_KEY_ENV)
    api_email = _optional_env(DEFAULT_API_EMAIL_ENV)
    if api_key is not None or api_email is not None:
        if api_key is None or api_email is None:
            missing = DEFAULT_API_KEY_ENV if api_key is None else DEFAULT_API_EMAIL_ENV
            raise CloudflareCredentialError(
                "Cloudflare legacy authentication is incomplete; set both "
                f"{DEFAULT_API_KEY_ENV} and {DEFAULT_API_EMAIL_ENV}. Missing {missing}."
            )
        return CloudflareAuthSettings(
            source=(f"global_api_key:{DEFAULT_API_KEY_ENV}+{DEFAULT_API_EMAIL_ENV}"),
            api_key=api_key,
            api_email=api_email,
            base_url=base_url,
        )

    raise CloudflareCredentialError(
        "Cloudflare credentials were not found. Set CLOUDFLARE_API_TOKEN, or set "
        "both CLOUDFLARE_API_KEY and CLOUDFLARE_EMAIL. Custom environment names "
        "can be selected with provider.options.api_token_env or the paired "
        "api_key_env and api_email_env options."
    )


def _string_option(
    provider_options: Mapping[str, object], option_name: str
) -> str | None:
    value = provider_options.get(option_name)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _required_env(environment_name: str, *, credential: str) -> str:
    value = _optional_env(environment_name)
    if value is None:
        raise CloudflareCredentialError(
            f"Cloudflare {credential} environment variable '{environment_name}' "
            "is not set or is empty."
        )
    return value


def _optional_env(environment_name: str) -> str | None:
    value = os.environ.get(environment_name)
    if value is None or not value.strip():
        return None
    return value.strip()
