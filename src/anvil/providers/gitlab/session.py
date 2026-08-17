"""Lazy python-gitlab client and runtime session construction."""

from __future__ import annotations

from dataclasses import dataclass
from types import ModuleType

from anvil.providers.gitlab.auth import GitLabAuthSettings
from anvil.providers.gitlab.config import (
    AUTH_TYPE_OAUTH,
    DEFAULT_GITLAB_PER_PAGE,
    GITLAB_EXTRA_REMEDIATION,
)


@dataclass(frozen=True, slots=True)
class GitLabSession:
    """Runtime session for one GitLab group or project target."""

    target_id: int
    target_type: str
    region_name: str
    client: object
    url: str
    auth_source: str


class GitLabSessionFactory:
    """Build python-gitlab clients only when the GitLab provider is used."""

    def create_client(self, *, settings: GitLabAuthSettings) -> object:
        """Create a configured python-gitlab client.

        Args:
            settings: Resolved GitLab instance and authentication settings.

        Returns:
            A configured ``gitlab.Gitlab`` client.

        Raises:
            RuntimeError: If python-gitlab is unavailable or client creation fails.
        """

        gitlab_module = self._load_python_gitlab()
        client_class = getattr(gitlab_module, "Gitlab", None)
        if not callable(client_class):
            raise RuntimeError("python-gitlab does not expose gitlab.Gitlab")

        token_keyword = (
            "oauth_token" if settings.auth_type == AUTH_TYPE_OAUTH else "private_token"
        )
        kwargs: dict[str, object] = {
            token_keyword: settings.token(),
            "ssl_verify": settings.ssl_verify,
            "per_page": DEFAULT_GITLAB_PER_PAGE,
            "retry_transient_errors": True,
            "keep_base_url": True,
        }
        try:
            return client_class(settings.url, **kwargs)
        except Exception as error:
            raise RuntimeError(
                f"GitLab provider could not build a client for '{settings.url}': "
                f"{settings.redact(str(error))}"
            ) from error

    def validate_auth(self, *, settings: GitLabAuthSettings) -> None:
        """Validate credentials against the configured GitLab instance."""

        client = self.create_client(settings=settings)
        try:
            authenticate = getattr(client, "auth", None)
            if not callable(authenticate):
                raise RuntimeError("python-gitlab client does not expose auth()")
            authenticate()
        except Exception as error:
            raise RuntimeError(
                f"GitLab authentication failed for '{settings.url}': "
                f"{settings.redact(str(error))}"
            ) from error
        finally:
            self.close_client(client)

    def create_session(
        self,
        *,
        target_id: int,
        target_type: str,
        region_name: str,
        settings: GitLabAuthSettings,
    ) -> GitLabSession:
        """Create one target-scoped GitLab runtime session."""

        return GitLabSession(
            target_id=target_id,
            target_type=target_type,
            region_name=region_name,
            client=self.create_client(settings=settings),
            url=settings.url,
            auth_source=settings.source,
        )

    @staticmethod
    def close_client(client: object) -> None:
        """Close a python-gitlab client's underlying HTTP session when available."""

        session = getattr(client, "session", None)
        close = getattr(session, "close", None)
        if callable(close):
            close()

    @staticmethod
    def _load_python_gitlab() -> ModuleType:
        """Import python-gitlab with an actionable optional-extra error."""

        try:
            import gitlab
        except ImportError as error:
            raise RuntimeError(
                "GitLab provider requires optional dependency 'python-gitlab'. "
                f"{GITLAB_EXTRA_REMEDIATION}"
            ) from error
        return gitlab
