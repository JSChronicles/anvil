from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from anvil.regions import ALL_REGION_SELECTOR, is_region_selector


PROVIDER_AWS = "aws"
PROVIDER_AZURE = "azure"
PROVIDER_GCP = "gcp"
PROVIDER_GITHUB = "github"

MODE_AWS_ORGANIZATION = "organization"
MODE_AWS_ACCOUNTS = "accounts"
MODE_AZURE_TENANT = "tenant"
MODE_AZURE_SUBSCRIPTIONS = "subscriptions"
MODE_GCP_ORGANIZATION = "organization"
MODE_GCP_PROJECTS = "projects"
MODE_GITHUB_ORGANIZATIONS = "organizations"
MODE_GITHUB_REPOSITORIES = "repositories"

SUPPORTED_PROVIDERS = {PROVIDER_AWS, PROVIDER_AZURE, PROVIDER_GCP, PROVIDER_GITHUB}
SUPPORTED_PROVIDER_MODES = {
    PROVIDER_AWS: {MODE_AWS_ORGANIZATION, MODE_AWS_ACCOUNTS},
    PROVIDER_AZURE: {MODE_AZURE_TENANT, MODE_AZURE_SUBSCRIPTIONS},
    PROVIDER_GCP: {MODE_GCP_ORGANIZATION, MODE_GCP_PROJECTS},
    PROVIDER_GITHUB: {MODE_GITHUB_ORGANIZATIONS, MODE_GITHUB_REPOSITORIES},
}
SUPPORTED_PROVIDER_OPTIONS = {
    PROVIDER_AWS: {"profile", "role_name"},
    PROVIDER_AZURE: {"tenant_id", "client_id", "client_secret"},
    PROVIDER_GCP: {"credentials_path", "organization_id", "quota_project_id"},
    PROVIDER_GITHUB: {
        "api_url",
        "api_version",
        "token_env",
        "app_id",
        "private_key_env",
        "private_key_path",
        "profile",
    },
}


class ConfigBranch(StrEnum):
    TARGETS = "targets"


@dataclass(frozen=True, slots=True)
class TargetDescriptor:
    """
    Declarative description of one execution target group loaded from config.

    Public configs load from schema_version: 2 top-level targets.
    """

    config_branch: ConfigBranch
    name: str
    profile: str | None = None
    regions: list[str] = field(default_factory=lambda: ["us-east-1"])
    role_name: str | None = None
    tasks: list[dict[str, object]] = field(default_factory=lambda: [{"name": "noop"}])
    post_run: list[dict[str, object]] = field(default_factory=list)

    max_workers: int = 10
    max_parallel_regions: int = 1
    fail_fast: bool = False
    dry_run: bool = False

    include: list[str] | None = None
    exclude: list[str] | None = None

    metadata: dict[str, object] = field(default_factory=dict)
    provider: str = PROVIDER_AWS
    mode: str | None = None
    provider_options: dict[str, object] = field(default_factory=dict)

    @property
    def is_organization_config(self) -> bool:
        return self.provider == PROVIDER_AWS and self.mode == MODE_AWS_ORGANIZATION

    @property
    def is_accounts_config(self) -> bool:
        return (
            self.provider == PROVIDER_AWS
            and self.mode == MODE_AWS_ACCOUNTS
            or self.provider == PROVIDER_AZURE
            and self.mode == MODE_AZURE_SUBSCRIPTIONS
            or self.provider == PROVIDER_GCP
            and self.mode == MODE_GCP_PROJECTS
            or self.provider == PROVIDER_GITHUB
            and self.mode == MODE_GITHUB_REPOSITORIES
        )

    @property
    def is_discovery_mode(self) -> bool:
        return (
            self.provider == PROVIDER_AWS
            and self.mode == MODE_AWS_ORGANIZATION
            or self.provider == PROVIDER_AZURE
            and self.mode == MODE_AZURE_TENANT
            or self.provider == PROVIDER_GCP
            and self.mode == MODE_GCP_ORGANIZATION
            or self.provider == PROVIDER_GITHUB
            and self.mode == MODE_GITHUB_ORGANIZATIONS
        )

    @property
    def is_explicit_mode(self) -> bool:
        return not self.is_discovery_mode

    @property
    def allows_region_selectors(self) -> bool:
        """Return whether this target can expand region/location selectors."""

        return (
            self.provider == PROVIDER_AWS
            and self.mode == MODE_AWS_ORGANIZATION
            or self.provider == PROVIDER_AZURE
            and self.mode in {MODE_AZURE_TENANT, MODE_AZURE_SUBSCRIPTIONS}
            or self.provider == PROVIDER_GCP
            and self.mode == MODE_GCP_PROJECTS
        )

    def __post_init__(self) -> None:
        if not isinstance(self.provider, str):
            raise ValueError("provider must be a string")

        normalized_provider = self.provider.strip().lower()
        if normalized_provider not in SUPPORTED_PROVIDERS:
            supported = ", ".join(sorted(SUPPORTED_PROVIDERS))
            raise ValueError(
                f"Unsupported provider '{self.provider}'. Supported providers: {supported}"
            )
        object.__setattr__(self, "provider", normalized_provider)

        self._validate_provider_options()
        self._normalize_provider_option_aliases()
        self._normalize_mode()

        if self.max_workers < 1:
            raise ValueError("max_workers must be >= 1")

        normalized_post_run = self._normalize_post_run(self.post_run)
        object.__setattr__(self, "post_run", normalized_post_run)

        if not 1 <= self.max_parallel_regions <= 4:
            raise ValueError("max_parallel_regions must be between 1 and 4")

        if not self.regions:
            raise ValueError("regions must contain at least one region")

        normalized_regions = [region.strip() for region in self.regions]
        if any(not region for region in normalized_regions):
            raise ValueError("regions must not contain empty values")

        if len(set(normalized_regions)) != len(normalized_regions):
            raise ValueError("regions must not contain duplicates")

        if ALL_REGION_SELECTOR in normalized_regions and normalized_regions != [
            ALL_REGION_SELECTOR
        ]:
            raise ValueError("regions selector 'all' must be the only region value")

        object.__setattr__(self, "regions", normalized_regions)

        normalized_include = self._normalize_target_ids(self.include)
        normalized_exclude = self._normalize_target_ids(self.exclude)

        object.__setattr__(self, "include", normalized_include)
        object.__setattr__(self, "exclude", normalized_exclude)

        if self.config_branch is ConfigBranch.TARGETS:
            if self.include is not None and self.exclude is not None:
                raise ValueError("include and exclude cannot both be set")

            allows_provider_discovery = (
                self.provider == PROVIDER_GCP and self.mode == MODE_GCP_PROJECTS
            )
            if self.is_explicit_mode and not allows_provider_discovery:
                if not self.include:
                    raise ValueError(
                        f"provider '{self.provider}' mode '{self.mode}' requires include"
                    )
                if self.exclude is not None:
                    raise ValueError(
                        f"provider '{self.provider}' mode '{self.mode}' does not allow exclude"
                    )

            if (
                self.provider == PROVIDER_AWS
                and self.mode == MODE_AWS_ACCOUNTS
                and self.role_name is None
                and len(self.include or []) != 1
            ):
                raise ValueError(
                    "AWS accounts targets without role_name must include exactly "
                    "one account ID"
                )

            if not self.allows_region_selectors:
                target_region_selectors = [
                    region for region in self.regions if is_region_selector(region)
                ]
                if target_region_selectors:
                    raise ValueError(
                        f"provider '{self.provider}' mode '{self.mode}' requires "
                        "explicit region names; selectors are not allowed: "
                        f"{', '.join(target_region_selectors)}"
                    )

            return

        raise ValueError(f"Unsupported config branch: {self.config_branch}")

    def _validate_provider_options(self) -> None:
        if not isinstance(self.provider_options, dict):
            raise ValueError("provider.options must be a mapping")

        if self.provider != PROVIDER_AWS:
            if self.profile is not None:
                raise ValueError(
                    f"profile is only supported for provider '{PROVIDER_AWS}'"
                )
            if self.role_name is not None:
                raise ValueError(
                    f"role_name is only supported for provider '{PROVIDER_AWS}'"
                )

        allowed_options = SUPPORTED_PROVIDER_OPTIONS[self.provider]
        unknown_options = sorted(set(self.provider_options) - allowed_options)
        if unknown_options:
            unknown_display = ", ".join(unknown_options)
            allowed_display = ", ".join(sorted(allowed_options)) or "(none)"
            raise ValueError(
                f"Unsupported provider.options for provider '{self.provider}': "
                f"{unknown_display}. Supported options: {allowed_display}"
            )

        for option_name, option_value in self.provider_options.items():
            if option_value is None:
                continue
            if not isinstance(option_value, str) or not option_value.strip():
                raise ValueError(
                    f"provider.options.{option_name} must be a non-empty string"
                )

        if self.provider == PROVIDER_AZURE:
            tenant_id = self.provider_options.get("tenant_id")
            client_id = self.provider_options.get("client_id")
            client_secret = self.provider_options.get("client_secret")
            if tenant_id is not None and client_secret is None:
                raise ValueError(
                    "Azure provider.options.tenant_id is only supported with "
                    "client_secret"
                )
            if client_secret is not None and (tenant_id is None or client_id is None):
                raise ValueError(
                    "Azure provider.options.client_secret requires tenant_id and "
                    "client_id"
                )

        if self.provider == PROVIDER_GITHUB:
            profile = self.provider_options.get("profile")
            if profile is not None and len(self.provider_options) > 1:
                raise ValueError(
                    "GitHub provider.options.profile cannot be combined with "
                    "inline GitHub auth options"
                )

    def _normalize_provider_option_aliases(self) -> None:
        if self.provider != PROVIDER_AWS:
            return

        profile = self.provider_options.get("profile")
        if profile is not None:
            if not isinstance(profile, str) or not profile.strip():
                raise ValueError("provider.options.profile must be a non-empty string")
            if self.profile is not None and self.profile != profile:
                raise ValueError(
                    "profile and provider.options.profile must not conflict"
                )
            object.__setattr__(self, "profile", profile.strip())

        role_name = self.provider_options.get("role_name")
        if role_name is not None:
            if not isinstance(role_name, str) or not role_name.strip():
                raise ValueError(
                    "provider.options.role_name must be a non-empty string"
                )
            if self.role_name is not None and self.role_name != role_name:
                raise ValueError(
                    "role_name and provider.options.role_name must not conflict"
                )
            object.__setattr__(self, "role_name", role_name.strip())

    def _normalize_mode(self) -> None:
        mode = self.mode.strip().lower() if isinstance(self.mode, str) else None
        if self.mode is not None and not mode:
            raise ValueError("mode must be a non-empty string")

        if mode is None:
            if self.provider == PROVIDER_AWS:
                mode = (
                    MODE_AWS_ACCOUNTS
                    if self.include is not None
                    else MODE_AWS_ORGANIZATION
                )
            elif self.provider == PROVIDER_AZURE:
                mode = MODE_AZURE_SUBSCRIPTIONS
            elif self.provider == PROVIDER_GCP:
                mode = MODE_GCP_PROJECTS
            elif self.provider == PROVIDER_GITHUB:
                mode = MODE_GITHUB_REPOSITORIES

        if mode not in SUPPORTED_PROVIDER_MODES[self.provider]:
            supported = ", ".join(sorted(SUPPORTED_PROVIDER_MODES[self.provider]))
            raise ValueError(
                f"Unsupported mode '{mode}' for provider '{self.provider}'. "
                f"Supported modes: {supported}"
            )

        object.__setattr__(self, "mode", mode)

    @staticmethod
    def _normalize_target_ids(target_ids: list[str] | None) -> list[str] | None:
        if target_ids is None:
            return None

        normalized = [target_id.strip() for target_id in target_ids]

        if any(not target_id for target_id in normalized):
            raise ValueError("target ID lists must not contain empty values")

        if len(set(normalized)) != len(normalized):
            raise ValueError("target ID lists must not contain duplicates")

        return normalized

    @staticmethod
    def _normalize_post_run(
        post_run: list[dict[str, object]] | None,
    ) -> list[dict[str, object]]:
        if post_run is None:
            return []

        normalized: list[dict[str, object]] = []
        for index, raw_spec in enumerate(post_run, start=1):
            if not isinstance(raw_spec, dict):
                raise ValueError(f"post_run entry #{index} must be a mapping")

            processor = raw_spec.get("processor")
            if not isinstance(processor, str) or not processor.strip():
                raise ValueError(
                    f"post_run entry #{index} requires a non-empty processor"
                )

            output = raw_spec.get("output")
            if output is not None and not isinstance(output, str):
                raise ValueError(f"post_run entry #{index} output must be a string")

            metadata = raw_spec.get("metadata", {})
            if not isinstance(metadata, dict):
                raise ValueError(f"post_run entry #{index} metadata must be a mapping")

            run_on_failure = raw_spec.get("run_on_failure", False)
            if not isinstance(run_on_failure, bool):
                raise ValueError(
                    f"post_run entry #{index} run_on_failure must be a boolean"
                )

            normalized_spec: dict[str, object] = {
                "processor": processor.strip(),
                "metadata": dict(metadata),
            }
            if output is not None:
                normalized_spec["output"] = output
            if run_on_failure:
                normalized_spec["run_on_failure"] = run_on_failure

            normalized.append(normalized_spec)

        return normalized


@dataclass(frozen=True, slots=True)
class LoadedConfig:
    branch: ConfigBranch
    targets: list[TargetDescriptor]
    max_parallel_targets: int = 1
