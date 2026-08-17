from __future__ import annotations

import os
from dataclasses import dataclass

from anvil.descriptors import TargetDescriptor
from anvil.execution_context import ExecutionContext
from anvil.providers.datadog.config import (
    SUPPORTED_OPTIONS,
    DatadogTargetSettings,
    target_settings,
)
from anvil.providers.datadog.session import (
    DATADOG_EXTRA_REMEDIATION,
    DatadogCredentialError,
    DatadogDependencyError,
    DatadogProviderError,
    DatadogSession,
    DatadogSessionFactory,
    redacted_error_message,
)
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
    validate_region_selectors,
    validate_string_options,
)
from anvil.results import ExecutionStatus

DEFAULT_REGIONS = ("global",)
MODE_ORGANIZATION = "organization"
SUPPORTED_MODES = frozenset({MODE_ORGANIZATION})


@dataclass(frozen=True, slots=True)
class DatadogExecutionTargetData:
    """Datadog-specific data needed to prepare one organization runtime."""

    target_id: str
    regions: list[str]
    settings: DatadogTargetSettings
    session_factory: "DatadogSessionFactory"


class DatadogExecutionRuntime:
    """Datadog runtime adapter for one key-bound organization target."""

    def __init__(self, *, data: DatadogExecutionTargetData) -> None:
        self._data = data
        self._sessions: list[DatadogSession] = []

    def build_session(self, *, region: str) -> DatadogSession:
        """Build a Datadog session for the provider's global coordinate."""

        if region not in self._data.regions:
            raise ValueError(
                f"Datadog target '{self._data.target_id}' does not "
                f"define execution region '{region}'"
            )
        session = self._data.session_factory.create_session(
            target_id=self._data.target_id,
            region_name=region,
            settings=self._data.settings,
        )
        self._sessions.append(session)
        return session

    def record_region_outcome(
        self, *, region: str, duration_seconds: float, failed: bool, interrupted: bool
    ) -> None:
        """Datadog runtime currently has no adaptive lifecycle state."""

    def close(self) -> None:
        """Close every Datadog session created by this runtime."""

        close_error: Exception | None = None
        for session in reversed(self._sessions):
            try:
                session.close()
            except Exception as error:
                close_error = close_error or error
        self._sessions.clear()
        if close_error is not None:
            secrets = (
                os.environ.get(self._data.settings.api_key_env),
                os.environ.get(self._data.settings.app_key_env),
            )
            raise RuntimeError(
                "Datadog runtime could not close an API client: "
                f"{redacted_error_message(close_error, secrets=secrets)}"
            ) from close_error


class DatadogProvider:
    """Datadog provider for one key-bound organization per Anvil target."""

    metadata = ProviderMetadata(
        name="datadog",
        display_name="Datadog",
        description="Datadog observability platform provider",
        default_regions=DEFAULT_REGIONS,
        supported_task_scopes=frozenset({"region", "target"}),
    )

    def __init__(self, *, session_factory: DatadogSessionFactory | None = None) -> None:
        self._session_factory = session_factory or DatadogSessionFactory()

    def validate_target(self, target: TargetDescriptor) -> None:
        """Validate a single-organization Datadog target descriptor."""

        if target.provider != self.metadata.name:
            raise ValueError(
                "Datadog provider supports provider 'datadog' targets only"
            )
        if target.mode not in SUPPORTED_MODES:
            raise ValueError(f"Unsupported Datadog target mode: {target.mode}")
        validate_string_options(target=target, allowed_options=SUPPORTED_OPTIONS)
        validate_region_selectors(target=target, selectors_allowed=False)
        if target.regions is not None and target.regions != ["global"]:
            raise ValueError(
                "Datadog targets support only region 'global'; configure the "
                "Datadog site with provider.options.site"
            )
        if target.include is not None or target.exclude is not None:
            raise ValueError(
                "Datadog organization mode does not allow include or exclude; "
                "configure one top-level target per organization"
            )
        target_settings(target.provider_options)

    def resolve_target_filters(
        self,
        *,
        target: TargetDescriptor,
        include_override: list[str] | None,
        exclude_override: list[str] | None,
    ) -> tuple[list[str] | None, list[str] | None]:
        """Reject child-target filtering for a key-bound organization."""

        if include_override is not None or exclude_override is not None:
            raise ValueError(
                "Datadog organization targets do not support --include or --exclude"
            )
        self.validate_target(target)
        return None, None

    def auth_cache_key(self, target: TargetDescriptor) -> object | None:
        """Return a stable, secret-safe Datadog authentication identity."""

        settings = target_settings(target.provider_options)
        return (self.metadata.name, *settings.cache_identity())

    def auth_check(self, target: TargetDescriptor) -> ProviderAuthResult:
        """Validate Datadog dependencies, credentials, and key-pair authentication."""

        self.validate_target(target)
        settings = target_settings(target.provider_options)
        try:
            source = self._session_factory.validate_auth(settings=settings)
        except DatadogDependencyError as error:
            return ProviderAuthResult(
                status=ExecutionStatus.ERROR,
                source="datadog",
                message=str(error),
                remediation=DATADOG_EXTRA_REMEDIATION,
            )
        except DatadogCredentialError as error:
            return ProviderAuthResult(
                status=ExecutionStatus.ERROR,
                source="environment",
                message=str(error),
                remediation=(
                    "Set the named environment variable or select another variable "
                    "with provider.options.api_key_env/app_key_env."
                ),
            )
        except DatadogProviderError as error:
            return ProviderAuthResult(
                status=ExecutionStatus.ERROR, source="datadog", message=str(error)
            )

        return ProviderAuthResult(
            status=ExecutionStatus.SUCCESS,
            source=source,
            message=f"Datadog authentication validated for site '{settings.site}'.",
        )

    def discover_regions(self, target: TargetDescriptor) -> list[ProviderRegion]:
        """Return the single provider-neutral Datadog execution coordinate."""

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
        """Return empty preflight state for a key-bound organization target."""

        self.validate_target(target)
        if include is not None or exclude is not None:
            raise ValueError(
                "Datadog organization preparation does not accept target filters"
            )
        return ProviderPreparation()

    def resolve_execution_targets(
        self,
        *,
        target: TargetDescriptor,
        regions: list[str],
        include: list[str] | None,
        exclude: list[str] | None,
        preparation: object | None = None,
    ) -> ProviderExecutionPlan:
        """Resolve one deterministic execution target for the configured organization."""

        self.validate_target(target)
        if preparation is not None:
            raise TypeError("Datadog does not accept provider preparation data")
        if include is not None or exclude is not None:
            raise ValueError(
                "Datadog organization resolution does not accept target filters"
            )
        if regions != ["global"]:
            raise ValueError("Datadog execution regions must resolve to ['global']")

        settings = target_settings(target.provider_options)
        data = DatadogExecutionTargetData(
            target_id=target.name,
            regions=list(regions),
            settings=settings,
            session_factory=self._session_factory,
        )
        execution_target = ExecutionTarget(
            id=target.name,
            name=target.name,
            type="organization",
            provider=self.metadata.name,
            regions=list(regions),
            metadata={
                "datadog_organization": target.name,
                "datadog_site": settings.site,
            },
            provider_data=data,
        )
        return ProviderExecutionPlan(execution_targets=[execution_target])

    def prepare_execution_runtime(
        self,
        *,
        target: TargetDescriptor,
        execution_target: ExecutionTarget,
        context: ExecutionContext,
    ) -> ProviderExecutionRuntime:
        """Prepare Datadog runtime state for one organization target."""

        self.validate_target(target)
        if execution_target.provider != self.metadata.name:
            raise ValueError(
                f"Execution target provider '{execution_target.provider}' is not "
                "datadog"
            )
        if not isinstance(execution_target.provider_data, DatadogExecutionTargetData):
            raise TypeError(
                "Datadog execution target is missing DatadogExecutionTargetData"
            )
        return DatadogExecutionRuntime(data=execution_target.provider_data)


def create_provider_instance() -> DatadogProvider:
    """Create the first-party Datadog provider."""

    return DatadogProvider()
