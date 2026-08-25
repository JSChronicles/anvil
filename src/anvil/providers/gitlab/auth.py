"""GitLab authentication settings and cache identities."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from anvil.descriptors import TargetDescriptor
from anvil.providers.base import secret_fingerprint
from anvil.providers.gitlab.config import (
    AUTH_TYPE_PRIVATE,
    SUPPORTED_AUTH_TYPES,
    gitlab_option,
    normalize_gitlab_url,
)


@dataclass(frozen=True, slots=True)
class GitLabAuthSettings:
    """Resolved GitLab instance and credential settings without token material."""

    url: str
    auth_type: str
    token_env: str
    ca_cert_path: str | None
    source: str

    def token(self) -> str:
        """Return the configured token from the environment.

        Raises:
            RuntimeError: If the configured environment variable is missing or empty.
        """

        token = os.environ.get(self.token_env)
        if token is None or not token.strip():
            raise RuntimeError(
                "GitLab authentication requires environment variable "
                f"'{self.token_env}' to contain a token"
            )
        return token.strip()

    def cache_identity(self) -> tuple[object, ...]:
        """Return a stable credential-sensitive identity without exposing secrets."""

        return (
            self.url,
            self.auth_type,
            self.token_env,
            secret_fingerprint(os.environ.get(self.token_env)),
            self.ca_cert_path,
        )

    def redact(self, message: str) -> str:
        """Remove current token material from provider error text.

        Args:
            message: Error text produced by python-gitlab or the GitLab API.

        Returns:
            Error text safe to expose in Anvil results and logs.
        """

        token = os.environ.get(self.token_env)
        if token is None or not token.strip():
            return message
        return message.replace(token.strip(), "<redacted>")

    @property
    def ssl_verify(self) -> bool | str:
        """Return the python-gitlab SSL verification setting."""

        return self.ca_cert_path or True


def resolve_auth_settings(
    *, target: TargetDescriptor, require_token: bool
) -> GitLabAuthSettings:
    """Resolve GitLab settings from one provider target.

    Args:
        target: GitLab target descriptor.
        require_token: Whether to verify token material is currently available.

    Returns:
        Resolved non-secret GitLab authentication settings.

    Raises:
        RuntimeError: If authentication configuration or token material is invalid.
        ValueError: If the GitLab URL is invalid.
    """

    token_env = gitlab_option(target, "token_env")
    if token_env is None:
        raise RuntimeError("GitLab provider.options.token_env is required")

    auth_type = gitlab_option(target, "auth_type") or AUTH_TYPE_PRIVATE
    if auth_type not in SUPPORTED_AUTH_TYPES:
        supported_display = ", ".join(sorted(SUPPORTED_AUTH_TYPES))
        raise RuntimeError(
            f"Unsupported GitLab auth_type '{auth_type}'. Supported values: "
            f"{supported_display}"
        )

    ca_cert_path = gitlab_option(target, "ca_cert_path")
    if ca_cert_path is not None:
        path = Path(ca_cert_path).expanduser()
        ca_cert_path = str(path)

    settings = GitLabAuthSettings(
        url=normalize_gitlab_url(gitlab_option(target, "url")),
        auth_type=auth_type,
        token_env=token_env,
        ca_cert_path=ca_cert_path,
        source=f"{auth_type}:{token_env}",
    )
    if require_token:
        settings.token()
    return settings
