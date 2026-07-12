from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Protocol

from boto3.session import Session

from anvil.account import Account, AccountAccessStrategy, _AssumedCredentialState
from anvil.account_resolver import AccountResolver
from anvil.auth import auth_check, infer_auth_source
from anvil.benchmark import BenchmarkRecorder
from anvil.descriptors import ConfigBranch, TargetDescriptor
from anvil.execution_context import ExecutionContext
from anvil.organization import OrganizationResolver
from anvil.providers.base import (
    ExecutionTarget,
    ProviderAuthResult,
    ProviderExecutionPlan,
    ProviderExecutionRuntime,
    ProviderMetadata,
    ProviderRegion,
)
from anvil.providers.aws.regions import AwsRegionService
from anvil.session import CachedClientSession, SessionFactory


@dataclass(frozen=True, slots=True)
class AwsExecutionTargetData:
    """AWS-specific data needed to prepare one account runtime."""

    account_id: str
    account_alias: str
    is_management: bool
    access_strategy: AccountAccessStrategy
    base_session: Session
    regions: list[str]
    session_factory: SessionFactory


@dataclass(frozen=True, slots=True)
class AwsOrganizationPreflightCacheEntry:
    """Cached AWS organization discovery data shared across configured targets."""

    management_account_id: str
    discovered_accounts: dict[str, dict[str, str]]
    region_statuses: dict[str, str]


@dataclass(frozen=True, slots=True)
class AwsPreflightData:
    """AWS provider-owned preflight data needed for execution target resolution."""

    session_factory: SessionFactory
    base_session: Session
    organization_id: str
    management_account_id: str
    base_session_account_id: str
    discovered_accounts: dict[str, dict[str, str]]
    region_statuses: dict[str, str]


@dataclass(frozen=True, slots=True)
class AwsPreflightResult:
    """AWS preflight result plus scheduler admission metadata."""

    data: AwsPreflightData
    exclusive_execution_key: str


@dataclass(frozen=True, slots=True)
class _AwsOrganizationCacheLookup:
    entry: object
    hit: bool
    waited: bool


class _AwsOrganizationCache(Protocol):
    def get_or_discover(
        self,
        *,
        organization_id: str,
        discover: Callable[[], AwsOrganizationPreflightCacheEntry],
    ) -> _AwsOrganizationCacheLookup:
        """Return cached or newly discovered organization data."""


class AwsExecutionRuntime:
    """AWS account runtime adapter around the v0.29.2 account lifecycle."""

    def __init__(self, *, account: Account) -> None:
        self._account = account
        self._assumed_credential_state: _AssumedCredentialState | None = None
        self._recorder = BenchmarkRecorder(enabled=account._context.benchmark_enabled)
        self._recorder.update(
            {
                "access_strategy": account.access_strategy.value,
                "assume_role_seconds": 0.0,
                "assume_role_refresh_count": 0,
                "direct_access_validation_seconds": 0.0,
            }
        )

        if account.access_strategy is AccountAccessStrategy.ASSUME_ROLE:
            with self._recorder.phase("assume_role_seconds"):
                self._assumed_credential_state = _AssumedCredentialState(
                    credentials=account._get_assumed_role_credentials()
                )
            self._sync_assume_role_benchmark()
        else:
            with self._recorder.phase("direct_access_validation_seconds"):
                account._validate_direct_account_access()

    def build_session(self, *, region: str) -> CachedClientSession:
        """Build a cached AWS client session for one account-region pair."""

        region_session = self._account._get_region_session(
            region=region, assumed_credential_state=self._assumed_credential_state
        )
        return self._account._session_factory.create_cached_client_session(
            session=region_session
        )

    def record_region_outcome(
        self, *, region: str, duration_seconds: float, failed: bool, interrupted: bool
    ) -> None:
        """Update AWS credential refresh timing after one region completes."""

        self._account._update_assumed_credential_refresh_window(
            assumed_credential_state=self._assumed_credential_state,
            region_duration_seconds=duration_seconds,
        )
        self._sync_assume_role_benchmark()

    def close(self) -> None:
        """AWS runtime currently has no explicit resources to release."""

    @property
    def benchmark(self) -> dict[str, object] | None:
        """Return AWS runtime benchmark data when benchmarking is enabled."""

        return self._recorder.data

    def _sync_assume_role_benchmark(self) -> None:
        if self._assumed_credential_state is None:
            return

        self._recorder.set(
            "assume_role_refresh_count", self._assumed_credential_state.refresh_count
        )
        self._recorder.set(
            "assume_role_refresh_window_seconds",
            self._assumed_credential_state.refresh_window.total_seconds(),
        )


class AwsProvider:
    """AWS provider adapter for existing organization/account target shapes."""

    metadata = ProviderMetadata(
        name="aws", display_name="AWS", description="Amazon Web Services provider"
    )

    def __init__(self, *, region_service: AwsRegionService | None = None) -> None:
        self._region_service = region_service or AwsRegionService()

    def validate_target(self, target: TargetDescriptor) -> None:
        """Validate that the target is one of the existing AWS config branches."""

        if target.config_branch is not ConfigBranch.TARGETS:
            raise ValueError(f"Unsupported AWS target branch: {target.config_branch}")
        if target.provider != self.metadata.name:
            raise ValueError("AWS provider supports provider 'aws' targets only")

    def default_regions(self, target: TargetDescriptor) -> list[str]:
        """Return the target's configured AWS regions."""

        self.validate_target(target)
        return self._region_service.default_regions(configured_regions=target.regions)

    def bootstrap_region(self, *, configured_regions: list[str]) -> str:
        """Return the concrete AWS region used for discovery calls."""

        return self._region_service.bootstrap_region(
            configured_regions=configured_regions
        )

    def discover_region_statuses(self, *, session: Session) -> dict[str, str]:
        """Discover AWS region statuses using an existing session."""

        # Compatibility shim: existing runner tests and callers patch
        # OrganizationResolver.discover_region_statuses. The resolver method now
        # delegates to AWS provider-owned region code, so keeping this call
        # preserves the old patch point without duplicating region logic.
        return OrganizationResolver.discover_region_statuses(session)

    def resolve_regions(
        self,
        *,
        target_name: str,
        configured_regions: list[str],
        region_statuses: dict[str, str],
    ) -> list[str]:
        """Resolve configured AWS region selectors against discovered statuses."""

        return self._region_service.resolve_regions(
            target_name=target_name,
            configured_regions=configured_regions,
            region_statuses=region_statuses,
        )

    def auth_cache_key(self, target: TargetDescriptor) -> object | None:
        """Return the same auth cache identity used by the current runner."""

        auth_source = infer_auth_source(target.profile)
        return (self.metadata.name, target.profile, auth_source.value)

    def auth_check(self, target: TargetDescriptor) -> ProviderAuthResult:
        """Run the existing AWS auth check and adapt its result."""

        auth_source = infer_auth_source(target.profile)
        result = auth_check(
            target_name=target.name, profile=target.profile, auth_source=auth_source
        )
        return ProviderAuthResult(
            status=result.status,
            source=result.source,
            message=result.message,
            remediation=result.remediation,
        )

    def discover_regions(self, target: TargetDescriptor) -> list[ProviderRegion]:
        """Discover AWS region statuses using the existing organization resolver."""

        self.validate_target(target)
        session_factory = SessionFactory()
        base_session = session_factory.create_base_session(
            profile_name=target.profile,
            region_name=self.bootstrap_region(configured_regions=target.regions),
        )
        statuses = self.discover_region_statuses(session=base_session)
        return self._region_service.provider_regions_from_statuses(
            region_statuses=statuses
        )

    def resolve_execution_targets(
        self,
        *,
        target: TargetDescriptor,
        regions: list[str],
        include: list[str] | None,
        exclude: list[str] | None,
        preflight_data: AwsPreflightData | None = None,
        organization_resolver_cls: type[OrganizationResolver] = OrganizationResolver,
        account_resolver_cls: type[AccountResolver] = AccountResolver,
    ) -> ProviderExecutionPlan:
        """Resolve existing AWS account objects into provider-neutral targets."""

        self.validate_target(target)
        effective_target = replace(target, include=include, exclude=exclude)
        context = ExecutionContext(
            regions=regions,
            role_name=effective_target.role_name,
            dry_run=effective_target.dry_run,
            tasks=[],
            metadata=effective_target.metadata,
            fail_fast=effective_target.fail_fast,
            max_parallel_regions=effective_target.max_parallel_regions,
        )
        resolved_session_factory = (
            preflight_data.session_factory if preflight_data else SessionFactory()
        )

        if effective_target.is_organization_config:
            resolver = organization_resolver_cls(
                descriptor=effective_target,
                context=context,
                management_account_id=(
                    preflight_data.management_account_id if preflight_data else None
                ),
                base_session_account_id=(
                    preflight_data.base_session_account_id if preflight_data else None
                ),
                session_factory=resolved_session_factory,
                base_session=preflight_data.base_session if preflight_data else None,
                discovered_accounts=(
                    preflight_data.discovered_accounts if preflight_data else None
                ),
                region_statuses=(
                    preflight_data.region_statuses if preflight_data else None
                ),
            )
            exclusive_execution_key = (
                preflight_data.organization_id if preflight_data else None
            )
        else:
            resolver = account_resolver_cls(
                descriptor=effective_target,
                context=context,
                session_factory=resolved_session_factory,
            )
            exclusive_execution_key = None

        accounts = resolver.resolve_accounts()
        execution_targets = [
            _execution_target_from_account(
                account=account, provider_name=self.metadata.name
            )
            for account in accounts
        ]

        return ProviderExecutionPlan(
            execution_targets=execution_targets,
            exclusive_execution_key=exclusive_execution_key,
        )

    def preflight_execution(
        self,
        *,
        target: TargetDescriptor,
        context: ExecutionContext,
        session_factory: SessionFactory,
        organization_cache: _AwsOrganizationCache,
        benchmark: dict[str, object] | None = None,
        organization_resolver_cls: type[OrganizationResolver] = OrganizationResolver,
    ) -> AwsPreflightResult:
        """Discover AWS organization execution data before target execution."""

        self.validate_target(target)
        if not target.is_organization_config:
            raise ValueError("AWS preflight requires organization mode")

        sink = BenchmarkRecorder(data=benchmark)
        with sink.phase("create_base_session_seconds"):
            base_session = session_factory.create_base_session(
                profile_name=target.profile,
                region_name=self.bootstrap_region(configured_regions=context.regions),
            )

        with sink.phase("describe_organization_seconds"):
            organization_id, management_account_id = (
                organization_resolver_cls.describe_organization(base_session)
            )

        with sink.phase("describe_base_session_account_seconds"):
            base_session_account_id = (
                organization_resolver_cls.describe_base_session_account(base_session)
            )

        def discover_organization() -> AwsOrganizationPreflightCacheEntry:
            with sink.phase("discover_accounts_seconds"):
                discovered_accounts = organization_resolver_cls.discover_accounts(
                    base_session
                )

            with sink.phase("discover_region_statuses_seconds"):
                region_statuses = self.discover_region_statuses(session=base_session)

            return AwsOrganizationPreflightCacheEntry(
                management_account_id=management_account_id,
                discovered_accounts=discovered_accounts,
                region_statuses=region_statuses,
            )

        lookup = organization_cache.get_or_discover(
            organization_id=organization_id, discover=discover_organization
        )
        if not isinstance(lookup.entry, AwsOrganizationPreflightCacheEntry):
            raise RuntimeError("AWS organization cache returned unexpected value")

        sink.set("organization_cache_hit", lookup.hit)
        sink.set("organization_cache_waited", lookup.waited)

        preflight_data = AwsPreflightData(
            session_factory=session_factory,
            base_session=base_session,
            organization_id=organization_id,
            management_account_id=lookup.entry.management_account_id,
            base_session_account_id=base_session_account_id,
            discovered_accounts=lookup.entry.discovered_accounts,
            region_statuses=lookup.entry.region_statuses,
        )
        return AwsPreflightResult(
            data=preflight_data, exclusive_execution_key=organization_id
        )

    def prepare_execution_runtime(
        self,
        *,
        target: TargetDescriptor,
        execution_target: ExecutionTarget,
        context: ExecutionContext,
    ) -> ProviderExecutionRuntime:
        """Prepare the AWS runtime for one execution target."""

        self.validate_target(target)
        if execution_target.provider != self.metadata.name:
            raise ValueError(
                f"Execution target provider '{execution_target.provider}' is not aws"
            )
        if not isinstance(execution_target.provider_data, AwsExecutionTargetData):
            raise TypeError("AWS execution target is missing AwsExecutionTargetData")

        account = self._account_from_execution_target(
            execution_target=execution_target, context=context
        )
        return AwsExecutionRuntime(account=account)

    def _account_from_execution_target(
        self, *, execution_target: ExecutionTarget, context: ExecutionContext
    ) -> Account:
        if execution_target.provider != self.metadata.name:
            raise ValueError(
                f"Execution target provider '{execution_target.provider}' is not aws"
            )
        if not isinstance(execution_target.provider_data, AwsExecutionTargetData):
            raise TypeError("AWS execution target is missing AwsExecutionTargetData")

        data = execution_target.provider_data
        return Account(
            account_id=data.account_id,
            account_alias=data.account_alias,
            is_management=data.is_management,
            access_strategy=data.access_strategy,
            base_session=data.base_session,
            context=context,
            regions=list(data.regions),
            session_factory=data.session_factory,
        )


def _execution_target_from_account(
    *, account: Account, provider_name: str
) -> ExecutionTarget:
    data = AwsExecutionTargetData(
        account_id=account.account_id,
        account_alias=account.account_alias,
        is_management=account.is_management,
        access_strategy=account.access_strategy,
        base_session=account._base_session,
        regions=list(account._regions),
        session_factory=account._session_factory,
    )
    return ExecutionTarget(
        id=account.account_id,
        name=account.account_alias,
        type="account",
        provider=provider_name,
        metadata={
            "account_id": account.account_id,
            "account_alias": account.account_alias,
            "is_management": account.is_management,
            "access_strategy": account.access_strategy.value,
        },
        provider_data=data,
    )


def create_provider() -> AwsProvider:
    """Create the first-party AWS provider."""

    return AwsProvider()
