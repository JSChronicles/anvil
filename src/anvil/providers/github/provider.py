from __future__ import annotations

import inspect
import os
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

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
DEFAULT_GITHUB_TOKEN_ENV = "GITHUB_TOKEN"
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
class GitHubSession:
    """GitHub runtime session for one configured org or repository target."""

    target_id: str
    target_type: str
    region_name: str
    client: CachedGitHubClient
    auth_type: str
    api_url: str | None
    api_version: str


class GitHubSessionFactory:
    """Create PyGithub clients lazily from runtime GitHub provider options."""

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
        auth_type = self._string_option(
            provider_options=provider_options, option_name="auth_type"
        )
        if auth_type is None:
            auth_type = "token"

        api_url = self._string_option(
            provider_options=provider_options, option_name="api_url"
        )
        api_version = self._string_option(
            provider_options=provider_options, option_name="api_version"
        )
        if api_version is None:
            api_version = DEFAULT_GITHUB_API_VERSION

        auth = self._build_auth(
            github_module=github_module,
            auth_type=auth_type,
            provider_options=provider_options,
        )
        raw_client = self._build_client(
            github_module=github_module,
            auth=auth,
            api_url=api_url,
            api_version=api_version,
        )
        return GitHubSession(
            target_id=target_id,
            target_type=target_type,
            region_name=region_name,
            client=CachedGitHubClient(client=raw_client),
            auth_type=auth_type,
            api_url=api_url,
            api_version=api_version,
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
        auth_type = self._string_option(
            provider_options=provider_options, option_name="auth_type"
        )
        if auth_type is None:
            auth_type = "token"
        api_url = self._string_option(
            provider_options=provider_options, option_name="api_url"
        )
        api_version = self._string_option(
            provider_options=provider_options, option_name="api_version"
        )
        if api_version is None:
            api_version = DEFAULT_GITHUB_API_VERSION

        auth = self._build_auth(
            github_module=github_module,
            auth_type=auth_type,
            provider_options=provider_options,
        )
        client = CachedGitHubClient(
            client=self._build_client(
                github_module=github_module,
                auth=auth,
                api_url=api_url,
                api_version=api_version,
            )
        )

        repositories: list[GithubRepository] = []
        for owner_login in owner_logins:
            repositories.extend(client.list_owner_repositories(owner_login))

        return sorted(repositories, key=lambda item: item.full_name.lower())

    def _build_auth(
        self,
        *,
        github_module: ModuleType,
        auth_type: str,
        provider_options: dict[str, object],
    ) -> object:
        auth_module = getattr(github_module, "Auth", None)
        if auth_module is None:
            raise RuntimeError("PyGithub module does not expose github.Auth")

        if auth_type == "token":
            token = self._token_from_env(provider_options=provider_options)
            return auth_module.Token(token)

        if auth_type == "app":
            private_key = self._private_key(provider_options=provider_options)
            app_id = self._required_int_option(
                provider_options=provider_options, option_name="app_id"
            )
            installation_id = self._required_int_option(
                provider_options=provider_options, option_name="installation_id"
            )
            app_auth = auth_module.AppAuth(app_id, private_key)
            return app_auth.get_installation_auth(installation_id)

        raise RuntimeError("GitHub provider.options.auth_type must be token or app")

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

    def _token_from_env(self, *, provider_options: dict[str, object]) -> str:
        token_env = self._string_option(
            provider_options=provider_options, option_name="token_env"
        )
        if token_env is None:
            token_env = DEFAULT_GITHUB_TOKEN_ENV

        token = os.environ.get(token_env)
        if token is None or not token.strip():
            raise RuntimeError(
                f"GitHub token auth requires environment variable '{token_env}'"
            )
        return token

    def _private_key(self, *, provider_options: dict[str, object]) -> str:
        private_key_env = self._string_option(
            provider_options=provider_options, option_name="private_key_env"
        )
        if private_key_env is not None:
            private_key = os.environ.get(private_key_env)
            if private_key is None or not private_key.strip():
                raise RuntimeError(
                    "GitHub app auth requires environment variable "
                    f"'{private_key_env}' to contain a private key"
                )
            return private_key

        private_key_path = self._string_option(
            provider_options=provider_options, option_name="private_key_path"
        )
        if private_key_path is None:
            raise RuntimeError(
                "GitHub app auth requires provider.options.private_key_env or "
                "provider.options.private_key_path"
            )

        try:
            private_key = Path(private_key_path).read_text(encoding="utf-8")
        except OSError as error:
            raise RuntimeError(
                "GitHub app auth could not read private key file "
                f"'{private_key_path}': {error}"
            ) from error

        if not private_key.strip():
            raise RuntimeError(
                f"GitHub app auth private key file '{private_key_path}' is empty"
            )
        return private_key

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

        auth_type = target.provider_options.get("auth_type", "token")
        api_url = target.provider_options.get("api_url")
        return (self.metadata.name, auth_type, api_url)

    def auth_check(self, target: TargetDescriptor) -> ProviderAuthResult:
        """Report deferred GitHub auth checks without runtime API calls."""

        self.validate_target(target)
        return ProviderAuthResult(
            status=ExecutionStatus.SUCCESS,
            source="deferred",
            message="GitHub authentication is validated when runtime API support is added.",
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
        auth_type = provider_options.get("auth_type", "token")
        api_url = provider_options.get("api_url")
        api_version = provider_options.get("api_version", DEFAULT_GITHUB_API_VERSION)
        token_env = provider_options.get("token_env", DEFAULT_GITHUB_TOKEN_ENV)
        app_id = provider_options.get("app_id")
        installation_id = provider_options.get("installation_id")
        return (
            GitHubSessionFactory,
            tuple(owner_logins),
            auth_type,
            api_url,
            api_version,
            token_env,
            app_id,
            installation_id,
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
