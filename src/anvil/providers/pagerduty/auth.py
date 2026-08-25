from __future__ import annotations

import os
from dataclasses import dataclass

from anvil.providers.base import secret_fingerprint
from anvil.providers.pagerduty.errors import PagerDutyCredentialError

DEFAULT_API_URL = "https://api.pagerduty.com"
DEFAULT_TOKEN_ENVIRONMENTS = ("PAGERDUTY_API_TOKEN", "PAGERDUTY_USER_API_KEY")


@dataclass(frozen=True, slots=True)
class PagerDutyAuthSettings:
    """Resolved PagerDuty authentication and endpoint settings."""

    source: str
    token_env: str
    token_fingerprint: str | None
    auth_type: str
    api_url: str
    from_email: str | None
    subdomain: str | None

    def cache_identity(self) -> tuple[object, ...]:
        """Return a stable non-secret identity for auth-sensitive caches."""

        return (
            self.source,
            self.token_env,
            self.token_fingerprint,
            self.auth_type,
            self.api_url,
            self.from_email,
            self.subdomain,
        )

    def require_token(self) -> str:
        """Return the configured token or raise an actionable error."""

        token = os.environ.get(self.token_env)
        if token is None or not token.strip():
            raise PagerDutyCredentialError(
                f"PagerDuty authentication requires a non-empty {self.token_env} "
                "environment variable."
            )
        return token.strip()


def resolve_auth_settings(
    *, provider_options: dict[str, object], require_token: bool = True
) -> PagerDutyAuthSettings:
    """Resolve PagerDuty settings without importing the optional SDK."""

    configured_token_env = _string_option(provider_options, "token_env")
    if configured_token_env is not None:
        token_env = configured_token_env
        source = f"environment:{token_env}"
    else:
        token_env = next(
            (
                environment_name
                for environment_name in DEFAULT_TOKEN_ENVIRONMENTS
                if _environment_token(environment_name) is not None
            ),
            DEFAULT_TOKEN_ENVIRONMENTS[0],
        )
        source = f"environment:{token_env}"

    token = _environment_token(token_env)
    settings = PagerDutyAuthSettings(
        source=source,
        token_env=token_env,
        token_fingerprint=secret_fingerprint(token),
        auth_type=_string_option(provider_options, "auth_type") or "token",
        api_url=(_string_option(provider_options, "api_url") or DEFAULT_API_URL).rstrip(
            "/"
        ),
        from_email=_string_option(provider_options, "from_email"),
        subdomain=_lower_string_option(provider_options, "subdomain"),
    )
    if require_token:
        settings.require_token()
    return settings


def _environment_token(environment_name: str) -> str | None:
    """Return a stripped environment token when it is non-empty."""

    token = os.environ.get(environment_name)
    if token is None or not token.strip():
        return None
    return token.strip()


def _string_option(provider_options: dict[str, object], option_name: str) -> str | None:
    """Return one stripped string provider option when configured."""

    option = provider_options.get(option_name)
    if not isinstance(option, str):
        return None
    return option.strip()


def _lower_string_option(
    provider_options: dict[str, object], option_name: str
) -> str | None:
    """Return one normalized case-insensitive string provider option."""

    option = _string_option(provider_options, option_name)
    return option.lower() if option is not None else None
