from __future__ import annotations

from dataclasses import dataclass, replace

from boto3.session import Session

from anvil.benchmark import BenchmarkRecorder
from anvil.descriptors import TargetDescriptor
from anvil.execution_context import ExecutionContext
from anvil.providers.aws.account import (
    Account,
    AccountAccessStrategy,
    _AssumedCredentialState,
)
from anvil.providers.aws.account_resolver import AccountResolver
from anvil.providers.aws.auth import auth_check, infer_auth_source
from anvil.providers.aws.config import DEFAULT_ORGANIZATION_ROLE_NAME, aws_option
from anvil.providers.aws.organization import OrganizationResolver
from anvil.providers.base import (
    ExecutionTarget,
    ProviderAuthResult,
    ProviderExecutionPlan,
    ProviderPreparation,
    ProviderPreparationCache,
    ProviderExecutionRuntime,
    ProviderMetadata,
    ProviderRegion,
    narrow_include,
    validate_region_selectors,
    validate_string_options,
)
from anvil.providers.aws.regions import AwsRegionService
from anvil.providers.aws.session import CachedClientSession, SessionFactory

DEFAULT_REGIONS = ("us-east-1",)
MODE_ORGANIZATION = "organization"
MODE_ACCOUNTS = "accounts"
SUPPORTED_MODES = frozenset({MODE_ORGANIZATION, MODE_ACCOUNTS})
SUPPORTED_OPTIONS = frozenset({"profile", "role_name"})


@dataclass(frozen=True, slots=True)
class AwsExecutionTargetData:
    """AWS-specific data needed to prepare one account runtime."""

    account_id: str
    account_alias: str
    is_management: bool
    access_strategy: AccountAccessStrategy
    role_name: str | None
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
        name="aws",
        display_name="AWS",
        description="Amazon Web Services provider",
        default_regions=DEFAULT_REGIONS,
        supported_task_scopes=frozenset({"configured_target", "region"}),
    )

    def __init__(self, *, region_service: AwsRegionService | None = None) -> None:
        self._region_service = region_service or AwsRegionService()

    def validate_target(self, target: TargetDescriptor) -> None:
        """Validate an AWS target descriptor."""
        if target.provider != self.metadata.name:
            raise ValueError("AWS provider supports provider 'aws' targets only")
        if target.mode not in SUPPORTED_MODES:
            raise ValueError(f"Unsupported AWS target mode: {target.mode}")
        validate_string_options(target=target, allowed_options=SUPPORTED_OPTIONS)
        validate_region_selectors(
            target=target, selectors_allowed=target.mode == MODE_ORGANIZATION
        )
        if target.include is not None and target.exclude is not None:
            raise ValueError("AWS include and exclude filters are mutually exclusive")
        for account_id in [*(target.include or []), *(target.exclude or [])]:
            if len(account_id) != 12 or not account_id.isdigit():
                raise ValueError(f"Invalid AWS account ID: {account_id}")
        if target.mode == MODE_ACCOUNTS:
            if not target.include:
                raise ValueError("AWS mode 'accounts' requires include")
            if target.exclude is not None:
                raise ValueError("AWS mode 'accounts' does not allow exclude")
            if aws_option(target, "role_name") is None and len(target.include) != 1:
                raise ValueError(
                    "AWS accounts targets without role_name must include exactly "
                    "one account ID"
                )

    def resolve_target_filters(
        self,
        *,
        target: TargetDescriptor,
        include_override: list[str] | None,
        exclude_override: list[str] | None,
    ) -> tuple[list[str] | None, list[str] | None]:
        """Apply discovery overrides or narrow explicit AWS account targets."""

        if target.mode == MODE_ORGANIZATION:
            include = (
                include_override if include_override is not None else target.include
            )
            exclude = (
                exclude_override if exclude_override is not None else target.exclude
            )
        else:
            if exclude_override is not None:
                raise ValueError("AWS mode 'accounts' does not allow --exclude")
            include = narrow_include(
                configured=target.include, override=include_override
            )
            exclude = None

        effective_target = replace(target, include=include, exclude=exclude)
        self.validate_target(effective_target)
        return include, exclude

    def validate_task_configuration(
        self, *, target: TargetDescriptor, task_scopes: dict[str, str]
    ) -> None:
        """Validate AWS configured-target ownership before authentication."""

        configured_task_ids = [
            task_id
            for task_id, scope in task_scopes.items()
            if scope == "configured_target"
        ]
        if not configured_task_ids or target.mode == MODE_ORGANIZATION:
            return

        account_ids = target.include or []
        if len(account_ids) != 1:
            task_display = ", ".join(configured_task_ids)
            raise ValueError(
                f"AWS target '{target.name}' has ambiguous configured-target "
                f"identity for task(s) {task_display}: accounts mode requires "
                "exactly one explicit account"
            )

    def bootstrap_region(self, *, configured_regions: list[str]) -> str:
        """Return the concrete AWS region used for discovery calls."""

        return self._region_service.bootstrap_region(
            configured_regions=configured_regions
        )

    def discover_region_statuses(self, *, session: Session) -> dict[str, str]:
        """Discover AWS region statuses using an existing session."""

        return self._region_service.discover_region_statuses(session=session)

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

        profile = aws_option(target, "profile")
        auth_source = infer_auth_source(profile)
        return (self.metadata.name, profile, auth_source.value)

    def auth_check(self, target: TargetDescriptor) -> ProviderAuthResult:
        """Run the existing AWS auth check and adapt its result."""

        self.validate_target(target)
        profile = aws_option(target, "profile")
        auth_source = infer_auth_source(profile)
        result = auth_check(
            target_name=target.name, profile=profile, auth_source=auth_source
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
            profile_name=aws_option(target, "profile"),
            region_name=self.bootstrap_region(
                configured_regions=target.regions or list(self.metadata.default_regions)
            ),
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
        preparation: object | None = None,
        organization_resolver_cls: type[OrganizationResolver] = OrganizationResolver,
        account_resolver_cls: type[AccountResolver] = AccountResolver,
    ) -> ProviderExecutionPlan:
        """Resolve existing AWS account objects into provider-neutral targets."""

        self.validate_target(target)
        if preparation is not None and not isinstance(preparation, AwsPreflightData):
            raise TypeError("AWS preparation must be AwsPreflightData")
        preflight_data = preparation
        effective_target = replace(target, include=include, exclude=exclude)
        context = ExecutionContext(
            regions=regions,
            dry_run=effective_target.dry_run,
            tasks=[],
            metadata=effective_target.metadata,
            fail_fast=effective_target.fail_fast,
            max_parallel_regions=effective_target.max_parallel_regions,
        )
        resolved_session_factory = (
            preflight_data.session_factory if preflight_data else SessionFactory()
        )

        if effective_target.mode == MODE_ORGANIZATION:
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
        else:
            resolver = account_resolver_cls(
                descriptor=effective_target,
                context=context,
                session_factory=resolved_session_factory,
            )
        accounts = resolver.resolve_accounts()
        execution_targets = [
            _execution_target_from_account(
                account=account, provider_name=self.metadata.name
            )
            for account in accounts
        ]

        configured_target: ExecutionTarget | None = None
        if effective_target.mode == MODE_ORGANIZATION and preflight_data is not None:
            configured_target = self._organization_configured_target(
                target=effective_target,
                context=context,
                preflight_data=preflight_data,
                effective_regions=(
                    list(execution_targets[0].regions) if execution_targets else None
                ),
            )
        elif effective_target.mode == MODE_ACCOUNTS and len(execution_targets) == 1:
            configured_target = replace(execution_targets[0], type="configured_target")

        return ProviderExecutionPlan(
            execution_targets=execution_targets, configured_target=configured_target
        )

    def prepare_target(
        self,
        *,
        target: TargetDescriptor,
        context: ExecutionContext,
        include: list[str] | None,
        exclude: list[str] | None,
        cache: ProviderPreparationCache,
        benchmark: dict[str, object] | None,
        organization_resolver_cls: type[OrganizationResolver] = OrganizationResolver,
    ) -> ProviderPreparation:
        """Discover AWS organization execution data before target execution."""

        self.validate_target(target)
        if target.mode != MODE_ORGANIZATION:
            return ProviderPreparation()

        sink = BenchmarkRecorder(data=benchmark)
        session_factory = SessionFactory()
        with sink.phase("create_base_session_seconds"):
            base_session = session_factory.create_base_session(
                profile_name=aws_option(target, "profile"),
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

        cached_entry, cache_hit, cache_waited = cache.get_or_create(
            key=(self.metadata.name, "organization", organization_id),
            create=discover_organization,
        )
        if not isinstance(cached_entry, AwsOrganizationPreflightCacheEntry):
            raise RuntimeError("AWS organization cache returned unexpected value")

        sink.set("organization_cache_hit", cache_hit)
        sink.set("organization_cache_waited", cache_waited)

        preflight_data = AwsPreflightData(
            session_factory=session_factory,
            base_session=base_session,
            organization_id=organization_id,
            management_account_id=cached_entry.management_account_id,
            base_session_account_id=base_session_account_id,
            discovered_accounts=cached_entry.discovered_accounts,
            region_statuses=cached_entry.region_statuses,
        )
        return ProviderPreparation(
            data=preflight_data, exclusive_execution_keys=(organization_id,)
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

    def prepare_configured_target_runtime(
        self,
        *,
        target: TargetDescriptor,
        execution_target: ExecutionTarget,
        context: ExecutionContext,
    ) -> ProviderExecutionRuntime:
        """Prepare an AWS runtime for the provider-owned configured identity."""

        if execution_target.type != "configured_target":
            raise ValueError(
                "AWS configured-target runtime requires execution target type "
                "'configured_target'"
            )
        return self.prepare_execution_runtime(
            target=target, execution_target=execution_target, context=context
        )

    def _organization_configured_target(
        self,
        *,
        target: TargetDescriptor,
        context: ExecutionContext,
        preflight_data: AwsPreflightData,
        effective_regions: list[str] | None,
    ) -> ExecutionTarget:
        """Build the management-account identity independently of entity filters."""

        management_info = preflight_data.discovered_accounts.get(
            preflight_data.management_account_id
        )
        if management_info is None:
            raise ValueError(
                f"AWS organization target '{target.name}' management account "
                f"'{preflight_data.management_account_id}' was not present in "
                "discovered organization accounts"
            )

        resolved_regions = effective_regions or self.resolve_regions(
            target_name=target.name,
            configured_regions=context.regions,
            region_statuses=preflight_data.region_statuses,
        )
        if not resolved_regions:
            raise ValueError("No effective configured regions remain after validation.")

        access_strategy = (
            AccountAccessStrategy.BASE_SESSION
            if preflight_data.base_session_account_id
            == preflight_data.management_account_id
            else AccountAccessStrategy.ASSUME_ROLE
        )
        management_account = Account(
            account_id=preflight_data.management_account_id,
            account_alias=management_info["account_alias"],
            is_management=True,
            access_strategy=access_strategy,
            role_name=(
                aws_option(target, "role_name") or DEFAULT_ORGANIZATION_ROLE_NAME
            ),
            base_session=preflight_data.base_session,
            context=context,
            regions=resolved_regions,
            session_factory=preflight_data.session_factory,
        )
        return replace(
            _execution_target_from_account(
                account=management_account, provider_name=self.metadata.name
            ),
            type="configured_target",
        )

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
            role_name=data.role_name,
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
        role_name=account.role_name,
        base_session=account._base_session,
        regions=list(account._regions),
        session_factory=account._session_factory,
    )
    return ExecutionTarget(
        id=account.account_id,
        name=account.account_alias,
        type="account",
        provider=provider_name,
        regions=list(account._regions),
        metadata={
            "account_id": account.account_id,
            "account_alias": account.account_alias,
            "is_management": account.is_management,
            "access_strategy": account.access_strategy.value,
        },
        provider_data=data,
    )


def create_provider_instance() -> AwsProvider:
    """Create the first-party AWS provider."""

    return AwsProvider()
