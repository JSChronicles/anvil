from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from anvil.descriptors import TargetDescriptor
from anvil.execution_context import ExecutionContext
from anvil.provider_profiles import ProviderProfileConfig, ProviderProfileResolver
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
from anvil.providers.pagerduty.auth import PagerDutyAuthSettings, resolve_auth_settings
from anvil.providers.pagerduty.errors import (
    PagerDutyClientError,
    PagerDutyCredentialError,
    PagerDutyDependencyError,
    PagerDutyProviderError,
)
from anvil.providers.pagerduty.session import (
    PAGERDUTY_EXTRA_REMEDIATION,
    PagerDutySession,
    PagerDutySessionFactory,
)
from anvil.results import ExecutionStatus

__LOGGER__ = logging.getLogger(__name__)

DEFAULT_REGIONS = ("global",)
MODE_ACCOUNT = "account"
SUPPORTED_MODES = frozenset({MODE_ACCOUNT})
SUPPORTED_OPTIONS = frozenset(
    {"api_url", "auth_type", "from_email", "subdomain", "token_env", "profile"}
)
PAGERDUTY_PROFILE_OPTIONS = frozenset(
    {"api_url", "auth_type", "from_email", "subdomain", "token_env"}
)
SUPPORTED_AUTH_TYPES = frozenset({"bearer", "token"})
SUBDOMAIN_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$")


@dataclass(frozen=True, slots=True)
class PagerDutyPreflightData:
    """Resolved PagerDuty account settings shared with target resolution."""

    settings: PagerDutyAuthSettings


@dataclass(frozen=True, slots=True)
class PagerDutyExecutionTargetData:
    """PagerDuty-specific data needed to prepare an account runtime."""

    account_id: str
    settings: PagerDutyAuthSettings
    session_factory: PagerDutySessionFactory


class PagerDutyExecutionRuntime:
    """PagerDuty runtime adapter for one account target."""

    def __init__(self, *, data: PagerDutyExecutionTargetData) -> None:
        self._data = data
        self._sessions: list[PagerDutySession] = []

    def build_session(self, *, region: str) -> PagerDutySession:
        """Build the global PagerDuty REST session."""

        if region != "global":
            raise ValueError(f"PagerDuty does not support execution region '{region}'")
        session = self._data.session_factory.create_session(
            account_id=self._data.account_id,
            region_name=region,
            settings=self._data.settings,
        )
        self._sessions.append(session)
        return session

    def record_region_outcome(
        self, *, region: str, duration_seconds: float, failed: bool, interrupted: bool
    ) -> None:
        """PagerDuty runtime currently has no adaptive lifecycle state."""

    def close(self) -> None:
        """Close all PagerDuty clients constructed by this runtime."""

        sessions = list(self._sessions)
        self._sessions.clear()
        for session in sessions:
            try:
                session.close()
            except PagerDutyClientError as error:
                __LOGGER__.warning(
                    "PagerDuty client cleanup failed for account "
                    f"{session.account_id}: {type(error.__cause__).__name__}"
                )


class PagerDutyProvider:
    """PagerDuty provider for account-scoped REST API execution."""

    metadata = ProviderMetadata(
        name="pagerduty",
        display_name="PagerDuty",
        description="PagerDuty incident response provider",
        default_regions=DEFAULT_REGIONS,
        supported_task_scopes=frozenset({"region", "target"}),
    )

    def __init__(
        self,
        *,
        session_factory: PagerDutySessionFactory | None = None,
        profile_config: ProviderProfileConfig | None = None,
    ) -> None:
        self._session_factory = session_factory or PagerDutySessionFactory()
        self._profile_resolver = ProviderProfileResolver(
            provider_name=self.metadata.name,
            profile_options=PAGERDUTY_PROFILE_OPTIONS,
            config=profile_config,
        )

    def validate_target(self, target: TargetDescriptor) -> None:
        """Validate PagerDuty account target configuration."""

        if target.provider != self.metadata.name:
            raise ValueError(
                "PagerDuty provider supports provider 'pagerduty' targets only"
            )
        if target.mode not in SUPPORTED_MODES:
            raise ValueError(f"Unsupported PagerDuty target mode: {target.mode}")
        validate_string_options(target=target, allowed_options=SUPPORTED_OPTIONS)
        validate_region_selectors(target=target, selectors_allowed=False)
        if target.regions is not None and target.regions != ["global"]:
            raise ValueError("PagerDuty regions must contain only 'global'")
        if target.include is not None or target.exclude is not None:
            raise ValueError("PagerDuty account mode does not allow include or exclude")

        provider_options = self._provider_options(target)
        auth_type_value = provider_options.get("auth_type", "token")
        auth_type = (
            auth_type_value.strip()
            if isinstance(auth_type_value, str)
            else auth_type_value
        )
        if auth_type not in SUPPORTED_AUTH_TYPES:
            allowed = ", ".join(sorted(SUPPORTED_AUTH_TYPES))
            raise ValueError(
                f"PagerDuty provider.options.auth_type must be one of: {allowed}"
            )
        self._validate_api_url(provider_options.get("api_url"))
        self._validate_subdomain(provider_options.get("subdomain"))

    def resolve_target_filters(
        self,
        *,
        target: TargetDescriptor,
        include_override: list[str] | None,
        exclude_override: list[str] | None,
    ) -> tuple[list[str] | None, list[str] | None]:
        """Reject filters because account mode has no child target boundary."""

        if include_override is not None or exclude_override is not None:
            raise ValueError("PagerDuty account mode does not allow target filters")
        self.validate_target(target)
        return None, None

    def auth_cache_key(self, target: TargetDescriptor) -> object | None:
        """Return a credential-sensitive identity without exposing the token."""

        settings = resolve_auth_settings(
            provider_options=self._provider_options(target), require_token=False
        )
        return (self.metadata.name, settings.cache_identity())

    def auth_check(self, target: TargetDescriptor) -> ProviderAuthResult:
        """Validate PagerDuty credentials, SDK availability, and client creation."""

        self.validate_target(target)
        settings = resolve_auth_settings(
            provider_options=self._provider_options(target), require_token=False
        )
        try:
            settings.require_token()
            self._session_factory.validate_settings(settings=settings)
        except PagerDutyProviderError as error:
            if isinstance(error, PagerDutyDependencyError):
                remediation = PAGERDUTY_EXTRA_REMEDIATION
            elif isinstance(error, PagerDutyCredentialError):
                remediation = (
                    f"Set {settings.token_env} to a non-empty PagerDuty API token."
                )
            else:
                remediation = (
                    "Verify the PagerDuty token, auth_type, api_url, and from_email "
                    "settings."
                )
            return ProviderAuthResult(
                status=ExecutionStatus.ERROR,
                source=settings.source,
                message=str(error),
                remediation=remediation,
            )
        return ProviderAuthResult(
            status=ExecutionStatus.SUCCESS,
            source=settings.source,
            message=(
                "PagerDuty token presence and REST client construction validated; "
                "no API request was made."
            ),
        )

    def discover_regions(self, target: TargetDescriptor) -> list[ProviderRegion]:
        """Return PagerDuty's configured global execution location."""

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
        """Resolve account settings and scheduler admission identity."""

        self.validate_target(target)
        if include is not None or exclude is not None:
            raise ValueError("PagerDuty account mode does not allow target filters")
        settings = resolve_auth_settings(
            provider_options=self._provider_options(target)
        )
        account_identity: object = settings.subdomain or (
            "credential",
            settings.token_fingerprint,
        )
        return ProviderPreparation(
            data=PagerDutyPreflightData(settings=settings),
            exclusive_execution_keys=(
                (self.metadata.name, MODE_ACCOUNT, settings.api_url, account_identity),
            ),
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
        """Resolve one deterministic PagerDuty account target."""

        self.validate_target(target)
        if include is not None or exclude is not None:
            raise ValueError("PagerDuty account mode does not allow target filters")
        if regions != ["global"]:
            raise ValueError("PagerDuty execution requires the single 'global' region")
        if preparation is not None and not isinstance(
            preparation, PagerDutyPreflightData
        ):
            raise TypeError("PagerDuty preparation must be PagerDutyPreflightData")

        settings = (
            preparation.settings
            if isinstance(preparation, PagerDutyPreflightData)
            else resolve_auth_settings(provider_options=self._provider_options(target))
        )
        account_id = settings.subdomain or target.name
        data = PagerDutyExecutionTargetData(
            account_id=account_id,
            settings=settings,
            session_factory=self._session_factory,
        )
        execution_target = ExecutionTarget(
            id=account_id,
            name=account_id,
            type="account",
            provider=self.metadata.name,
            regions=["global"],
            metadata={
                "pagerduty_account": account_id,
                "pagerduty_api_url": settings.api_url,
                "pagerduty_auth_type": settings.auth_type,
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
        """Prepare PagerDuty runtime state for one account target."""

        self.validate_target(target)
        if execution_target.provider != self.metadata.name:
            raise ValueError(
                f"Execution target provider '{execution_target.provider}' is not "
                "pagerduty"
            )
        if not isinstance(execution_target.provider_data, PagerDutyExecutionTargetData):
            raise TypeError(
                "PagerDuty execution target is missing PagerDutyExecutionTargetData"
            )
        return PagerDutyExecutionRuntime(data=execution_target.provider_data)

    def _provider_options(self, target: TargetDescriptor) -> dict[str, object]:
        """Return PagerDuty options with any Anvil profile expanded."""

        return self._profile_resolver.resolve(target.provider_options)

    @staticmethod
    def _validate_api_url(value: object) -> None:
        """Validate an optional PagerDuty REST API origin."""

        if value is None:
            return
        if not isinstance(value, str):
            return
        parsed = urlparse(value.strip())
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError(
                "PagerDuty provider.options.api_url must be an HTTPS origin "
                "without credentials, path, query, or fragment"
            )

    @staticmethod
    def _validate_subdomain(value: object) -> None:
        """Validate an optional PagerDuty account subdomain."""

        if value is None or not isinstance(value, str):
            return
        if SUBDOMAIN_PATTERN.fullmatch(value.strip()) is None:
            raise ValueError(
                "PagerDuty provider.options.subdomain must be a valid account subdomain"
            )


def create_provider_instance() -> PagerDutyProvider:
    """Create the first-party PagerDuty provider."""

    return PagerDutyProvider()
