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

DEFAULT_GCP_LOCATIONS = ["us-central1"]


@dataclass(frozen=True, slots=True)
class GcpExecutionTargetData:
    """GCP-specific data needed to prepare one project runtime."""

    project_id: str
    locations: list[str]
    session_factory: "GcpSessionFactory"


@dataclass(frozen=True, slots=True)
class GcpSession:
    """Lazy GCP runtime session for one project and location."""

    project_id: str
    location: str
    credentials: object


class GcpSessionFactory:
    """Create GCP credentials lazily so provider validation has no SDK dependency."""

    def create_session(self, *, project_id: str, location: str) -> GcpSession:
        """Create a GCP session for a project/location pair."""

        try:
            import google.auth
        except ImportError as error:
            raise RuntimeError(
                "GCP provider requires optional dependency 'google-auth' "
                "when building a GCP runtime session."
            ) from error

        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        return GcpSession(
            project_id=project_id, location=location, credentials=credentials
        )


class GcpExecutionRuntime:
    """GCP runtime adapter for one explicit project target."""

    def __init__(self, *, data: GcpExecutionTargetData) -> None:
        self._data = data

    def build_session(self, *, region: str) -> GcpSession:
        """Build a lazy GCP session for one location."""

        return self._data.session_factory.create_session(
            project_id=self._data.project_id, location=region
        )

    def record_region_outcome(
        self, *, region: str, duration_seconds: float, failed: bool, interrupted: bool
    ) -> None:
        """GCP runtime currently has no adaptive lifecycle state."""

    def close(self) -> None:
        """GCP runtime currently has no explicit resources to release."""


class GcpProvider:
    """Minimal GCP provider for explicit project targets."""

    metadata = ProviderMetadata(
        name="gcp", display_name="GCP", description="Google Cloud provider"
    )

    def __init__(self, *, session_factory: GcpSessionFactory | None = None) -> None:
        self._session_factory = session_factory or GcpSessionFactory()

    def validate_target(self, target: TargetDescriptor) -> None:
        """Validate minimal GCP support: explicit project targets only."""

        if target.config_branch is not ConfigBranch.ACCOUNTS:
            raise ValueError("GCP provider currently supports explicit projects only")
        if not target.include:
            raise ValueError("GCP provider requires explicit project IDs")
        if target.exclude is not None:
            raise ValueError("GCP provider does not support exclude")

    def default_regions(self, target: TargetDescriptor) -> list[str]:
        """Return configured GCP locations or the minimal default."""

        self.validate_target(target)
        if target.regions == ["us-east-1"]:
            return list(DEFAULT_GCP_LOCATIONS)
        return list(target.regions or DEFAULT_GCP_LOCATIONS)

    def auth_cache_key(self, target: TargetDescriptor) -> object | None:
        """Return a provider auth cache identity without loading GCP SDKs."""

        return (self.metadata.name, target.profile)

    def auth_check(self, target: TargetDescriptor) -> ProviderAuthResult:
        """Report deferred GCP auth checks without live SDK calls."""

        self.validate_target(target)
        return ProviderAuthResult(
            status=ExecutionStatus.SUCCESS,
            source="deferred",
            message="GCP authentication is validated when a runtime session is built.",
        )

    def discover_regions(self, target: TargetDescriptor) -> list[ProviderRegion]:
        """Return configured/default GCP locations without live discovery."""

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
        """Resolve explicit GCP project IDs deterministically."""

        self.validate_target(target)
        if exclude is not None:
            raise ValueError("GCP provider does not support exclude")

        project_ids = include or target.include or []
        execution_targets = [
            self._execution_target(project_id=project_id, locations=regions)
            for project_id in project_ids
        ]
        return ProviderExecutionPlan(execution_targets=execution_targets)

    def prepare_execution_runtime(
        self,
        *,
        target: TargetDescriptor,
        execution_target: ExecutionTarget,
        context: ExecutionContext,
    ) -> ProviderExecutionRuntime:
        """Prepare GCP runtime state for one explicit project."""

        self.validate_target(target)
        if execution_target.provider != self.metadata.name:
            raise ValueError(
                f"Execution target provider '{execution_target.provider}' is not gcp"
            )
        if not isinstance(execution_target.provider_data, GcpExecutionTargetData):
            raise TypeError("GCP execution target is missing GcpExecutionTargetData")

        return GcpExecutionRuntime(data=execution_target.provider_data)

    def _execution_target(
        self, *, project_id: str, locations: list[str]
    ) -> ExecutionTarget:
        data = GcpExecutionTargetData(
            project_id=project_id,
            locations=list(locations),
            session_factory=self._session_factory,
        )
        return ExecutionTarget(
            id=project_id,
            name=project_id,
            type="project",
            provider=self.metadata.name,
            metadata={"project_id": project_id},
            provider_data=data,
        )


def create_provider() -> GcpProvider:
    """Create the first-party GCP provider."""

    return GcpProvider()
