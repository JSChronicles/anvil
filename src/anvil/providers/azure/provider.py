from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

from anvil.descriptors import ConfigBranch, MODE_AZURE_TENANT, TargetDescriptor
from anvil.execution_context import ExecutionContext
from anvil.providers.base import (
    ExecutionTarget,
    ProviderAuthResult,
    ProviderExecutionPlan,
    ProviderExecutionRuntime,
    ProviderMetadata,
    ProviderRegion,
)
from anvil.regions import is_region_selector, resolve_location_selectors
from anvil.results import ExecutionStatus

DEFAULT_AZURE_LOCATIONS = ["eastus"]
AZURE_AVAILABLE_LOCATION_STATUS = "available"
AZURE_AVAILABLE_LOCATION_STATUSES = {AZURE_AVAILABLE_LOCATION_STATUS}


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
    configured_subscription_id: str | None
    session_factory: "AzureSessionFactory"


@dataclass(frozen=True, slots=True)
class AzureSession:
    """Lazy Azure runtime session for one subscription and location."""

    subscription_id: str
    location: str
    credential: object
    configured_subscription_id: str | None = None

    @property
    def region_name(self) -> str:
        """Compatibility alias used by existing task call signatures."""

        return self.location


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
                "when building an Azure runtime session. Install with "
                "'anvil[azure]'."
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
        configured_subscription_id: str | None = None,
    ) -> AzureSession:
        """Create an Azure session for a subscription/location pair."""

        credential = self._build_credential(
            tenant_id=tenant_id, client_id=client_id, client_secret=client_secret
        )
        return AzureSession(
            subscription_id=subscription_id,
            location=location,
            credential=credential,
            configured_subscription_id=configured_subscription_id,
        )

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
                "'azure-mgmt-subscription'. Install with 'anvil[azure]'."
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
                "'azure-mgmt-resource-subscriptions'. Install with 'anvil[azure]'."
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
            configured_subscription_id=self._data.configured_subscription_id,
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
        name="azure", display_name="Azure", description="Microsoft Azure provider"
    )

    def __init__(self, *, session_factory: AzureSessionFactory | None = None) -> None:
        self._session_factory = session_factory or AzureSessionFactory()

    def validate_target(self, target: TargetDescriptor) -> None:
        """Validate Azure support for tenant discovery and explicit subscriptions."""

        if target.config_branch is not ConfigBranch.TARGETS:
            raise ValueError(
                "Azure provider supports targets config (schema_version: 2) only"
            )
        if target.provider != self.metadata.name:
            raise ValueError("Azure provider supports provider 'azure' targets only")
        if (
            target.provider_options.get("tenant_id") is not None
            and target.provider_options.get("client_secret") is None
        ):
            raise ValueError(
                "Azure provider.options.tenant_id is only supported with client_secret"
            )
        if (
            target.provider_options.get("subscription_id") is not None
            and target.provider_options.get("client_secret") is None
        ):
            raise ValueError(
                "Azure provider.options.subscription_id is only supported with "
                "client_secret"
            )
        if target.provider_options.get("client_secret") is not None and (
            target.provider_options.get("tenant_id") is None
            or target.provider_options.get("client_id") is None
        ):
            raise ValueError(
                "Azure provider.options.client_secret requires tenant_id and client_id"
            )

    def default_regions(self, target: TargetDescriptor) -> list[str]:
        """Return configured Azure locations or the minimal default."""

        self.validate_target(target)
        if target.regions == ["us-east-1"]:
            return list(DEFAULT_AZURE_LOCATIONS)
        return list(target.regions or DEFAULT_AZURE_LOCATIONS)

    def auth_cache_key(self, target: TargetDescriptor) -> object | None:
        """Return a provider auth cache identity without loading Azure SDKs."""

        return (self.metadata.name, target.profile)

    def auth_check(self, target: TargetDescriptor) -> ProviderAuthResult:
        """Report deferred Azure auth checks without live SDK calls."""

        self.validate_target(target)
        return ProviderAuthResult(
            status=ExecutionStatus.SUCCESS,
            source="deferred",
            message="Azure authentication is validated when a runtime session is built.",
        )

    def discover_regions(self, target: TargetDescriptor) -> list[ProviderRegion]:
        """Return configured/default Azure locations without live discovery."""

        return [
            ProviderRegion(name=location, available=True, status="configured")
            for location in self.default_regions(target)
        ]

    def resolve_execution_targets(
        self,
        *,
        target: TargetDescriptor,
        regions: list[str],
        include: list[str] | None,
        exclude: list[str] | None,
    ) -> ProviderExecutionPlan:
        """Resolve Azure subscription IDs deterministically."""

        self.validate_target(target)

        if target.mode == MODE_AZURE_TENANT or target.include is None:
            subscriptions = self._discover_subscriptions(
                provider_options=target.provider_options
            )
            subscription_ids = self._filter_discovered_subscription_ids(
                subscriptions=subscriptions, include=include, exclude=exclude
            )
        else:
            subscription_ids = include or target.include
        execution_targets = [
            self._execution_target(
                subscription_id=subscription_id,
                locations=self._resolve_locations(
                    target=target, subscription_id=subscription_id, regions=regions
                ),
                provider_options=target.provider_options,
            )
            for subscription_id in subscription_ids
        ]
        return ProviderExecutionPlan(execution_targets=execution_targets)

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
        subscription_id: str,
        locations: list[str],
        provider_options: dict[str, object],
    ) -> ExecutionTarget:
        data = AzureExecutionTargetData(
            subscription_id=subscription_id,
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
            configured_subscription_id=self._string_option(
                provider_options=provider_options, option_name="subscription_id"
            ),
            session_factory=self._session_factory,
        )
        return ExecutionTarget(
            id=subscription_id,
            name=subscription_id,
            type="subscription",
            provider=self.metadata.name,
            metadata={"subscription_id": subscription_id},
            provider_data=data,
        )

    def _resolve_locations(
        self, *, target: TargetDescriptor, subscription_id: str, regions: list[str]
    ) -> list[str]:
        if not any(is_region_selector(region) for region in regions):
            return list(regions)

        locations = self._session_factory.list_locations(
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
        return resolve_location_selectors(
            target_name=target.name,
            configured_locations=regions,
            location_statuses={
                location.name: location.status or "unknown" for location in locations
            },
            available_statuses=AZURE_AVAILABLE_LOCATION_STATUSES,
            label="location",
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

    def _filter_discovered_subscription_ids(
        self,
        *,
        subscriptions: list[AzureSubscription],
        include: list[str] | None,
        exclude: list[str] | None,
    ) -> list[str]:
        discovered_ids = sorted(
            subscription.subscription_id for subscription in subscriptions
        )
        discovered_set = set(discovered_ids)

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
            subscription_ids = [subscription_id for subscription_id in include]
        else:
            subscription_ids = discovered_ids

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
            subscription_ids = [
                subscription_id
                for subscription_id in subscription_ids
                if subscription_id not in excluded
            ]

        return subscription_ids

    def _string_option(
        self, *, provider_options: dict[str, object], option_name: str
    ) -> str | None:
        option = provider_options.get(option_name)
        return option if isinstance(option, str) else None


def create_provider() -> AzureProvider:
    """Create the first-party Azure provider."""

    return AzureProvider()
