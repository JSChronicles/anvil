from __future__ import annotations

from dataclasses import dataclass

from anvil.descriptors import ConfigBranch, TargetDescriptor
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

DEFAULT_AZURE_LOCATIONS = ["eastus"]


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

        try:
            from azure.identity import ClientSecretCredential, DefaultAzureCredential
        except ImportError as error:
            raise RuntimeError(
                "Azure provider requires optional dependency 'azure-identity' "
                "when building an Azure runtime session."
            ) from error

        try:
            if client_secret is not None:
                if tenant_id is None or client_id is None:
                    raise RuntimeError(
                        "Azure provider_options.client_secret requires tenant_id and "
                        "client_id when building an Azure runtime session."
                    )
                credential = ClientSecretCredential(
                    tenant_id=tenant_id,
                    client_id=client_id,
                    client_secret=client_secret,
                )
            else:
                credential_kwargs: dict[str, str] = {}
                if client_id is not None:
                    credential_kwargs["managed_identity_client_id"] = client_id
                credential = DefaultAzureCredential(**credential_kwargs)
        except RuntimeError:
            raise
        except Exception as error:
            raise RuntimeError(
                "Azure provider could not build a runtime session from configured "
                f"credentials: {error}"
            ) from error

        return AzureSession(
            subscription_id=subscription_id,
            location=location,
            credential=credential,
            configured_subscription_id=configured_subscription_id,
        )


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
    """Minimal Azure provider for explicit subscription targets."""

    metadata = ProviderMetadata(
        name="azure", display_name="Azure", description="Microsoft Azure provider"
    )

    def __init__(self, *, session_factory: AzureSessionFactory | None = None) -> None:
        self._session_factory = session_factory or AzureSessionFactory()

    def validate_target(self, target: TargetDescriptor) -> None:
        """Validate minimal Azure support: explicit subscription targets only."""

        if target.config_branch is not ConfigBranch.ACCOUNTS:
            raise ValueError(
                "Azure provider currently supports explicit subscriptions only"
            )
        if not target.include:
            raise ValueError("Azure provider requires explicit subscription IDs")
        if target.exclude is not None:
            raise ValueError("Azure provider does not support exclude")
        if (
            target.provider_options.get("tenant_id") is not None
            and target.provider_options.get("client_secret") is None
        ):
            raise ValueError(
                "Azure provider_options.tenant_id is only supported with "
                "client_secret"
            )
        if (
            target.provider_options.get("subscription_id") is not None
            and target.provider_options.get("client_secret") is None
        ):
            raise ValueError(
                "Azure provider_options.subscription_id is only supported with "
                "client_secret"
            )
        if (
            target.provider_options.get("client_secret") is not None
            and (
                target.provider_options.get("tenant_id") is None
                or target.provider_options.get("client_id") is None
            )
        ):
            raise ValueError(
                "Azure provider_options.client_secret requires tenant_id and client_id"
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
        """Resolve explicit Azure subscription IDs deterministically."""

        self.validate_target(target)
        if exclude is not None:
            raise ValueError("Azure provider does not support exclude")

        subscription_ids = include or target.include or []
        execution_targets = [
            self._execution_target(
                subscription_id=subscription_id,
                locations=regions,
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

    def _string_option(
        self, *, provider_options: dict[str, object], option_name: str
    ) -> str | None:
        option = provider_options.get(option_name)
        return option if isinstance(option, str) else None


def create_provider() -> AzureProvider:
    """Create the first-party Azure provider."""

    return AzureProvider()
