"""First-party Cloudflare provider implementation."""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from string import hexdigits
from typing import cast

from anvil.benchmark import BenchmarkRecorder
from anvil.descriptors import TargetDescriptor
from anvil.execution_context import ExecutionContext
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
from anvil.providers.cloudflare.auth import (
    CLOUDFLARE_CREDENTIAL_REMEDIATION,
    CloudflareAuthSettings,
    CloudflareCredentialError,
    resolve_auth_settings,
    validate_auth_options,
)
from anvil.providers.cloudflare.session import (
    CLOUDFLARE_EXTRA_REMEDIATION,
    CloudflareAccount,
    CloudflareDependencyError,
    CloudflareSession,
    CloudflareSessionFactory,
    CloudflareZone,
)
from anvil.results import ExecutionStatus

DEFAULT_REGIONS = ("global",)
MODE_ACCOUNTS = "accounts"
MODE_ZONES = "zones"
SUPPORTED_MODES = frozenset({MODE_ACCOUNTS, MODE_ZONES})
SUPPORTED_OPTIONS = frozenset(
    {"account_id", "api_token_env", "api_key_env", "api_email_env", "base_url"}
)
CLOUDFLARE_IDENTIFIER_LENGTH = 32


@dataclass(frozen=True, slots=True)
class CloudflarePreflightData:
    """Cloudflare provider-owned target data prepared before execution."""

    settings: CloudflareAuthSettings
    accounts: tuple[CloudflareAccount, ...] = ()
    zones: tuple[CloudflareZone, ...] = ()


@dataclass(frozen=True, slots=True)
class CloudflareExecutionTargetData:
    """Cloudflare-specific data required to build one target runtime."""

    target_type: str
    target_id: str
    account_id: str | None
    zone_id: str | None
    settings: CloudflareAuthSettings
    session_factory: CloudflareSessionFactory


class CloudflareExecutionRuntime:
    """Cloudflare runtime adapter for one account or zone target."""

    def __init__(
        self, *, data: CloudflareExecutionTargetData, benchmark_enabled: bool
    ) -> None:
        self._data = data
        self._session: CloudflareSession | None = None
        self._closed = False
        self._lock = threading.Lock()
        self._recorder = BenchmarkRecorder(enabled=benchmark_enabled)

    def build_session(self, *, region: str) -> CloudflareSession:
        """Build and reuse one SDK client for the global target coordinate."""

        if region != "global":
            raise ValueError(
                f"Cloudflare runtime requires region 'global', received '{region}'"
            )
        with self._lock:
            if self._closed:
                raise RuntimeError("Cloudflare runtime is already closed")
            if self._session is None:
                with self._recorder.phase("cloudflare_create_session_seconds"):
                    self._session = self._data.session_factory.create_session(
                        settings=self._data.settings,
                        target_type=self._data.target_type,
                        target_id=self._data.target_id,
                        region_name=region,
                        account_id=self._data.account_id,
                        zone_id=self._data.zone_id,
                    )
            return self._session

    def record_region_outcome(
        self, *, region: str, duration_seconds: float, failed: bool, interrupted: bool
    ) -> None:
        """Record no adaptive state for Cloudflare's global coordinate."""

    def close(self) -> None:
        """Close the SDK client created for this execution target."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            session = self._session
            self._session = None
        if session is not None:
            self._data.session_factory.close_session(session=session)

    @property
    def benchmark(self) -> dict[str, object] | None:
        """Return Cloudflare runtime benchmark data when enabled."""

        return self._recorder.data


class CloudflareProvider:
    """Cloudflare provider for account-scoped and zone-scoped execution."""

    metadata = ProviderMetadata(
        name="cloudflare",
        display_name="Cloudflare",
        description="Cloudflare provider",
        default_regions=DEFAULT_REGIONS,
        supported_task_scopes=frozenset({"region", "target"}),
    )

    def __init__(
        self, *, session_factory: CloudflareSessionFactory | None = None
    ) -> None:
        self._session_factory = session_factory or CloudflareSessionFactory()

    def validate_target(self, target: TargetDescriptor) -> None:
        """Validate Cloudflare modes, credentials, identifiers, and coordinates."""

        if target.provider != self.metadata.name:
            raise ValueError(
                "Cloudflare provider supports provider 'cloudflare' targets only"
            )
        if target.mode not in SUPPORTED_MODES:
            raise ValueError(f"Unsupported Cloudflare target mode: {target.mode}")
        validate_string_options(target=target, allowed_options=SUPPORTED_OPTIONS)
        validate_auth_options(provider_options=target.provider_options)
        validate_region_selectors(target=target, selectors_allowed=False)
        if target.regions is not None and target.regions != ["global"]:
            raise ValueError("Cloudflare targets support only regions: [global]")
        if (
            target.mode == MODE_ACCOUNTS
            and self._string_option(target=target, option_name="account_id") is not None
        ):
            raise ValueError(
                "Cloudflare provider.options.account_id is supported only in zones mode"
            )
        configured_account_id = self._string_option(
            target=target, option_name="account_id"
        )
        if configured_account_id is not None:
            self._validate_identifier(
                identifier=configured_account_id, label="provider.options.account_id"
            )
        target_label = "account" if target.mode == MODE_ACCOUNTS else "zone"
        for target_id in [*(target.include or []), *(target.exclude or [])]:
            self._validate_identifier(identifier=target_id, label=target_label)

    def resolve_target_filters(
        self,
        *,
        target: TargetDescriptor,
        include_override: list[str] | None,
        exclude_override: list[str] | None,
    ) -> tuple[list[str] | None, list[str] | None]:
        """Resolve explicit-ID narrowing or discovery exclusion filters."""

        if target.include is not None:
            if exclude_override is not None:
                raise ValueError(
                    f"Cloudflare mode '{target.mode}' with explicit include does "
                    "not allow --exclude"
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
        """Return a secret-safe authentication cache key when credentials resolve."""

        try:
            settings = resolve_auth_settings(provider_options=target.provider_options)
        except RuntimeError, ValueError:
            return None
        return (self.metadata.name, settings.cache_identity())

    def auth_check(self, target: TargetDescriptor) -> ProviderAuthResult:
        """Validate credential settings and lazy SDK client construction."""

        self.validate_target(target)
        try:
            settings = resolve_auth_settings(provider_options=target.provider_options)
            self._session_factory.validate_client(settings=settings)
        except CloudflareDependencyError as error:
            return ProviderAuthResult(
                status=ExecutionStatus.ERROR,
                source="cloudflare",
                message=str(error),
                remediation=CLOUDFLARE_EXTRA_REMEDIATION,
            )
        except CloudflareCredentialError as error:
            return ProviderAuthResult(
                status=ExecutionStatus.ERROR,
                source="cloudflare",
                message=str(error),
                remediation=CLOUDFLARE_CREDENTIAL_REMEDIATION,
            )
        except RuntimeError as error:
            return ProviderAuthResult(
                status=ExecutionStatus.ERROR, source="cloudflare", message=str(error)
            )
        return ProviderAuthResult(
            status=ExecutionStatus.SUCCESS,
            source=settings.source,
            message=(
                "Cloudflare authentication settings resolved. Resource permissions "
                "are validated during discovery and execution."
            ),
        )

    def discover_regions(self, target: TargetDescriptor) -> list[ProviderRegion]:
        """Return Cloudflare's single global control-plane coordinate."""

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
        """Resolve and cache Cloudflare account or zone discovery before execution."""

        self.validate_target(target)
        settings = resolve_auth_settings(provider_options=target.provider_options)
        recorder = BenchmarkRecorder(data=benchmark)
        cache_hit = False
        cache_waited = False

        if target.mode == MODE_ACCOUNTS:
            with recorder.phase("cloudflare_resolve_accounts_seconds"):
                if include is not None:
                    accounts = self._explicit_accounts(include)
                else:
                    cached, cache_hit, cache_waited = cache.get_or_create(
                        key=(self.metadata.name, "accounts", settings.cache_identity()),
                        create=lambda: tuple(
                            self._session_factory.list_accounts(settings=settings)
                        ),
                    )
                    accounts = self._cached_accounts(cached)
                    accounts = self._exclude_accounts(accounts, exclude=exclude)
            preflight = CloudflarePreflightData(
                settings=settings, accounts=tuple(accounts)
            )
            execution_keys = tuple(
                (self.metadata.name, "account", account.account_id)
                for account in accounts
            )
            selected_count = len(accounts)
        else:
            configured_account_id = self._string_option(
                target=target, option_name="account_id"
            )
            with recorder.phase("cloudflare_resolve_zones_seconds"):
                if include is not None:
                    zones = self._explicit_zones(
                        include, account_id=configured_account_id
                    )
                else:
                    cached, cache_hit, cache_waited = cache.get_or_create(
                        key=(
                            self.metadata.name,
                            "zones",
                            configured_account_id,
                            settings.cache_identity(),
                        ),
                        create=lambda: tuple(
                            self._session_factory.list_zones(
                                settings=settings, account_id=configured_account_id
                            )
                        ),
                    )
                    zones = self._cached_zones(cached)
                    zones = self._exclude_zones(zones, exclude=exclude)
            preflight = CloudflarePreflightData(settings=settings, zones=tuple(zones))
            execution_keys = tuple(
                (self.metadata.name, "zone", zone.zone_id) for zone in zones
            )
            selected_count = len(zones)

        recorder.update(
            {
                "cloudflare_discovery_cache_hit": cache_hit,
                "cloudflare_discovery_cache_waited": cache_waited,
                "cloudflare_selected_target_count": selected_count,
            }
        )
        return ProviderPreparation(
            data=preflight, exclusive_execution_keys=execution_keys
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
        """Resolve prepared or direct Cloudflare account and zone targets."""

        self.validate_target(target)
        if regions != ["global"]:
            raise ValueError("Cloudflare execution requires regions: [global]")
        if preparation is not None and not isinstance(
            preparation, CloudflarePreflightData
        ):
            raise TypeError("Cloudflare preparation must be CloudflarePreflightData")

        if preparation is None:
            settings = resolve_auth_settings(provider_options=target.provider_options)
            if target.mode == MODE_ACCOUNTS:
                accounts = (
                    self._explicit_accounts(include)
                    if include is not None
                    else self._exclude_accounts(
                        self._session_factory.list_accounts(settings=settings),
                        exclude=exclude,
                    )
                )
                zones: list[CloudflareZone] = []
            else:
                account_id = self._string_option(
                    target=target, option_name="account_id"
                )
                zones = (
                    self._explicit_zones(include, account_id=account_id)
                    if include is not None
                    else self._exclude_zones(
                        self._session_factory.list_zones(
                            settings=settings, account_id=account_id
                        ),
                        exclude=exclude,
                    )
                )
                accounts = []
        else:
            settings = preparation.settings
            accounts = list(preparation.accounts)
            zones = list(preparation.zones)

        if target.mode == MODE_ACCOUNTS:
            execution_targets = [
                self._account_execution_target(
                    account=account, regions=regions, settings=settings
                )
                for account in accounts
            ]
        else:
            execution_targets = [
                self._zone_execution_target(
                    zone=zone, regions=regions, settings=settings
                )
                for zone in zones
            ]
        return ProviderExecutionPlan(execution_targets=execution_targets)

    def prepare_execution_runtime(
        self,
        *,
        target: TargetDescriptor,
        execution_target: ExecutionTarget,
        context: ExecutionContext,
    ) -> ProviderExecutionRuntime:
        """Prepare a Cloudflare runtime for one account or zone target."""

        self.validate_target(target)
        if execution_target.provider != self.metadata.name:
            raise ValueError(
                f"Execution target provider '{execution_target.provider}' is not "
                "cloudflare"
            )
        if not isinstance(
            execution_target.provider_data, CloudflareExecutionTargetData
        ):
            raise TypeError(
                "Cloudflare execution target is missing CloudflareExecutionTargetData"
            )
        return CloudflareExecutionRuntime(
            data=execution_target.provider_data,
            benchmark_enabled=context.benchmark_enabled,
        )

    def _account_execution_target(
        self,
        *,
        account: CloudflareAccount,
        regions: list[str],
        settings: CloudflareAuthSettings,
    ) -> ExecutionTarget:
        data = CloudflareExecutionTargetData(
            target_type="account",
            target_id=account.account_id,
            account_id=account.account_id,
            zone_id=None,
            settings=settings,
            session_factory=self._session_factory,
        )
        return ExecutionTarget(
            id=account.account_id,
            name=account.display_name or account.account_id,
            type="account",
            provider=self.metadata.name,
            regions=list(regions),
            metadata={"account_id": account.account_id},
            provider_data=data,
        )

    def _zone_execution_target(
        self,
        *,
        zone: CloudflareZone,
        regions: list[str],
        settings: CloudflareAuthSettings,
    ) -> ExecutionTarget:
        data = CloudflareExecutionTargetData(
            target_type="zone",
            target_id=zone.zone_id,
            account_id=zone.account_id,
            zone_id=zone.zone_id,
            settings=settings,
            session_factory=self._session_factory,
        )
        metadata: dict[str, object] = {"zone_id": zone.zone_id}
        if zone.account_id is not None:
            metadata["account_id"] = zone.account_id
        if zone.account_name is not None:
            metadata["account_name"] = zone.account_name
        if zone.status is not None:
            metadata["zone_status"] = zone.status
        return ExecutionTarget(
            id=zone.zone_id,
            name=zone.display_name or zone.zone_id,
            type="zone",
            provider=self.metadata.name,
            regions=list(regions),
            metadata=metadata,
            provider_data=data,
        )

    @staticmethod
    def _explicit_accounts(account_ids: list[str]) -> list[CloudflareAccount]:
        return [CloudflareAccount(account_id=account_id) for account_id in account_ids]

    @staticmethod
    def _explicit_zones(
        zone_ids: list[str], *, account_id: str | None
    ) -> list[CloudflareZone]:
        return [
            CloudflareZone(zone_id=zone_id, account_id=account_id)
            for zone_id in zone_ids
        ]

    @staticmethod
    def _cached_accounts(cached: object) -> list[CloudflareAccount]:
        if not isinstance(cached, tuple) or any(
            not isinstance(item, CloudflareAccount) for item in cached
        ):
            raise RuntimeError(
                "Cloudflare account discovery cache returned invalid data"
            )
        return list(cast(tuple[CloudflareAccount, ...], cached))

    @staticmethod
    def _cached_zones(cached: object) -> list[CloudflareZone]:
        if not isinstance(cached, tuple) or any(
            not isinstance(item, CloudflareZone) for item in cached
        ):
            raise RuntimeError("Cloudflare zone discovery cache returned invalid data")
        return list(cast(tuple[CloudflareZone, ...], cached))

    @staticmethod
    def _exclude_accounts(
        accounts: list[CloudflareAccount], *, exclude: list[str] | None
    ) -> list[CloudflareAccount]:
        ordered = CloudflareProvider._unique_accounts(accounts)
        if exclude is None:
            return ordered
        discovered_ids = {account.account_id for account in ordered}
        CloudflareProvider._validate_known_exclusions(
            exclude=exclude, discovered_ids=discovered_ids, label="account"
        )
        excluded = set(exclude)
        return [account for account in ordered if account.account_id not in excluded]

    @staticmethod
    def _exclude_zones(
        zones: list[CloudflareZone], *, exclude: list[str] | None
    ) -> list[CloudflareZone]:
        ordered = CloudflareProvider._unique_zones(zones)
        if exclude is None:
            return ordered
        discovered_ids = {zone.zone_id for zone in ordered}
        CloudflareProvider._validate_known_exclusions(
            exclude=exclude, discovered_ids=discovered_ids, label="zone"
        )
        excluded = set(exclude)
        return [zone for zone in ordered if zone.zone_id not in excluded]

    @staticmethod
    def _unique_accounts(accounts: list[CloudflareAccount]) -> list[CloudflareAccount]:
        ordered = sorted(accounts, key=lambda account: account.account_id)
        for account in ordered:
            CloudflareProvider._validate_identifier(
                identifier=account.account_id, label="discovered account"
            )
        ids = [account.account_id for account in ordered]
        if len(ids) != len(set(ids)):
            raise RuntimeError("Cloudflare account discovery returned duplicate IDs")
        return ordered

    @staticmethod
    def _unique_zones(zones: list[CloudflareZone]) -> list[CloudflareZone]:
        ordered = sorted(zones, key=lambda zone: zone.zone_id)
        for zone in ordered:
            CloudflareProvider._validate_identifier(
                identifier=zone.zone_id, label="discovered zone"
            )
            if zone.account_id is not None:
                CloudflareProvider._validate_identifier(
                    identifier=zone.account_id, label="discovered parent account"
                )
        ids = [zone.zone_id for zone in ordered]
        if len(ids) != len(set(ids)):
            raise RuntimeError("Cloudflare zone discovery returned duplicate IDs")
        return ordered

    @staticmethod
    def _validate_known_exclusions(
        *, exclude: list[str], discovered_ids: set[str], label: str
    ) -> None:
        unknown = [
            target_id for target_id in exclude if target_id not in discovered_ids
        ]
        if unknown:
            raise ValueError(
                f"Cloudflare exclude filter matched unknown {label} IDs: "
                f"{', '.join(unknown)}"
            )

    @staticmethod
    def _validate_identifier(*, identifier: str, label: str) -> None:
        if len(identifier) != CLOUDFLARE_IDENTIFIER_LENGTH or any(
            character not in hexdigits for character in identifier
        ):
            raise ValueError(
                f"Invalid Cloudflare {label} ID '{identifier}': expected a "
                f"{CLOUDFLARE_IDENTIFIER_LENGTH}-character hexadecimal ID"
            )

    @staticmethod
    def _string_option(*, target: TargetDescriptor, option_name: str) -> str | None:
        value = target.provider_options.get(option_name)
        return value.strip() if isinstance(value, str) else None


def create_provider_instance() -> CloudflareProvider:
    """Create the first-party Cloudflare provider."""

    return CloudflareProvider()
