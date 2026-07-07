from __future__ import annotations

import hashlib
import inspect
import netrc
import os
import re
import subprocess
import threading
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import urlparse

from anvil.descriptors import (
    ConfigBranch,
    MODE_GITHUB_ORGANIZATIONS,
    MODE_GITHUB_REPOSITORIES,
    TargetDescriptor,
)
from anvil.execution_context import ExecutionContext
from anvil.providers.base import (
    ExecutionTarget,
    ProviderAuthResult,
    ProviderExecutionPlan,
    ProviderExecutionRuntime,
    ProviderMetadata,
    ProviderRegion,
)
from anvil.results import ExecutionStatus

DEFAULT_GITHUB_REGIONS = ["global"]
DEFAULT_GITHUB_API_VERSION = "2022-11-28"
GITHUB_CONFIG_ENV = "ANVIL_GITHUB_CONFIG"
GITHUB_CONFIG_PATH = Path(".github") / "config"
GITHUB_FALLBACK_TOKEN_ENVS = ("GITHUB_TOKEN", "GH_TOKEN")
GITHUB_PROFILE_OPTIONS = {
    "api_url",
    "api_version",
    "token_env",
    "app_id",
    "private_key_env",
    "private_key_path",
}
GITHUB_LOGIN_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
GITHUB_REPOSITORY_PATTERN = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[^/\s]+$"
)


@dataclass(frozen=True, slots=True)
class GithubRepository:
    """GitHub repository identity discovered from an owner login."""

    full_name: str
    name: str | None = None
    owner: str | None = None


@dataclass(slots=True)
class _GithubRepositoryDiscoveryFlight:
    event: threading.Event
    repositories: list[GithubRepository] | None = None
    error: BaseException | None = None


class _GithubRepositoryDiscoveryCache:
    """Single-flight cache for GitHub repository discovery."""

    def __init__(self) -> None:
        self._values: dict[object, list[GithubRepository]] = {}
        self._flights: dict[object, _GithubRepositoryDiscoveryFlight] = {}
        self._lock = threading.Lock()

    def get_or_discover(
        self, *, key: object, discover: Callable[[], list[GithubRepository]]
    ) -> list[GithubRepository]:
        with self._lock:
            cached = self._values.get(key)
            if cached is not None:
                return list(cached)

            flight = self._flights.get(key)
            if flight is None:
                flight = _GithubRepositoryDiscoveryFlight(event=threading.Event())
                self._flights[key] = flight
                owns_discovery = True
            else:
                owns_discovery = False

        if owns_discovery:
            try:
                repositories = list(discover())
            except BaseException as error:
                with self._lock:
                    flight.error = error
                    self._flights.pop(key, None)
                    flight.event.set()
                raise

            with self._lock:
                cached = self._values.get(key)
                stored = list(cached) if cached is not None else repositories
                self._values[key] = list(stored)
                flight.repositories = list(stored)
                self._flights.pop(key, None)
                flight.event.set()

            return list(stored)

        flight.event.wait()
        if flight.error is not None:
            raise flight.error
        if flight.repositories is None:
            raise RuntimeError("GitHub repository discovery completed empty")
        return list(flight.repositories)


_GITHUB_REPOSITORY_DISCOVERY_CACHE = _GithubRepositoryDiscoveryCache()


@dataclass(frozen=True, slots=True)
class GithubExecutionTargetData:
    """GitHub-specific target identity and provider options."""

    target_id: str
    target_type: str
    provider_options: dict[str, object]
    session_factory: "GitHubSessionFactory"


class CachedGitHubClient:
    """PyGithub client wrapper with small per-session object caches."""

    def __init__(self, *, client: object) -> None:
        self._client = client
        self._repositories: dict[str, object] = {}
        self._organizations: dict[str, object] = {}

    @property
    def raw_client(self) -> object:
        """Return the wrapped PyGithub client."""

        return self._client

    def get_repo(self, full_name_or_id: str) -> object:
        """Return a cached PyGithub repository object."""

        repository = self._repositories.get(full_name_or_id)
        if repository is None:
            repository = self._client.get_repo(full_name_or_id)
            self._repositories[full_name_or_id] = repository

        return repository

    def get_organization(self, login: str) -> object:
        """Return a cached PyGithub organization object."""

        organization = self._organizations.get(login)
        if organization is None:
            organization = self._client.get_organization(login)
            self._organizations[login] = organization

        return organization

    def list_owner_repositories(self, login: str) -> list[GithubRepository]:
        """Return repositories visible under one organization or user login."""

        owner = self._get_repository_owner(login)
        get_repos = getattr(owner, "get_repos", None)
        if not callable(get_repos):
            raise RuntimeError(
                "GitHub owner object does not expose get_repos()"
            )

        repositories: list[GithubRepository] = []
        for repository in get_repos():
            full_name = getattr(repository, "full_name", None)
            if not isinstance(full_name, str) or not full_name.strip():
                continue
            name = getattr(repository, "name", None)
            repositories.append(
                GithubRepository(
                    full_name=full_name.strip(),
                    name=name if isinstance(name, str) else None,
                    owner=login,
                )
            )

        return sorted(repositories, key=lambda item: item.full_name.lower())

    def _get_repository_owner(self, login: str) -> object:
        try:
            return self.get_organization(login)
        except Exception as error:
            if not _is_not_found(error):
                raise
            get_user = getattr(self._client, "get_user", None)
            if not callable(get_user):
                raise
            return get_user(login)

    def search_code(self, query: str, *, highlight: bool = False) -> object:
        """Run a PyGithub code search through the scoped client."""

        return self._client.search_code(query=query, highlight=highlight)

    def __getattr__(self, name: str) -> object:
        return getattr(self._client, name)


@dataclass(frozen=True, slots=True)
class GitHubAuthSettings:
    """Resolved GitHub authentication and endpoint settings."""

    source: str
    api_url: str | None
    api_version: str
    token_env: str | None = None
    app_id: int | None = None
    private_key: str | None = None
    use_netrc: bool = False
    gh_token: str | None = None

    def cache_identity(self) -> tuple[object, ...]:
        """Return a stable, non-secret identity for credential-sensitive caches."""

        token_fingerprint = None
        if self.token_env is not None:
            token_fingerprint = _secret_fingerprint(os.environ.get(self.token_env))

        return (
            self.source,
            self.api_url,
            self.api_version,
            self.token_env,
            token_fingerprint,
            self.app_id,
            _secret_fingerprint(self.private_key),
            self.use_netrc,
            _secret_fingerprint(self.gh_token),
        )


class GitHubProfileConfig:
    """Load Anvil GitHub profiles from the user profile config file."""

    def __init__(self, *, path: Path | None = None) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        configured_path = os.environ.get(GITHUB_CONFIG_ENV)
        if configured_path:
            return Path(configured_path).expanduser()
        if self._path is not None:
            return self._path.expanduser()
        return Path.home() / GITHUB_CONFIG_PATH

    def load(self) -> dict[str, dict[str, str]]:
        """Return configured GitHub profiles keyed by profile name."""

        path = self.path
        if not path.exists():
            return {}

        try:
            raw_profiles = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as error:
            raise RuntimeError(f"GitHub profile config '{path}' is invalid: {error}") from error
        except OSError as error:
            raise RuntimeError(f"GitHub profile config '{path}' could not be read: {error}") from error

        profiles: dict[str, dict[str, str]] = {}
        for profile_name, raw_profile in raw_profiles.items():
            if not isinstance(raw_profile, dict):
                raise RuntimeError(
                    f"GitHub profile '{profile_name}' in '{path}' must be a table"
                )
            profile: dict[str, str] = {}
            for option_name, option_value in raw_profile.items():
                if option_name not in GITHUB_PROFILE_OPTIONS:
                    raise RuntimeError(
                        f"GitHub profile '{profile_name}' in '{path}' has unsupported "
                        f"option '{option_name}'"
                    )
                if not isinstance(option_value, str) or not option_value.strip():
                    raise RuntimeError(
                        f"GitHub profile '{profile_name}' option '{option_name}' "
                        "must be a non-empty string"
                    )
                profile[option_name] = option_value.strip()
            profiles[profile_name] = profile

        return profiles


@dataclass(frozen=True, slots=True)
class GitHubSession:
    """GitHub runtime session for one configured org or repository target."""

    target_id: str
    target_type: str
    region_name: str
    client: CachedGitHubClient
    api_url: str | None
    api_version: str
    auth_source: str


@dataclass(slots=True)
class _GitHubInstallationFlight:
    event: threading.Event
    installation_id: int | None = None
    error: BaseException | None = None


@dataclass(slots=True)
class _GitHubInstallationClientFlight:
    event: threading.Event
    client: CachedGitHubClient | None = None
    error: BaseException | None = None


class GitHubSessionFactory:
    """Create PyGithub clients lazily from runtime GitHub provider options."""

    def __init__(self, *, profile_config: GitHubProfileConfig | None = None) -> None:
        self._profile_config = profile_config or GitHubProfileConfig()
        self._profile_cache: tuple[Path, dict[str, dict[str, str]]] | None = None
        self._profile_lock = threading.Lock()
        self._installation_ids: dict[object, int] = {}
        self._installation_flights: dict[object, _GitHubInstallationFlight] = {}
        self._installation_clients: dict[object, CachedGitHubClient] = {}
        self._installation_client_flights: dict[
            object, _GitHubInstallationClientFlight
        ] = {}
        self._installation_lock = threading.Lock()

    def create_session(
        self,
        *,
        target_id: str,
        target_type: str,
        region_name: str,
        provider_options: dict[str, object],
    ) -> GitHubSession:
        """Create a GitHub session for one explicit target."""

        github_module = self._load_pygithub()
        settings = self._resolve_auth_settings(provider_options=provider_options)
        client = self._build_session_client(
            github_module=github_module,
            settings=settings,
            target_id=target_id,
            target_type=target_type,
        )
        return GitHubSession(
            target_id=target_id,
            target_type=target_type,
            region_name=region_name,
            client=client,
            api_url=settings.api_url,
            api_version=settings.api_version,
            auth_source=settings.source,
        )

    def list_owner_repositories(
        self, *, owner_logins: list[str], provider_options: dict[str, object]
    ) -> list[GithubRepository]:
        """List repositories visible in configured GitHub owner logins."""

        if not owner_logins:
            raise RuntimeError(
                "GitHub repository discovery requires at least one owner login"
            )

        github_module = self._load_pygithub()
        settings = self._resolve_auth_settings(provider_options=provider_options)

        if settings.app_id is None:
            auth = self._build_auth(
                github_module=github_module,
                settings=settings,
                target_id=owner_logins[0],
                target_type="organization",
            )
            client = CachedGitHubClient(
                client=self._build_client(
                    github_module=github_module,
                    auth=auth,
                    api_url=settings.api_url,
                    api_version=settings.api_version,
                )
            )

            repositories: list[GithubRepository] = []
            for owner_login in owner_logins:
                repositories.extend(client.list_owner_repositories(owner_login))
            return sorted(repositories, key=lambda item: item.full_name.lower())

        repositories = []
        for owner_login in owner_logins:
            client = self._build_session_client(
                github_module=github_module,
                settings=settings,
                target_id=owner_login,
                target_type="organization",
            )
            repositories.extend(client.list_owner_repositories(owner_login))
        return sorted(repositories, key=lambda item: item.full_name.lower())

    def resolve_auth_settings(
        self, *, provider_options: dict[str, object]
    ) -> GitHubAuthSettings:
        """Resolve GitHub auth settings without importing PyGithub or calling GitHub."""

        return self._resolve_auth_settings(provider_options=provider_options)

    def auth_cache_identity(self, *, provider_options: dict[str, object]) -> object:
        """Return a credential-sensitive cache identity without exposing secrets."""

        return self.resolve_auth_settings(
            provider_options=provider_options
        ).cache_identity()

    def _build_session_client(
        self,
        *,
        github_module: ModuleType,
        settings: GitHubAuthSettings,
        target_id: str,
        target_type: str,
    ) -> CachedGitHubClient:
        if settings.app_id is not None:
            return self._installation_client(
                github_module=github_module,
                settings=settings,
                target_id=target_id,
                target_type=target_type,
            )

        auth = self._build_auth(
            github_module=github_module,
            settings=settings,
            target_id=target_id,
            target_type=target_type,
        )
        return CachedGitHubClient(
            client=self._build_client(
                github_module=github_module,
                auth=auth,
                api_url=settings.api_url,
                api_version=settings.api_version,
            )
        )

    def _build_auth(
        self,
        *,
        github_module: ModuleType,
        settings: GitHubAuthSettings,
        target_id: str,
        target_type: str,
    ) -> object:
        auth_module = getattr(github_module, "Auth", None)
        if auth_module is None:
            raise RuntimeError("PyGithub module does not expose github.Auth")

        if settings.token_env is not None:
            return auth_module.Token(self._required_env_token(settings.token_env))

        if settings.gh_token is not None:
            return auth_module.Token(settings.gh_token)

        if settings.use_netrc:
            netrc_auth = getattr(auth_module, "NetrcAuth", None)
            if netrc_auth is None:
                raise RuntimeError("PyGithub module does not expose github.Auth.NetrcAuth")
            return netrc_auth()

        if settings.app_id is not None and settings.private_key is not None:
            app_auth = auth_module.AppAuth(settings.app_id, settings.private_key)
            installation_id = self._installation_id(
                github_module=github_module,
                app_auth=app_auth,
                settings=settings,
                target_id=target_id,
                target_type=target_type,
            )
            return app_auth.get_installation_auth(installation_id)

        raise RuntimeError("GitHub authentication could not be resolved")

    def _installation_client(
        self,
        *,
        github_module: ModuleType,
        settings: GitHubAuthSettings,
        target_id: str,
        target_type: str,
    ) -> CachedGitHubClient:
        auth_module = getattr(github_module, "Auth", None)
        if auth_module is None:
            raise RuntimeError("PyGithub module does not expose github.Auth")
        if settings.app_id is None or settings.private_key is None:
            raise RuntimeError("GitHub app authentication could not be resolved")

        app_auth = auth_module.AppAuth(settings.app_id, settings.private_key)
        installation_id = self._installation_id(
            github_module=github_module,
            app_auth=app_auth,
            settings=settings,
            target_id=target_id,
            target_type=target_type,
        )
        client_key = (
            settings.api_url,
            settings.api_version,
            settings.app_id,
            _secret_fingerprint(settings.private_key),
            installation_id,
        )
        with self._installation_lock:
            cached_client = self._installation_clients.get(client_key)
            if cached_client is not None:
                return cached_client

            flight = self._installation_client_flights.get(client_key)
            if flight is None:
                flight = _GitHubInstallationClientFlight(event=threading.Event())
                self._installation_client_flights[client_key] = flight
                owns_build = True
            else:
                owns_build = False

        if not owns_build:
            flight.event.wait()
            if flight.error is not None:
                raise flight.error
            if flight.client is None:
                raise RuntimeError("GitHub app installation client build completed empty")
            return flight.client

        try:
            installation_auth = app_auth.get_installation_auth(installation_id)
            client = CachedGitHubClient(
                client=self._build_client(
                    github_module=github_module,
                    auth=installation_auth,
                    api_url=settings.api_url,
                    api_version=settings.api_version,
                )
            )
        except BaseException as error:
            with self._installation_lock:
                flight.error = error
                self._installation_client_flights.pop(client_key, None)
                flight.event.set()
            raise

        with self._installation_lock:
            cached_client = self._installation_clients.get(client_key)
            if cached_client is not None:
                flight.client = cached_client
                self._installation_client_flights.pop(client_key, None)
                flight.event.set()
                return cached_client
            self._installation_clients[client_key] = client
            flight.client = client
            self._installation_client_flights.pop(client_key, None)
            flight.event.set()
            return client

    def _build_client(
        self,
        *,
        github_module: ModuleType,
        auth: object,
        api_url: str | None,
        api_version: str,
    ) -> object:
        github_client = getattr(github_module, "Github", None)
        if github_client is None:
            raise RuntimeError("PyGithub module does not expose github.Github")

        kwargs: dict[str, object] = {"auth": auth}
        if api_url is not None:
            kwargs["base_url"] = api_url
        if self._supports_keyword(callable_object=github_client, keyword="api_version"):
            kwargs["api_version"] = api_version

        try:
            return github_client(**kwargs)
        except Exception as error:
            raise RuntimeError(
                f"GitHub provider could not build a runtime session: {error}"
            ) from error

    def _resolve_auth_settings(
        self, *, provider_options: dict[str, object]
    ) -> GitHubAuthSettings:
        profile_name = self._string_option(
            provider_options=provider_options, option_name="profile"
        )
        if profile_name is not None:
            if len(provider_options) > 1:
                raise RuntimeError(
                    "GitHub provider.options.profile cannot be combined with "
                    "inline GitHub auth options"
                )
            profiles = self._load_profiles()
            profile = profiles.get(profile_name)
            if profile is None:
                raise RuntimeError(
                    f"GitHub profile '{profile_name}' was not found in "
                    f"'{self._profile_config.path}'"
                )
            return self._settings_from_options(
                options=profile, source=f"profile:{profile_name}", fail_on_missing=True
            )

        if self._has_explicit_auth_options(provider_options):
            return self._settings_from_options(
                options=provider_options, source="inline", fail_on_missing=True
            )

        profiles = self._load_profiles()
        default_profile = profiles.get("default")
        if default_profile is not None:
            return self._settings_from_options(
                options=default_profile, source="profile:default", fail_on_missing=True
            )

        return self._default_chain(
            api_url=None, api_version=DEFAULT_GITHUB_API_VERSION, source="default"
        )

    def _settings_from_options(
        self, *, options: dict[str, object], source: str, fail_on_missing: bool
    ) -> GitHubAuthSettings:
        api_url = self._string_option(provider_options=options, option_name="api_url")
        api_version = self._string_option(
            provider_options=options, option_name="api_version"
        )
        if api_version is None:
            api_version = DEFAULT_GITHUB_API_VERSION

        has_token = self._string_option(
            provider_options=options, option_name="token_env"
        ) is not None
        has_app = any(
            self._string_option(provider_options=options, option_name=option_name)
            is not None
            for option_name in ("app_id", "private_key_env", "private_key_path")
        )
        if has_token and has_app:
            raise RuntimeError(
                f"GitHub auth settings from {source} mix token and app credentials"
            )

        if has_app:
            app_id = self._required_int_option(
                provider_options=options, option_name="app_id"
            )
            private_key = self._private_key(options=options, source=source)
            return GitHubAuthSettings(
                source=source,
                api_url=api_url,
                api_version=api_version,
                app_id=app_id,
                private_key=private_key,
            )

        if has_token:
            token_env = self._required_string_option(
                provider_options=options, option_name="token_env", source=source
            )
            if fail_on_missing:
                self._required_env_token(token_env)
            return GitHubAuthSettings(
                source=source,
                api_url=api_url,
                api_version=api_version,
                token_env=token_env,
            )

        return self._default_chain(
            api_url=api_url, api_version=api_version, source=source
        )

    def _load_profiles(self) -> dict[str, dict[str, str]]:
        path = self._profile_config.path
        with self._profile_lock:
            if self._profile_cache is not None and self._profile_cache[0] == path:
                return {
                    profile_name: dict(profile)
                    for profile_name, profile in self._profile_cache[1].items()
                }

            profiles = self._profile_config.load()
            cached_profiles = {
                profile_name: dict(profile)
                for profile_name, profile in profiles.items()
            }
            self._profile_cache = (path, cached_profiles)
            return {
                profile_name: dict(profile)
                for profile_name, profile in cached_profiles.items()
            }

    def _default_chain(
        self, *, api_url: str | None, api_version: str, source: str
    ) -> GitHubAuthSettings:
        for token_env in GITHUB_FALLBACK_TOKEN_ENVS:
            token = os.environ.get(token_env)
            if token is not None and token.strip():
                return GitHubAuthSettings(
                    source=f"{source}:{token_env}",
                    api_url=api_url,
                    api_version=api_version,
                    token_env=token_env,
                )

        if self._has_netrc_credentials(api_url=api_url):
            return GitHubAuthSettings(
                source=f"{source}:netrc",
                api_url=api_url,
                api_version=api_version,
                use_netrc=True,
            )

        gh_token = self._github_cli_token()
        if gh_token is not None:
            return GitHubAuthSettings(
                source=f"{source}:gh",
                api_url=api_url,
                api_version=api_version,
                gh_token=gh_token,
            )

        tried = ", ".join(
            [*GITHUB_FALLBACK_TOKEN_ENVS, ".netrc", "gh auth token"]
        )
        raise RuntimeError(f"GitHub authentication failed. Tried: {tried}")

    def _private_key(self, *, options: dict[str, object], source: str) -> str:
        private_key_env = self._string_option(
            provider_options=options, option_name="private_key_env"
        )
        private_key_path = self._string_option(
            provider_options=options, option_name="private_key_path"
        )
        if private_key_env is not None and private_key_path is not None:
            raise RuntimeError(
                f"GitHub app auth from {source} must set only one of "
                "private_key_env or private_key_path"
            )
        if private_key_env is not None:
            private_key = os.environ.get(private_key_env)
            if private_key is None or not private_key.strip():
                raise RuntimeError(
                    "GitHub app auth requires environment variable "
                    f"'{private_key_env}' to contain a private key"
                )
            return private_key

        if private_key_path is None:
            raise RuntimeError(
                f"GitHub app auth from {source} requires private_key_env or "
                "private_key_path"
            )

        path = Path(private_key_path).expanduser()
        try:
            private_key = path.read_text(encoding="utf-8")
        except OSError as error:
            raise RuntimeError(
                f"GitHub app auth could not read private key file '{path}': {error}"
            ) from error

        if not private_key.strip():
            raise RuntimeError(f"GitHub app auth private key file '{path}' is empty")
        return private_key

    @staticmethod
    def _required_env_token(token_env: str) -> str:
        token = os.environ.get(token_env)
        if token is None or not token.strip():
            raise RuntimeError(
                f"GitHub token auth requires environment variable '{token_env}'"
            )
        return token.strip()

    @staticmethod
    def _has_explicit_auth_options(provider_options: dict[str, object]) -> bool:
        return any(option_name in provider_options for option_name in GITHUB_PROFILE_OPTIONS)

    @staticmethod
    def _has_netrc_credentials(*, api_url: str | None) -> bool:
        try:
            credentials = netrc.netrc()
        except (FileNotFoundError, netrc.NetrcParseError, OSError):
            return False

        hosts = [_github_netrc_host(api_url)]
        if hosts[0] == "api.github.com":
            hosts.append("github.com")
        return any(credentials.authenticators(host) is not None for host in hosts)

    @staticmethod
    def _github_cli_token() -> str | None:
        try:
            result = subprocess.run(
                ["gh", "auth", "token"],
                capture_output=True,
                check=False,
                text=True,
                timeout=10,
            )
        except (FileNotFoundError, subprocess.SubprocessError, OSError):
            return None

        if result.returncode != 0:
            return None
        token = result.stdout.strip()
        return token or None

    def _installation_id(
        self,
        *,
        github_module: ModuleType,
        app_auth: object,
        settings: GitHubAuthSettings,
        target_id: str,
        target_type: str,
    ) -> int:
        lookup_paths = _installation_lookup_paths(
            target_id=target_id, target_type=target_type
        )
        cache_key = (
            settings.api_url,
            settings.app_id,
            _installation_cache_owner(target_id=target_id, target_type=target_type),
        )
        with self._installation_lock:
            cached = self._installation_ids.get(cache_key)
            if cached is not None:
                return cached

            flight = self._installation_flights.get(cache_key)
            if flight is None:
                flight = _GitHubInstallationFlight(event=threading.Event())
                self._installation_flights[cache_key] = flight
                owns_lookup = True
            else:
                owns_lookup = False

        if not owns_lookup:
            flight.event.wait()
            if flight.error is not None:
                raise flight.error
            if flight.installation_id is None:
                raise RuntimeError("GitHub app installation lookup completed empty")
            return flight.installation_id

        app_client = self._build_client(
            github_module=github_module,
            auth=app_auth,
            api_url=settings.api_url,
            api_version=settings.api_version,
        )
        try:
            installation_id = self._resolve_installation_id(
                client=app_client, lookup_paths=lookup_paths, target_id=target_id
            )
        except BaseException as error:
            with self._installation_lock:
                flight.error = error
                self._installation_flights.pop(cache_key, None)
                flight.event.set()
            raise

        with self._installation_lock:
            cached = self._installation_ids.get(cache_key)
            stored_installation_id = cached if cached is not None else installation_id
            self._installation_ids[cache_key] = stored_installation_id
            flight.installation_id = stored_installation_id
            self._installation_flights.pop(cache_key, None)
            flight.event.set()
        return stored_installation_id

    def _resolve_installation_id(
        self, *, client: object, lookup_paths: list[str], target_id: str
    ) -> int:
        last_not_found: Exception | None = None
        for lookup_path in lookup_paths:
            try:
                data = self._rest_get_json(client=client, path=lookup_path)
            except Exception as error:
                if _is_not_found(error):
                    last_not_found = error
                    continue
                raise RuntimeError(
                    f"GitHub app installation lookup failed for '{target_id}': {error}"
                ) from error
            return _installation_id_from_response(data=data, target_id=target_id)

        if last_not_found is not None:
            raise RuntimeError(
                f"GitHub app is not installed for target '{target_id}'"
            ) from last_not_found
        raise RuntimeError(f"GitHub app installation lookup failed for '{target_id}'")

    @staticmethod
    def _rest_get_json(*, client: object, path: str) -> object:
        custom = getattr(client, "rest_get_json", None)
        if callable(custom):
            return custom(path, params={})

        requester = getattr(client, "requester", None)
        if requester is None:
            requester = getattr(client, "_Github__requester", None)
        if requester is None or not callable(
            getattr(requester, "requestJsonAndCheck", None)
        ):
            raise RuntimeError(
                "PyGithub client does not expose a REST requester for app "
                "installation lookup"
            )

        try:
            _headers, data = requester.requestJsonAndCheck("GET", path)
        except TypeError:
            try:
                _headers, data = requester.requestJsonAndCheck(
                    "GET", path, parameters={}, headers={}
                )
            except TypeError:
                _headers, data = requester.requestJsonAndCheck("GET", path, {}, {})
        return data

    @staticmethod
    def _load_pygithub() -> ModuleType:
        try:
            import github
        except ImportError as error:
            raise RuntimeError(
                "GitHub provider requires optional dependency 'PyGithub' when "
                "building a GitHub runtime session. Install with 'anvil[github]'."
            ) from error

        return github

    @staticmethod
    def _required_string_option(
        *, provider_options: dict[str, object], option_name: str, source: str
    ) -> str:
        option = provider_options.get(option_name)
        if not isinstance(option, str) or not option.strip():
            raise RuntimeError(f"GitHub auth from {source} requires {option_name}")
        return option.strip()

    @staticmethod
    def _required_int_option(
        *, provider_options: dict[str, object], option_name: str
    ) -> int:
        option = provider_options.get(option_name)
        if not isinstance(option, str) or not option.strip():
            raise RuntimeError(
                f"GitHub app auth requires provider.options.{option_name}"
            )
        try:
            return int(option)
        except ValueError as error:
            raise RuntimeError(
                f"GitHub provider.options.{option_name} must be an integer"
            ) from error

    @staticmethod
    def _string_option(
        *, provider_options: dict[str, object], option_name: str
    ) -> str | None:
        option = provider_options.get(option_name)
        return option if isinstance(option, str) else None

    @staticmethod
    def _supports_keyword(*, callable_object: Any, keyword: str) -> bool:
        try:
            signature = inspect.signature(callable_object)
        except TypeError, ValueError:
            return False

        return keyword in signature.parameters or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        )


class GithubExecutionRuntime:
    """GitHub runtime adapter for one explicit org or repository target."""

    def __init__(self, *, data: GithubExecutionTargetData) -> None:
        self._data = data

    def build_session(self, *, region: str) -> GitHubSession:
        """Build a lazy GitHub session for one global location."""

        return self._data.session_factory.create_session(
            target_id=self._data.target_id,
            target_type=self._data.target_type,
            region_name=region,
            provider_options=self._data.provider_options,
        )

    def record_region_outcome(
        self, *, region: str, duration_seconds: float, failed: bool, interrupted: bool
    ) -> None:
        """GitHub runtime currently has no adaptive lifecycle state."""

    def close(self) -> None:
        """GitHub runtime currently has no explicit resources to release."""


class GithubProvider:
    """GitHub provider for organization-discovered and explicit repository targets."""

    metadata = ProviderMetadata(
        name="github", display_name="GitHub", description="GitHub provider"
    )

    def __init__(self, *, session_factory: GitHubSessionFactory | None = None) -> None:
        self._session_factory = session_factory or GitHubSessionFactory()

    def validate_target(self, target: TargetDescriptor) -> None:
        """Validate GitHub's first schema v2 target modes."""

        if target.config_branch is not ConfigBranch.TARGETS:
            raise ValueError(
                "GitHub provider supports targets config (schema_version: 2) only"
            )
        if target.mode not in {MODE_GITHUB_ORGANIZATIONS, MODE_GITHUB_REPOSITORIES}:
            raise ValueError(f"Unsupported GitHub target mode: {target.mode}")
        if not target.include:
            raise ValueError(
                f"GitHub mode '{target.mode}' requires include with owner or "
                "repository targets"
            )
        if target.exclude is not None:
            raise ValueError(f"GitHub mode '{target.mode}' does not allow exclude")
        self._validate_include_values(mode=target.mode, include=target.include)

    def default_regions(self, target: TargetDescriptor) -> list[str]:
        """Return GitHub's provider-neutral global location."""

        self.validate_target(target)
        if target.regions == ["us-east-1"]:
            return list(DEFAULT_GITHUB_REGIONS)
        return list(target.regions or DEFAULT_GITHUB_REGIONS)

    def auth_cache_key(self, target: TargetDescriptor) -> object | None:
        """Return a stable auth cache identity without importing PyGithub."""

        return (
            self.metadata.name,
            self._session_factory.auth_cache_identity(
                provider_options=target.provider_options
            ),
        )

    def auth_check(self, target: TargetDescriptor) -> ProviderAuthResult:
        """Validate GitHub auth settings without importing PyGithub or calling GitHub."""

        self.validate_target(target)
        try:
            settings = self._session_factory.resolve_auth_settings(
                provider_options=target.provider_options
            )
        except RuntimeError as error:
            return ProviderAuthResult(
                status=ExecutionStatus.ERROR,
                source="github",
                message=str(error),
            )

        return ProviderAuthResult(
            status=ExecutionStatus.SUCCESS,
            source=settings.source,
            message="GitHub authentication settings resolved.",
        )

    def discover_regions(self, target: TargetDescriptor) -> list[ProviderRegion]:
        """Return configured/default GitHub locations without live discovery."""

        return [
            ProviderRegion(name=region, available=True, status="configured")
            for region in self.default_regions(target)
        ]

    def resolve_execution_targets(
        self,
        *,
        target: TargetDescriptor,
        regions: list[str],
        include: list[str] | None,
        exclude: list[str] | None,
    ) -> ProviderExecutionPlan:
        """Resolve configured GitHub organizations or repositories."""

        self.validate_target(target)
        if exclude is not None:
            raise ValueError(f"GitHub mode '{target.mode}' does not allow exclude")

        if target.mode == MODE_GITHUB_ORGANIZATIONS:
            repositories = self._discover_repositories(
                owner_logins=include or target.include or [],
                provider_options=target.provider_options,
            )
            target_ids = [repository.full_name for repository in repositories]
            target_type = "repository"
        else:
            target_ids = include or target.include or []
            target_type = "repository"

        execution_targets = [
            self._execution_target(
                target_id=target_id,
                target_type=target_type,
                provider_options=target.provider_options,
            )
            for target_id in target_ids
        ]
        return ProviderExecutionPlan(execution_targets=execution_targets)

    def _discover_repositories(
        self, *, owner_logins: list[str], provider_options: dict[str, object]
    ) -> list[GithubRepository]:
        if type(self._session_factory) is not GitHubSessionFactory:
            return self._session_factory.list_owner_repositories(
                owner_logins=owner_logins,
                provider_options=provider_options,
            )

        discovery_key = self._repository_discovery_cache_key(
            owner_logins=owner_logins, provider_options=provider_options
        )
        return _GITHUB_REPOSITORY_DISCOVERY_CACHE.get_or_discover(
            key=discovery_key,
            discover=lambda: self._session_factory.list_owner_repositories(
                owner_logins=owner_logins,
                provider_options=provider_options,
            ),
        )

    def _repository_discovery_cache_key(
        self, *, owner_logins: list[str], provider_options: dict[str, object]
    ) -> object:
        return (
            GitHubSessionFactory,
            tuple(owner_logins),
            self._session_factory.auth_cache_identity(
                provider_options=provider_options
            ),
        )

    def prepare_execution_runtime(
        self,
        *,
        target: TargetDescriptor,
        execution_target: ExecutionTarget,
        context: ExecutionContext,
    ) -> ProviderExecutionRuntime:
        """Prepare GitHub runtime state for one explicit org or repository target."""

        self.validate_target(target)
        if execution_target.provider != self.metadata.name:
            raise ValueError(
                f"Execution target provider '{execution_target.provider}' is not github"
            )
        if not isinstance(execution_target.provider_data, GithubExecutionTargetData):
            raise TypeError(
                "GitHub execution target is missing GithubExecutionTargetData"
            )

        return GithubExecutionRuntime(data=execution_target.provider_data)

    def _execution_target(
        self, *, target_id: str, target_type: str, provider_options: dict[str, object]
    ) -> ExecutionTarget:
        data = GithubExecutionTargetData(
            target_id=target_id,
            target_type=target_type,
            provider_options=dict(provider_options),
            session_factory=self._session_factory,
        )
        return ExecutionTarget(
            id=target_id,
            name=target_id,
            type=target_type,
            provider=self.metadata.name,
            metadata={"github_target": target_id, "github_target_type": target_type},
            provider_data=data,
        )

    def _validate_include_values(self, *, mode: str | None, include: list[str]) -> None:
        if mode == MODE_GITHUB_ORGANIZATIONS:
            invalid = [
                target_id
                for target_id in include
                if GITHUB_LOGIN_PATTERN.fullmatch(target_id) is None
            ]
            if invalid:
                invalid_display = ", ".join(invalid)
                raise ValueError(
                    "GitHub organizations mode include values must be owner "
                    f"logins: {invalid_display}"
                )
            return

        invalid = [
            target_id
            for target_id in include
            if GITHUB_REPOSITORY_PATTERN.fullmatch(target_id) is None
        ]
        if invalid:
            invalid_display = ", ".join(invalid)
            raise ValueError(
                "GitHub repositories mode include values must use owner/repo: "
                f"{invalid_display}"
            )


def create_provider() -> GithubProvider:
    """Create the first-party GitHub provider."""

    return GithubProvider()


GitHubExecutionTargetData = GithubExecutionTargetData
GitHubExecutionRuntime = GithubExecutionRuntime
GitHubProvider = GithubProvider


def _is_not_found(error: Exception) -> bool:
    status = getattr(error, "status", None)
    data = getattr(error, "data", None)
    return status == 404 or "404" in str(error) or (
        isinstance(data, dict) and data.get("status") == "404"
    )


def _github_netrc_host(api_url: str | None) -> str:
    if api_url is None:
        return "github.com"
    parsed = urlparse(api_url)
    return parsed.hostname or api_url


def _secret_fingerprint(secret: str | None) -> str | None:
    if secret is None:
        return None
    stripped = secret.strip()
    if not stripped:
        return None
    return hashlib.sha256(stripped.encode("utf-8")).hexdigest()


def _installation_lookup_paths(*, target_id: str, target_type: str) -> list[str]:
    owner, separator, repository = target_id.partition("/")
    if target_type == "repository" and separator == "/" and owner and repository:
        return [f"/orgs/{owner}/installation", f"/users/{owner}/installation"]
    return [f"/orgs/{target_id}/installation", f"/users/{target_id}/installation"]


def _installation_cache_owner(*, target_id: str, target_type: str) -> str:
    owner, separator, repository = target_id.partition("/")
    if target_type == "repository" and separator == "/" and owner and repository:
        return owner
    return target_id


def _installation_id_from_response(*, data: object, target_id: str) -> int:
    installation_id: object
    if isinstance(data, dict):
        installation_id = data.get("id")
    else:
        installation_id = getattr(data, "id", None)

    if isinstance(installation_id, bool):
        installation_id = None
    if isinstance(installation_id, int):
        return installation_id
    if isinstance(installation_id, str) and installation_id.strip():
        try:
            return int(installation_id)
        except ValueError:
            pass

    raise RuntimeError(
        f"GitHub app installation lookup for '{target_id}' did not return an id"
    )
