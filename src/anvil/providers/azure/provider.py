from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace

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
from anvil.regions import is_region_selector, resolve_location_selectors
from anvil.results import ExecutionStatus

__LOGGER__ = logging.getLogger(__name__)

DEFAULT_REGIONS = ("eastus",)
MODE_TENANT = "tenant"
MODE_SUBSCRIPTIONS = "subscriptions"
SUPPORTED_MODES = frozenset({MODE_TENANT, MODE_SUBSCRIPTIONS})
SUPPORTED_OPTIONS = frozenset({"tenant_id", "client_id", "client_secret"})
AZURE_AVAILABLE_LOCATION_STATUS = "available"
AZURE_AVAILABLE_LOCATION_STATUSES = {AZURE_AVAILABLE_LOCATION_STATUS}
AZURE_EXTRA_REMEDIATION = (
    "Install Azure dependencies with 'uv sync --extra azure' for a source checkout "
    "or 'pip install \"anvil[azure]\"' for an installed package."
)


@dataclass(frozen=True, slots=True)
class AzureSubscription:
    """Azure subscription identity discovered from the subscription API."""

    subscription_id: str
    display_name: str | None = None


@dataclass(slots=True)
class _AzureSubscriptionDiscoveryFlight:
    event: threading.Event
    subscriptions: list[AzureSubscription] | None = None
    error: BaseException | None = None


class _AzureSubscriptionDiscoveryCache:
    """Single-flight cache for Azure subscription discovery only."""

    def __init__(self) -> None:
        self._values: dict[object, list[AzureSubscription]] = {}
        self._flights: dict[object, _AzureSubscriptionDiscoveryFlight] = {}
        self._lock = threading.Lock()

    def get_or_discover(
        self, *, key: object, discover: Callable[[], list[AzureSubscription]]
    ) -> list[AzureSubscription]:
        with self._lock:
            cached = self._values.get(key)
            if cached is not None:
                return list(cached)

            flight = self._flights.get(key)
            if flight is None:
                flight = _AzureSubscriptionDiscoveryFlight(event=threading.Event())
                self._flights[key] = flight
                owns_discovery = True
            else:
                owns_discovery = False

        if owns_discovery:
            try:
                subscriptions = list(discover())
            except BaseException as error:
                with self._lock:
                    flight.error = error
                    self._flights.pop(key, None)
                    flight.event.set()
                raise

            with self._lock:
                cached = self._values.get(key)
                stored = list(cached) if cached is not None else subscriptions
                self._values[key] = list(stored)
                flight.subscriptions = list(stored)
                self._flights.pop(key, None)
                flight.event.set()

            return list(stored)

        flight.event.wait()
        if flight.error is not None:
            raise flight.error
        if flight.subscriptions is None:
            raise RuntimeError("Azure subscription discovery completed empty")
        return list(flight.subscriptions)


_AZURE_SUBSCRIPTION_DISCOVERY_CACHE = _AzureSubscriptionDiscoveryCache()


@dataclass(frozen=True, slots=True)
class AzureExecutionTargetData:
    """Azure-specific data needed to prepare one subscription runtime."""

    subscription_id: str
    locations: list[str]
    tenant_id: str | None
    client_id: str | None
    client_secret: str | None
    session_factory: "AzureSessionFactory"


@dataclass(frozen=True, slots=True)
class AzurePreflightData:
    """Azure provider-owned discovery data prepared before execution."""

    subscriptions: list[AzureSubscription]
    location_statuses_by_subscription: dict[str, dict[str, str]]


@dataclass(frozen=True, slots=True)
class AzureSession:
    """Lazy Azure runtime session for one subscription and location."""

    subscription_id: str
    location: str
    credential: object


class AzureSessionFactory:
    """Create Azure credentials lazily so provider validation has no SDK dependency."""

    def _build_credential(
        self,
        *,
        tenant_id: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
    ) -> object:
        try:
            from azure.identity import ClientSecretCredential, DefaultAzureCredential
        except ImportError as error:
            raise RuntimeError(
                "Azure provider requires optional dependency 'azure-identity' "
                f"when building an Azure runtime session. {AZURE_EXTRA_REMEDIATION}"
            ) from error

        try:
            if client_secret is not None:
                if tenant_id is None or client_id is None:
                    raise RuntimeError(
                        "Azure provider.options.client_secret requires tenant_id and "
                        "client_id when building an Azure runtime session."
                    )
                return ClientSecretCredential(
                    tenant_id=tenant_id,
                    client_id=client_id,
                    client_secret=client_secret,
                )

            credential_kwargs: dict[str, str] = {}
            if client_id is not None:
                credential_kwargs["managed_identity_client_id"] = client_id
            return DefaultAzureCredential(**credential_kwargs)
        except RuntimeError:
            raise
        except Exception as error:
            raise RuntimeError(
                "Azure provider could not build a runtime session from configured "
                f"credentials: {error}"
            ) from error

    def create_session(
        self,
        *,
        subscription_id: str,
        location: str,
        tenant_id: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
    ) -> AzureSession:
        """Create an Azure session for a subscription/location pair."""

        credential = self._build_credential(
            tenant_id=tenant_id, client_id=client_id, client_secret=client_secret
        )
        return AzureSession(
            subscription_id=subscription_id, location=location, credential=credential
        )

    def validate_auth(
        self,
        *,
        tenant_id: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
    ) -> None:
        """Validate Azure credentials can acquire an ARM access token."""

        credential = self._build_credential(
            tenant_id=tenant_id, client_id=client_id, client_secret=client_secret
        )
        get_token = getattr(credential, "get_token", None)
        if not callable(get_token):
            raise RuntimeError(
                "Azure credential does not support token validation with get_token()."
            )
        try:
            get_token("https://management.azure.com/.default")
        except Exception as error:
            raise RuntimeError(
                f"Azure provider could not validate ARM authentication: {error}"
            ) from error

    def list_subscriptions(
        self,
        *,
        tenant_id: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
    ) -> list[AzureSubscription]:
        """List Azure subscriptions lazily through the Azure management SDK."""

        credential = self._build_credential(
            tenant_id=tenant_id, client_id=client_id, client_secret=client_secret
        )
        try:
            from azure.mgmt.subscription import SubscriptionClient
        except ImportError as error:
            raise RuntimeError(
                "Azure subscription discovery requires optional dependency "
                f"'azure-mgmt-subscription'. {AZURE_EXTRA_REMEDIATION}"
            ) from error

        try:
            client = SubscriptionClient(credential)
            subscriptions = []
            for subscription in client.subscriptions.list():
                subscription_id = getattr(subscription, "subscription_id", None)
                display_name = getattr(subscription, "display_name", None)
                if isinstance(subscription_id, str) and subscription_id.strip():
                    subscriptions.append(
                        AzureSubscription(
                            subscription_id=subscription_id.strip(),
                            display_name=display_name
                            if isinstance(display_name, str)
                            else None,
                        )
                    )
            return sorted(subscriptions, key=lambda item: item.subscription_id)
        except RuntimeError:
            raise
        except Exception as error:
            raise RuntimeError(
                f"Azure provider could not discover subscriptions: {error}"
            ) from error

    def list_locations(
        self,
        *,
        subscription_id: str,
        tenant_id: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
    ) -> list[ProviderRegion]:
        """List Azure locations available to one subscription."""

        credential = self._build_credential(
            tenant_id=tenant_id, client_id=client_id, client_secret=client_secret
        )
        try:
            try:
                from azure.mgmt.resource.subscriptions import SubscriptionClient
            except ImportError:
                from azure.mgmt.subscription import SubscriptionClient
        except ImportError as error:
            raise RuntimeError(
                "Azure location discovery requires optional dependency "
                f"'azure-mgmt-resource-subscriptions'. {AZURE_EXTRA_REMEDIATION}"
            ) from error

        try:
            client = SubscriptionClient(credential)
            locations = []
            for location in client.subscriptions.list_locations(subscription_id):
                location_name = getattr(location, "name", None)
                if isinstance(location_name, str) and location_name.strip():
                    locations.append(
                        ProviderRegion(
                            name=location_name.strip(),
                            available=True,
                            status=AZURE_AVAILABLE_LOCATION_STATUS,
                        )
                    )
            return sorted(locations, key=lambda item: item.name)
        except RuntimeError:
            raise
        except Exception as error:
            raise RuntimeError(
                f"Azure provider could not discover locations for subscription "
                f"'{subscription_id}': {error}"
            ) from error


class AzureExecutionRuntime:
    """Azure runtime adapter for one explicit subscription target."""

    def __init__(self, *, data: AzureExecutionTargetData) -> None:
        self._data = data

    def build_session(self, *, region: str) -> AzureSession:
        """Build a lazy Azure session for one location."""

        return self._data.session_factory.create_session(
            subscription_id=self._data.subscription_id,
            location=region,
            tenant_id=self._data.tenant_id,
            client_id=self._data.client_id,
            client_secret=self._data.client_secret,
        )

    def record_region_outcome(
        self, *, region: str, duration_seconds: float, failed: bool, interrupted: bool
    ) -> None:
        """Azure runtime currently has no adaptive lifecycle state."""

    def close(self) -> None:
        """Azure runtime currently has no explicit resources to release."""


class AzureProvider:
    """Azure provider for explicit and discovered subscription targets."""

    metadata = ProviderMetadata(
        name="azure",
        display_name="Azure",
        description="Microsoft Azure provider",
        default_regions=DEFAULT_REGIONS,
        supported_task_scopes=frozenset({"region", "target"}),
    )

    def __init__(self, *, session_factory: AzureSessionFactory | None = None) -> None:
        self._session_factory = session_factory or AzureSessionFactory()

    def validate_target(self, target: TargetDescriptor) -> None:
        """Validate Azure support for tenant discovery and explicit subscriptions."""

        if target.provider != self.metadata.name:
            raise ValueError("Azure provider supports provider 'azure' targets only")
        if target.mode not in SUPPORTED_MODES:
            raise ValueError(f"Unsupported Azure target mode: {target.mode}")
        validate_string_options(target=target, allowed_options=SUPPORTED_OPTIONS)
        validate_region_selectors(target=target, selectors_allowed=True)
        if target.include is not None and target.exclude is not None:
            raise ValueError("Azure include and exclude filters are mutually exclusive")
        if target.mode == MODE_SUBSCRIPTIONS:
            if not target.include:
                raise ValueError("Azure mode 'subscriptions' requires include")
            if target.exclude is not None:
                raise ValueError("Azure mode 'subscriptions' does not allow exclude")
        if (
            target.provider_options.get("tenant_id") is not None
            and target.provider_options.get("client_secret") is None
        ):
            raise ValueError(
                "Azure provider.options.tenant_id is only supported with client_secret"
            )
        if target.provider_options.get("client_secret") is not None and (
            target.provider_options.get("tenant_id") is None
            or target.provider_options.get("client_id") is None
        ):
            raise ValueError(
                "Azure provider.options.client_secret requires tenant_id and client_id"
            )

    def resolve_target_filters(
        self,
        *,
        target: TargetDescriptor,
        include_override: list[str] | None,
        exclude_override: list[str] | None,
    ) -> tuple[list[str] | None, list[str] | None]:
        """Apply tenant discovery overrides or narrow explicit subscriptions."""

        if target.mode == MODE_TENANT:
            include = (
                include_override if include_override is not None else target.include
            )
            exclude = (
                exclude_override if exclude_override is not None else target.exclude
            )
        else:
            if exclude_override is not None:
                raise ValueError("Azure mode 'subscriptions' does not allow --exclude")
            include = narrow_include(
                configured=target.include, override=include_override
            )
            exclude = None

        self.validate_target(replace(target, include=include, exclude=exclude))
        return include, exclude

    def auth_cache_key(self, target: TargetDescriptor) -> object | None:
        """Return a provider auth cache identity without loading Azure SDKs."""

        return (
            self.metadata.name,
            target.provider_options.get("tenant_id"),
            target.provider_options.get("client_id"),
            target.provider_options.get("client_secret"),
        )

    def auth_check(self, target: TargetDescriptor) -> ProviderAuthResult:
        """Validate Azure auth dependencies and ARM token acquisition."""

        self.validate_target(target)
        try:
            self._session_factory.validate_auth(
                tenant_id=self._string_option(
                    provider_options=target.provider_options, option_name="tenant_id"
                ),
                client_id=self._string_option(
                    provider_options=target.provider_options, option_name="client_id"
                ),
                client_secret=self._string_option(
                    provider_options=target.provider_options,
                    option_name="client_secret",
                ),
            )
        except RuntimeError as error:
            message = str(error)
            if "azure-identity" in message:
                return ProviderAuthResult(
                    status=ExecutionStatus.ERROR,
                    source="azure",
                    message="Azure authentication requires optional dependency "
                    "'azure-identity'.",
                    remediation=AZURE_EXTRA_REMEDIATION,
                )
            return ProviderAuthResult(
                status=ExecutionStatus.ERROR, source="azure", message=message
            )
        return ProviderAuthResult(
            status=ExecutionStatus.SUCCESS,
            source="azure",
            message="Azure ARM authentication validated.",
        )

    def discover_regions(self, target: TargetDescriptor) -> list[ProviderRegion]:
        """Discover Azure locations when a concrete subscription is configured."""

        self.validate_target(target)
        return [
            ProviderRegion(name=location, available=True, status="configured")
            for location in configured_or_default_regions(
                configured=target.regions, default=self.metadata.default_regions
            )
        ]

    def resolve_execution_targets(
        self,
        *,
        target: TargetDescriptor,
        regions: list[str],
        include: list[str] | None,
        exclude: list[str] | None,
        preparation: object | None = None,
    ) -> ProviderExecutionPlan:
        """Resolve Azure subscription IDs deterministically."""

        self.validate_target(target)
        if preparation is not None and not isinstance(preparation, AzurePreflightData):
            raise TypeError("Azure preparation must be AzurePreflightData")
        preflight_data = preparation

        if preflight_data is not None:
            subscriptions = list(preflight_data.subscriptions)
        elif target.mode == MODE_TENANT or target.include is None:
            subscriptions = self._resolve_discovered_subscriptions(
                target=target, include=include, exclude=exclude
            )
        else:
            subscriptions = self._subscriptions_for_explicit_ids(
                provider_options=target.provider_options,
                subscription_ids=include or target.include or [],
            )
        execution_targets = [
            self._execution_target(
                subscription=subscription,
                locations=self._resolve_locations(
                    target=target,
                    subscription_id=subscription.subscription_id,
                    regions=regions,
                    preflight_data=preflight_data,
                ),
                provider_options=target.provider_options,
            )
            for subscription in subscriptions
        ]
        return ProviderExecutionPlan(execution_targets=execution_targets)

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
        """Discover Azure execution data before target execution."""

        self.validate_target(target)
        regions = context.regions
        sink = BenchmarkRecorder(data=benchmark)
        if target.mode == MODE_TENANT or target.include is None:
            with sink.phase("azure_discover_subscriptions_seconds"):
                subscriptions = self._resolve_discovered_subscriptions(
                    target=target, include=include, exclude=exclude
                )
        else:
            subscriptions = self._subscriptions_for_explicit_preflight(
                subscription_ids=include or target.include or []
            )

        with sink.phase("azure_discover_locations_seconds"):
            location_statuses_by_subscription = self._discover_location_statuses(
                target=target, subscriptions=subscriptions
            )

        sink.update(
            {
                "azure_selected_subscription_count": len(subscriptions),
                "azure_validated_subscription_count": len(
                    location_statuses_by_subscription
                ),
                "azure_selected_location_count": len(regions),
                "azure_discovered_location_count": sum(
                    len(statuses)
                    for statuses in location_statuses_by_subscription.values()
                ),
            }
        )
        preflight_data = AzurePreflightData(
            subscriptions=subscriptions,
            location_statuses_by_subscription=location_statuses_by_subscription,
        )

        return ProviderPreparation(
            data=preflight_data,
            exclusive_execution_keys=self._subscription_execution_exclusion_keys(
                subscriptions=subscriptions
            ),
        )

    def _resolve_discovered_subscriptions(
        self,
        *,
        target: TargetDescriptor,
        include: list[str] | None,
        exclude: list[str] | None,
    ) -> list[AzureSubscription]:
        subscriptions = self._discover_subscriptions(
            provider_options=target.provider_options
        )
        return self._filter_discovered_subscriptions(
            subscriptions=subscriptions, include=include, exclude=exclude
        )

    def _subscriptions_for_explicit_preflight(
        self, *, subscription_ids: list[str]
    ) -> list[AzureSubscription]:
        return [
            AzureSubscription(subscription_id=subscription_id)
            for subscription_id in subscription_ids
        ]

    def _discover_location_statuses(
        self, *, target: TargetDescriptor, subscriptions: list[AzureSubscription]
    ) -> dict[str, dict[str, str]]:
        return {
            subscription.subscription_id: {
                location.name: location.status or "unknown"
                for location in self._list_subscription_locations(
                    target=target, subscription_id=subscription.subscription_id
                )
            }
            for subscription in subscriptions
        }

    def _subscription_execution_exclusion_keys(
        self, *, subscriptions: list[AzureSubscription]
    ) -> tuple[object, ...]:
        return tuple(
            (self.metadata.name, "subscription", subscription.subscription_id)
            for subscription in subscriptions
        )

    def prepare_execution_runtime(
        self,
        *,
        target: TargetDescriptor,
        execution_target: ExecutionTarget,
        context: ExecutionContext,
    ) -> ProviderExecutionRuntime:
        """Prepare Azure runtime state for one explicit subscription."""

        self.validate_target(target)
        if execution_target.provider != self.metadata.name:
            raise ValueError(
                f"Execution target provider '{execution_target.provider}' is not azure"
            )
        if not isinstance(execution_target.provider_data, AzureExecutionTargetData):
            raise TypeError(
                "Azure execution target is missing AzureExecutionTargetData"
            )

        return AzureExecutionRuntime(data=execution_target.provider_data)

    def _execution_target(
        self,
        *,
        subscription: AzureSubscription,
        locations: list[str],
        provider_options: dict[str, object],
    ) -> ExecutionTarget:
        subscription_name = subscription.display_name or subscription.subscription_id
        data = AzureExecutionTargetData(
            subscription_id=subscription.subscription_id,
            locations=list(locations),
            tenant_id=self._string_option(
                provider_options=provider_options, option_name="tenant_id"
            ),
            client_id=self._string_option(
                provider_options=provider_options, option_name="client_id"
            ),
            client_secret=self._string_option(
                provider_options=provider_options, option_name="client_secret"
            ),
            session_factory=self._session_factory,
        )
        return ExecutionTarget(
            id=subscription.subscription_id,
            name=subscription_name,
            type="subscription",
            provider=self.metadata.name,
            regions=list(locations),
            metadata={"subscription_id": subscription.subscription_id},
            provider_data=data,
        )

    def _resolve_locations(
        self,
        *,
        target: TargetDescriptor,
        subscription_id: str,
        regions: list[str],
        preflight_data: AzurePreflightData | None = None,
    ) -> list[str]:
        if not any(is_region_selector(region) for region in regions):
            return list(regions)

        if preflight_data is not None:
            location_statuses = preflight_data.location_statuses_by_subscription.get(
                subscription_id, {}
            )
        else:
            location_statuses = {
                location.name: location.status or "unknown"
                for location in self._list_subscription_locations(
                    target=target, subscription_id=subscription_id
                )
            }
        return resolve_location_selectors(
            target_name=target.name,
            configured_locations=regions,
            location_statuses=location_statuses,
            available_statuses=AZURE_AVAILABLE_LOCATION_STATUSES,
            label="location",
        )

    def _list_subscription_locations(
        self, *, target: TargetDescriptor, subscription_id: str
    ) -> list[ProviderRegion]:
        return self._session_factory.list_locations(
            subscription_id=subscription_id,
            tenant_id=self._string_option(
                provider_options=target.provider_options, option_name="tenant_id"
            ),
            client_id=self._string_option(
                provider_options=target.provider_options, option_name="client_id"
            ),
            client_secret=self._string_option(
                provider_options=target.provider_options, option_name="client_secret"
            ),
        )

    def _discover_subscriptions(
        self, *, provider_options: dict[str, object]
    ) -> list[AzureSubscription]:
        tenant_id = self._string_option(
            provider_options=provider_options, option_name="tenant_id"
        )
        client_id = self._string_option(
            provider_options=provider_options, option_name="client_id"
        )
        client_secret = self._string_option(
            provider_options=provider_options, option_name="client_secret"
        )
        if type(self._session_factory) is not AzureSessionFactory:
            return self._session_factory.list_subscriptions(
                tenant_id=tenant_id, client_id=client_id, client_secret=client_secret
            )

        discovery_key = self._subscription_discovery_cache_key(
            tenant_id=tenant_id, client_id=client_id, client_secret=client_secret
        )
        return _AZURE_SUBSCRIPTION_DISCOVERY_CACHE.get_or_discover(
            key=discovery_key,
            discover=lambda: self._session_factory.list_subscriptions(
                tenant_id=tenant_id, client_id=client_id, client_secret=client_secret
            ),
        )

    def _subscription_discovery_cache_key(
        self, *, tenant_id: str | None, client_id: str | None, client_secret: str | None
    ) -> object:
        return (AzureSessionFactory, tenant_id, client_id, client_secret)

    def _subscriptions_for_explicit_ids(
        self, *, provider_options: dict[str, object], subscription_ids: list[str]
    ) -> list[AzureSubscription]:
        try:
            discovered_subscriptions = self._discover_subscriptions(
                provider_options=provider_options
            )
        except Exception as error:
            __LOGGER__.debug(f"Azure subscription display-name lookup skipped: {error}")
            discovered_subscriptions = []

        discovered_by_id = {
            subscription.subscription_id: subscription
            for subscription in discovered_subscriptions
        }
        return [
            discovered_by_id.get(
                subscription_id, AzureSubscription(subscription_id=subscription_id)
            )
            for subscription_id in subscription_ids
        ]

    def _filter_discovered_subscriptions(
        self,
        *,
        subscriptions: list[AzureSubscription],
        include: list[str] | None,
        exclude: list[str] | None,
    ) -> list[AzureSubscription]:
        discovered_subscriptions = sorted(
            subscriptions, key=lambda subscription: subscription.subscription_id
        )
        discovered_by_id = {
            subscription.subscription_id: subscription
            for subscription in discovered_subscriptions
        }
        discovered_set = set(discovered_by_id)

        if include is not None:
            unknown = [
                subscription_id
                for subscription_id in include
                if subscription_id not in discovered_set
            ]
            if unknown:
                unknown_display = ", ".join(unknown)
                raise ValueError(
                    "Azure include filter matched unknown subscription IDs: "
                    f"{unknown_display}"
                )
            selected_subscriptions = [
                discovered_by_id[subscription_id] for subscription_id in include
            ]
        else:
            selected_subscriptions = discovered_subscriptions

        if exclude is not None:
            unknown = [
                subscription_id
                for subscription_id in exclude
                if subscription_id not in discovered_set
            ]
            if unknown:
                unknown_display = ", ".join(unknown)
                raise ValueError(
                    "Azure exclude filter matched unknown subscription IDs: "
                    f"{unknown_display}"
                )
            excluded = set(exclude)
            selected_subscriptions = [
                subscription
                for subscription in selected_subscriptions
                if subscription.subscription_id not in excluded
            ]

        return selected_subscriptions

    def _string_option(
        self, *, provider_options: dict[str, object], option_name: str
    ) -> str | None:
        option = provider_options.get(option_name)
        return option if isinstance(option, str) else None


def create_provider_instance() -> AzureProvider:
    """Create the first-party Azure provider."""

    return AzureProvider()
