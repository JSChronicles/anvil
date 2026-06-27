from __future__ import annotations

from dataclasses import dataclass, replace

from boto3.session import Session

from anvil.account import Account, AccountAccessStrategy, _AssumedCredentialState
from anvil.account_resolver import AccountResolver
from anvil.auth import auth_check, infer_auth_source
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


class AwsExecutionRuntime:
    """AWS account runtime adapter around the v0.29.2 account lifecycle."""

    def __init__(self, *, account: Account) -> None:
        self._account = account
        self._assumed_credential_state: _AssumedCredentialState | None = None

        if account.access_strategy is AccountAccessStrategy.ASSUME_ROLE:
            self._assumed_credential_state = _AssumedCredentialState(
                credentials=account._get_assumed_role_credentials()
            )
        else:
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

    def close(self) -> None:
        """AWS runtime currently has no explicit resources to release."""


class AwsProvider:
    """AWS provider adapter for existing organization/account target shapes."""

    metadata = ProviderMetadata(
        name="aws", display_name="AWS", description="Amazon Web Services provider"
    )

    def __init__(self, *, region_service: AwsRegionService | None = None) -> None:
        self._region_service = region_service or AwsRegionService()

    def validate_target(self, target: TargetDescriptor) -> None:
        """Validate that the target is one of the existing AWS config branches."""

        if target.config_branch not in {
            ConfigBranch.ORGANIZATIONS,
            ConfigBranch.ACCOUNTS,
        }:
            raise ValueError(f"Unsupported AWS target branch: {target.config_branch}")

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
        session_factory: SessionFactory | None = None,
        base_session: Session | None = None,
        organization_id: str | None = None,
        management_account_id: str | None = None,
        base_session_account_id: str | None = None,
        discovered_accounts: dict[str, dict[str, str]] | None = None,
        region_statuses: dict[str, str] | None = None,
        organization_resolver_cls: type[OrganizationResolver] = OrganizationResolver,
        account_resolver_cls: type[AccountResolver] = AccountResolver,
    ) -> ProviderExecutionPlan:
        """Resolve existing AWS account objects into provider-neutral targets.

        Extra keyword-only parameters are temporary AWS compatibility adapter
        inputs for the v0.29.2 runner path while provider dispatch is completed.
        """

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
        resolved_session_factory = session_factory or SessionFactory()

        if effective_target.is_organization_config:
            resolver = organization_resolver_cls(
                descriptor=effective_target,
                context=context,
                management_account_id=management_account_id,
                base_session_account_id=base_session_account_id,
                session_factory=resolved_session_factory,
                base_session=base_session,
                discovered_accounts=discovered_accounts,
                region_statuses=region_statuses,
            )
            exclusive_execution_key = organization_id
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

    def accounts_from_execution_targets(
        self, *, execution_targets: list[ExecutionTarget], context: ExecutionContext
    ) -> list[Account]:
        """Adapt AWS provider execution targets back to current account objects."""

        return [
            self._account_from_execution_target(
                execution_target=execution_target, context=context
            )
            for execution_target in execution_targets
        ]

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
