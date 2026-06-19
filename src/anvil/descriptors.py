from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from anvil.regions import ALL_REGION_SELECTOR, is_region_selector


class ConfigBranch(StrEnum):
    ORGANIZATIONS = "organizations"
    ACCOUNTS = "accounts"


@dataclass(frozen=True, slots=True)
class TargetDescriptor:
    """
    Declarative description of one execution target group loaded from config.

    A target group can come from either:
    - organizations: discover accounts from AWS Organizations
    - accounts: execute against an explicit list of account IDs
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

    @property
    def is_organization_config(self) -> bool:
        return self.config_branch is ConfigBranch.ORGANIZATIONS

    @property
    def is_accounts_config(self) -> bool:
        return self.config_branch is ConfigBranch.ACCOUNTS

    def __post_init__(self) -> None:
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

        normalized_include = self._normalize_account_ids(self.include)
        normalized_exclude = self._normalize_account_ids(self.exclude)

        object.__setattr__(self, "include", normalized_include)
        object.__setattr__(self, "exclude", normalized_exclude)

        if self.config_branch is ConfigBranch.ORGANIZATIONS:
            if self.role_name is None:
                object.__setattr__(self, "role_name", "OrganizationAccountAccessRole")

            if self.include and self.exclude:
                raise ValueError("include and exclude cannot both be set")
            return

        if self.config_branch is ConfigBranch.ACCOUNTS:
            account_region_selectors = [
                region for region in self.regions if is_region_selector(region)
            ]
            if account_region_selectors:
                raise ValueError(
                    "accounts config entries require explicit region names; "
                    f"selectors are not allowed: {', '.join(account_region_selectors)}"
                )

            if not self.include:
                raise ValueError("accounts config entries require include")

            if self.exclude is not None:
                raise ValueError("accounts config entries do not allow exclude")

            if self.role_name is None and len(self.include) != 1:
                raise ValueError(
                    "accounts config entries without role_name must include exactly "
                    "one account ID"
                )

            return

        raise ValueError(f"Unsupported config branch: {self.config_branch}")

    @staticmethod
    def _normalize_account_ids(account_ids: list[str] | None) -> list[str] | None:
        if account_ids is None:
            return None

        normalized = [account_id.strip() for account_id in account_ids]

        if any(not account_id for account_id in normalized):
            raise ValueError("account ID lists must not contain empty values")

        if len(set(normalized)) != len(normalized):
            raise ValueError("account ID lists must not contain duplicates")

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
