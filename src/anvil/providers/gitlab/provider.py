"""First-party GitLab provider implementation."""

from __future__ import annotations

from dataclasses import dataclass, replace

from anvil.benchmark import BenchmarkRecorder
from anvil.descriptors import TargetDescriptor
from anvil.execution_context import ExecutionContext
from anvil.provider_profiles import ProviderProfileConfig, ProviderProfileResolver
from anvil.providers.base import (
    ExecutionTarget,
    ProviderAuthResult,
    ProviderExecutionPlan,
    ProviderExecutionRuntime,
    ProviderMetadata,
    ProviderPreparation,
    ProviderPreparationCache,
    ProviderRegion,
    configured_or_default_regions,
    narrow_include,
    validate_region_selectors,
    validate_string_options,
)
from anvil.providers.gitlab.auth import GitLabAuthSettings, resolve_auth_settings
from anvil.providers.gitlab.config import (
    DEFAULT_REGIONS,
    GITLAB_AUTH_REMEDIATION,
    GITLAB_PROFILE_OPTIONS,
    GITLAB_EXTRA_REMEDIATION,
    SUPPORTED_MODES,
    SUPPORTED_OPTIONS,
    gitlab_option,
    normalize_gitlab_url,
)
from anvil.providers.gitlab.session import GitLabSession, GitLabSessionFactory
from anvil.providers.gitlab.target_resolver import GitLabResource, GitLabTargetResolver
from anvil.results import ExecutionStatus


@dataclass(frozen=True, slots=True)
class GitLabPreflightData:
    """GitLab provider-owned state resolved before scheduler admission."""

    settings: GitLabAuthSettings
    resources: list[GitLabResource]


@dataclass(frozen=True, slots=True)
class GitLabExecutionTargetData:
    """GitLab-specific state required to construct one target runtime."""

    resource_id: int
    resource_type: str
    settings: GitLabAuthSettings
    session_factory: GitLabSessionFactory


class GitLabExecutionRuntime:
    """Lifecycle adapter for one GitLab group or project target."""

    def __init__(self, *, data: GitLabExecutionTargetData) -> None:
        """Initialize a runtime from provider-owned execution data."""

        self._data = data
        self._sessions: list[GitLabSession] = []

    def build_session(self, *, region: str) -> GitLabSession:
        """Build the target's global GitLab task session."""

        if region != "global":
            raise ValueError(f"GitLab runtime requires region 'global', got '{region}'")
        session = self._data.session_factory.create_session(
            target_id=self._data.resource_id,
            target_type=self._data.resource_type,
            region_name=region,
            settings=self._data.settings,
        )
        self._sessions.append(session)
        return session

    def record_region_outcome(
        self, *, region: str, duration_seconds: float, failed: bool, interrupted: bool
    ) -> None:
        """GitLab currently has no adaptive global-location lifecycle state."""

    def close(self) -> None:
        """Close every client session created by this execution runtime."""

        for session in self._sessions:
            self._data.session_factory.close_client(session.client)
        self._sessions.clear()


class GitLabProvider:
    """GitLab provider for group and project execution targets."""

    metadata = ProviderMetadata(
        name="gitlab",
        display_name="GitLab",
        description="GitLab provider",
        default_regions=DEFAULT_REGIONS,
        supported_task_scopes=frozenset({"region", "target"}),
    )

    def __init__(
        self,
        *,
        session_factory: GitLabSessionFactory | None = None,
        target_resolver: GitLabTargetResolver | None = None,
        profile_config: ProviderProfileConfig | None = None,
    ) -> None:
        """Initialize provider collaborators with injectable test seams."""

        self._session_factory = session_factory or GitLabSessionFactory()
        self._target_resolver = target_resolver or GitLabTargetResolver(
            session_factory=self._session_factory
        )
        self._profile_resolver = ProviderProfileResolver(
            provider_name=self.metadata.name,
            profile_options=GITLAB_PROFILE_OPTIONS,
            config=profile_config,
        )

    def validate_target(self, target: TargetDescriptor) -> None:
        """Validate GitLab modes, instance options, selectors, and locations."""

        if target.provider != self.metadata.name:
            raise ValueError("GitLab provider supports provider 'gitlab' targets only")
        if target.mode not in SUPPORTED_MODES:
            raise ValueError(f"Unsupported GitLab target mode: {target.mode}")
        validate_string_options(target=target, allowed_options=SUPPORTED_OPTIONS)
        validate_region_selectors(target=target, selectors_allowed=False)
        if target.regions is not None and target.regions != ["global"]:
            raise ValueError("GitLab targets support only region 'global'")
        if target.include is not None and target.exclude is not None:
            raise ValueError(
                "GitLab include and exclude filters are mutually exclusive"
            )
        resolved_target = self._resolved_target(target)
        if gitlab_option(resolved_target, "token_env") is None:
            raise ValueError("GitLab provider.options.token_env is required")

        auth_type = gitlab_option(resolved_target, "auth_type") or "private"
        if auth_type not in {"private", "oauth"}:
            raise ValueError(
                f"Unsupported GitLab auth_type '{auth_type}'. Supported values: "
                "oauth, private"
            )
        normalize_gitlab_url(gitlab_option(resolved_target, "url"))

        for selector in [*(target.include or []), *(target.exclude or [])]:
            if selector.isdigit() and int(selector) <= 0:
                raise ValueError(f"Invalid GitLab resource ID: {selector}")

    def resolve_target_filters(
        self,
        *,
        target: TargetDescriptor,
        include_override: list[str] | None,
        exclude_override: list[str] | None,
    ) -> tuple[list[str] | None, list[str] | None]:
        """Apply discovery filters or narrow explicit GitLab selections."""

        if target.include is not None:
            if exclude_override is not None:
                raise ValueError(
                    f"GitLab mode '{target.mode}' with configured include does not "
                    "allow --exclude"
                )
            include = narrow_include(
                configured=target.include, override=include_override
            )
            exclude = None
        else:
            include = include_override
            exclude = (
                exclude_override if exclude_override is not None else target.exclude
            )

        self.validate_target(replace(target, include=include, exclude=exclude))
        return include, exclude

    def auth_cache_key(self, target: TargetDescriptor) -> object | None:
        """Return an instance and credential-sensitive authentication cache key."""

        settings = resolve_auth_settings(
            target=self._resolved_target(target), require_token=False
        )
        return (self.metadata.name, settings.cache_identity())

    def auth_check(self, target: TargetDescriptor) -> ProviderAuthResult:
        """Validate GitLab credentials with a live authenticated API request."""

        self.validate_target(target)
        settings: GitLabAuthSettings | None = None
        try:
            settings = resolve_auth_settings(
                target=self._resolved_target(target), require_token=True
            )
            self._session_factory.validate_auth(settings=settings)
        except RuntimeError as error:
            message = (
                settings.redact(str(error)) if settings is not None else str(error)
            )
            return ProviderAuthResult(
                status=ExecutionStatus.ERROR,
                source="gitlab",
                message=message,
                remediation=(
                    GITLAB_EXTRA_REMEDIATION
                    if "python-gitlab" in message
                    else GITLAB_AUTH_REMEDIATION
                ),
            )
        return ProviderAuthResult(
            status=ExecutionStatus.SUCCESS,
            source=settings.source,
            message=f"GitLab authentication validated for '{settings.url}'.",
        )

    def discover_regions(self, target: TargetDescriptor) -> list[ProviderRegion]:
        """Return the configured/default global GitLab location."""

        self.validate_target(target)
        return [
            ProviderRegion(name=region, available=True, status="configured")
            for region in configured_or_default_regions(
                configured=target.regions, default=self.metadata.default_regions
            )
        ]

    def prepare_target(
        self,
        *,
        target: TargetDescriptor,
        context: ExecutionContext,
        include: list[str] | None,
        exclude: list[str] | None,
        cache: ProviderPreparationCache,
        benchmark: dict[str, object] | None,
    ) -> ProviderPreparation:
        """Resolve GitLab resources before scheduler admission."""

        self.validate_target(target)
        settings = resolve_auth_settings(
            target=self._resolved_target(target), require_token=True
        )
        recorder = BenchmarkRecorder(data=benchmark)
        with recorder.phase(f"gitlab_resolve_{target.mode}_seconds"):
            resources = self._target_resolver.resolve(
                mode=target.mode,
                include=include,
                exclude=exclude,
                settings=settings,
                cache=cache,
            )
        resources = sorted(resources, key=lambda resource: resource.id)
        recorder.set("gitlab_selected_resource_count", len(resources))

        return ProviderPreparation(
            data=GitLabPreflightData(settings=settings, resources=resources),
            exclusive_execution_keys=tuple(
                (self.metadata.name, settings.url, resource.type, resource.id)
                for resource in resources
            ),
        )

    def resolve_execution_targets(
        self,
        *,
        target: TargetDescriptor,
        regions: list[str],
        include: list[str] | None,
        exclude: list[str] | None,
        preparation: object | None = None,
    ) -> ProviderExecutionPlan:
        """Adapt prepared GitLab resources into deterministic execution targets."""

        self.validate_target(target)
        if not isinstance(preparation, GitLabPreflightData):
            raise TypeError("GitLab preparation must be GitLabPreflightData")
        if regions != ["global"]:
            raise ValueError("GitLab execution targets require region 'global'")

        execution_targets = [
            self._execution_target(
                resource=resource, regions=regions, settings=preparation.settings
            )
            for resource in sorted(preparation.resources, key=lambda item: item.id)
        ]
        return ProviderExecutionPlan(execution_targets=execution_targets)

    def prepare_execution_runtime(
        self,
        *,
        target: TargetDescriptor,
        execution_target: ExecutionTarget,
        context: ExecutionContext,
    ) -> ProviderExecutionRuntime:
        """Prepare one GitLab group or project execution runtime."""

        self.validate_target(target)
        if execution_target.provider != self.metadata.name:
            raise ValueError(
                f"Execution target provider '{execution_target.provider}' is not gitlab"
            )
        if not isinstance(execution_target.provider_data, GitLabExecutionTargetData):
            raise TypeError(
                "GitLab execution target is missing GitLabExecutionTargetData"
            )
        return GitLabExecutionRuntime(data=execution_target.provider_data)

    def _resolved_target(self, target: TargetDescriptor) -> TargetDescriptor:
        """Return a GitLab target with any Anvil profile expanded."""

        return replace(
            target,
            provider_options=self._profile_resolver.resolve(target.provider_options),
        )

    def _execution_target(
        self,
        *,
        resource: GitLabResource,
        regions: list[str],
        settings: GitLabAuthSettings,
    ) -> ExecutionTarget:
        """Build one provider-neutral target from canonical GitLab identity."""

        data = GitLabExecutionTargetData(
            resource_id=resource.id,
            resource_type=resource.type,
            settings=settings,
            session_factory=self._session_factory,
        )
        return ExecutionTarget(
            id=str(resource.id),
            name=resource.full_path,
            type=resource.type,
            provider=self.metadata.name,
            regions=list(regions),
            metadata={
                "gitlab_instance_url": settings.url,
                "gitlab_id": resource.id,
                "gitlab_path": resource.full_path,
                **resource.metadata,
            },
            provider_data=data,
        )


def create_provider_instance() -> GitLabProvider:
    """Create the first-party GitLab provider."""

    return GitLabProvider()
