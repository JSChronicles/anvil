from __future__ import annotations

import re
from dataclasses import dataclass

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
GITHUB_LOGIN_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
GITHUB_REPOSITORY_PATTERN = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[^/\s]+$"
)


@dataclass(frozen=True, slots=True)
class GithubExecutionTargetData:
    """GitHub-specific target identity and provider options."""

    target_id: str
    target_type: str
    provider_options: dict[str, object]


class GithubExecutionRuntime:
    """Offline GitHub runtime placeholder until GitHub tasks are introduced."""

    def __init__(self, *, data: GithubExecutionTargetData) -> None:
        self._data = data

    def build_session(self, *, region: str) -> object:
        """Raise until GitHub runtime API sessions are implemented."""

        raise RuntimeError(
            "GitHub runtime sessions are not implemented yet; no GitHub tasks are "
            "available in this provider skeleton."
        )

    def record_region_outcome(
        self, *, region: str, duration_seconds: float, failed: bool, interrupted: bool
    ) -> None:
        """GitHub runtime currently has no adaptive lifecycle state."""

    def close(self) -> None:
        """GitHub runtime currently has no explicit resources to release."""


class GithubProvider:
    """GitHub provider skeleton for explicit organization and repository targets."""

    metadata = ProviderMetadata(
        name="github", display_name="GitHub", description="GitHub provider"
    )

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
                f"GitHub mode '{target.mode}' requires include with explicit targets"
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
        """Resolve configured GitHub org or repository IDs without API calls."""

        self.validate_target(target)
        if exclude is not None:
            raise ValueError(f"GitHub mode '{target.mode}' does not allow exclude")

        target_ids = include or target.include or []
        target_type = (
            "organization"
            if target.mode == MODE_GITHUB_ORGANIZATIONS
            else "repository"
        )
        execution_targets = [
            self._execution_target(
                target_id=target_id,
                target_type=target_type,
                provider_options=target.provider_options,
            )
            for target_id in target_ids
        ]
        return ProviderExecutionPlan(execution_targets=execution_targets)

    def prepare_execution_runtime(
        self,
        *,
        target: TargetDescriptor,
        execution_target: ExecutionTarget,
        context: ExecutionContext,
    ) -> ProviderExecutionRuntime:
        """Prepare the offline GitHub runtime placeholder."""

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
        self,
        *,
        target_id: str,
        target_type: str,
        provider_options: dict[str, object],
    ) -> ExecutionTarget:
        data = GithubExecutionTargetData(
            target_id=target_id,
            target_type=target_type,
            provider_options=dict(provider_options),
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
                    "GitHub organizations mode include values must be organization "
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
