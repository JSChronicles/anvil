"""GitLab provider configuration helpers."""

from __future__ import annotations

from urllib.parse import SplitResult, urlsplit, urlunsplit

from anvil.descriptors import TargetDescriptor

DEFAULT_GITLAB_URL = "https://gitlab.com"
DEFAULT_GITLAB_PER_PAGE = 100
DEFAULT_REGIONS = ("global",)

MODE_GROUPS = "groups"
MODE_PROJECTS = "projects"
SUPPORTED_MODES = frozenset({MODE_GROUPS, MODE_PROJECTS})

AUTH_TYPE_PRIVATE = "private"
AUTH_TYPE_OAUTH = "oauth"
SUPPORTED_AUTH_TYPES = frozenset({AUTH_TYPE_PRIVATE, AUTH_TYPE_OAUTH})

GITLAB_PROFILE_OPTIONS = frozenset({"url", "auth_type", "token_env", "ca_cert_path"})
SUPPORTED_OPTIONS = GITLAB_PROFILE_OPTIONS | {"profile"}

GITLAB_EXTRA_REMEDIATION = (
    "Install GitLab dependencies with 'uv sync --extra gitlab' for a source "
    "checkout or 'pip install \"anvil[gitlab]\"' for an installed package."
)
GITLAB_AUTH_REMEDIATION = (
    "Verify provider.options.url, auth_type, token_env, and GitLab resource "
    "permissions. Tokens used for discovery and read-only tasks should include "
    "the read_api scope or the broader api scope."
)


def gitlab_option(target: TargetDescriptor, name: str) -> str | None:
    """Return one validated GitLab provider string option."""

    value = target.provider_options.get(name)
    return value.strip() if isinstance(value, str) else None


def normalize_gitlab_url(value: str | None) -> str:
    """Return a canonical GitLab instance URL.

    Args:
        value: Configured GitLab server URL, or ``None`` for GitLab.com.

    Returns:
        A normalized URL without credentials, query parameters, or a trailing slash.

    Raises:
        ValueError: If the URL cannot identify an HTTP(S) GitLab instance.
    """

    raw_url = (value or DEFAULT_GITLAB_URL).strip()
    try:
        parsed = urlsplit(raw_url)
        port = parsed.port
    except ValueError as error:
        raise ValueError(f"GitLab provider.options.url is invalid: {error}") from error

    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("GitLab provider.options.url must use http or https")
    if parsed.hostname is None:
        raise ValueError("GitLab provider.options.url must include a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("GitLab provider.options.url must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError(
            "GitLab provider.options.url must not contain a query or fragment"
        )

    scheme = parsed.scheme.lower()
    hostname = parsed.hostname.lower()
    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    default_port = (scheme == "https" and port == 443) or (
        scheme == "http" and port == 80
    )
    netloc = (
        rendered_host if port is None or default_port else f"{rendered_host}:{port}"
    )
    normalized = SplitResult(
        scheme=scheme,
        netloc=netloc,
        path=parsed.path.rstrip("/"),
        query="",
        fragment="",
    )
    return urlunsplit(normalized)
