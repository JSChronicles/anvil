from __future__ import annotations

import os
import re
from dataclasses import dataclass

from anvil.providers.base import secret_fingerprint


DEFAULT_SITE = "datadoghq.com"
DEFAULT_API_KEY_ENV = "DD_API_KEY"
DEFAULT_APP_KEY_ENV = "DD_APP_KEY"
SUPPORTED_OPTIONS = frozenset({"site", "api_key_env", "app_key_env"})
SUPPORTED_SITES = frozenset(
    {
        "ap1.datadoghq.com",
        "ap2.datadoghq.com",
        "datadoghq.com",
        "datadoghq.eu",
        "ddog-gov.com",
        "uk1.datadoghq.com",
        "us2.ddog-gov.com",
        "us3.datadoghq.com",
        "us5.datadoghq.com",
    }
)

_ENVIRONMENT_VARIABLE_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SITE_LABEL_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$")


@dataclass(frozen=True, slots=True)
class DatadogTargetSettings:
    """Non-secret Datadog endpoint and credential-source settings."""

    site: str
    api_key_env: str
    app_key_env: str

    def cache_identity(self) -> tuple[object, ...]:
        """Return a secret-safe identity that changes when credentials rotate."""

        return (
            self.site,
            self.api_key_env,
            secret_fingerprint(os.environ.get(self.api_key_env)),
            self.app_key_env,
            secret_fingerprint(os.environ.get(self.app_key_env)),
        )


def target_settings(provider_options: dict[str, object]) -> DatadogTargetSettings:
    """Resolve and validate non-secret Datadog provider settings.

    Args:
        provider_options: Provider options from one target descriptor.

    Returns:
        Normalized site and environment-variable names.

    Raises:
        ValueError: If a site or credential-source option is invalid.
    """

    configured_site = provider_options.get("site")
    if configured_site is None:
        environment_site = os.environ.get("DD_SITE")
        site = DEFAULT_SITE if environment_site is None else environment_site
    else:
        site = configured_site
    if not isinstance(site, str) or not site.strip():
        raise ValueError(
            "Datadog site must be a non-empty hostname in provider.options.site "
            "or DD_SITE"
        )
    normalized_site = _normalize_site(site)

    api_key_env = _string_option(
        provider_options=provider_options,
        option_name="api_key_env",
        default=DEFAULT_API_KEY_ENV,
    )
    app_key_env = _string_option(
        provider_options=provider_options,
        option_name="app_key_env",
        default=DEFAULT_APP_KEY_ENV,
    )
    for option_name, environment_name in (
        ("api_key_env", api_key_env),
        ("app_key_env", app_key_env),
    ):
        if _ENVIRONMENT_VARIABLE_PATTERN.fullmatch(environment_name) is None:
            raise ValueError(
                f"Datadog provider.options.{option_name} must be a valid "
                "environment variable name"
            )

    return DatadogTargetSettings(
        site=normalized_site, api_key_env=api_key_env, app_key_env=app_key_env
    )


def _normalize_site(site: str) -> str:
    """Normalize and validate a generated-client Datadog site parameter."""

    normalized = site.strip().lower()
    if len(normalized) > 253 or "://" in normalized or "/" in normalized:
        raise ValueError(
            "Datadog site must be a hostname such as 'datadoghq.com', not a URL"
        )
    labels = normalized.split(".")
    if any(
        len(label) > 63 or _SITE_LABEL_PATTERN.fullmatch(label) is None
        for label in labels
    ):
        raise ValueError(f"Invalid Datadog site hostname: {site}")
    if normalized not in SUPPORTED_SITES:
        supported_display = ", ".join(sorted(SUPPORTED_SITES))
        raise ValueError(
            f"Unsupported Datadog site '{normalized}'. Supported sites: "
            f"{supported_display}"
        )
    return normalized


def _string_option(
    *, provider_options: dict[str, object], option_name: str, default: str
) -> str:
    """Return a validated string option or its provider default."""

    value = provider_options.get(option_name)
    if value is None:
        return default
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"provider.options.{option_name} must be a non-empty string")
    return value.strip()
