from __future__ import annotations

import threading
from collections.abc import Callable
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
class GcpProject:
    """GCP project identity discovered from Cloud Resource Manager."""

    project_id: str
    display_name: str | None = None


@dataclass(slots=True)
class _GcpProjectDiscoveryFlight:
    event: threading.Event
    projects: list[GcpProject] | None = None
    error: BaseException | None = None


class _GcpProjectDiscoveryCache:
    """Single-flight cache for GCP project discovery only."""

    def __init__(self) -> None:
        self._values: dict[object, list[GcpProject]] = {}
        self._flights: dict[object, _GcpProjectDiscoveryFlight] = {}
        self._lock = threading.Lock()

    def get_or_discover(
        self, *, key: object, discover: Callable[[], list[GcpProject]]
    ) -> list[GcpProject]:
        with self._lock:
            cached = self._values.get(key)
            if cached is not None:
                return list(cached)

            flight = self._flights.get(key)
            if flight is None:
                flight = _GcpProjectDiscoveryFlight(event=threading.Event())
                self._flights[key] = flight
                owns_discovery = True
            else:
                owns_discovery = False

        if owns_discovery:
            try:
                projects = list(discover())
            except BaseException as error:
                with self._lock:
                    flight.error = error
                    self._flights.pop(key, None)
                    flight.event.set()
                raise

            with self._lock:
                cached = self._values.get(key)
                stored = list(cached) if cached is not None else projects
                self._values[key] = list(stored)
                flight.projects = list(stored)
                self._flights.pop(key, None)
                flight.event.set()

            return list(stored)

        flight.event.wait()
        if flight.error is not None:
            raise flight.error
        if flight.projects is None:
            raise RuntimeError("GCP project discovery completed empty")
        return list(flight.projects)


_GCP_PROJECT_DISCOVERY_CACHE = _GcpProjectDiscoveryCache()


@dataclass(frozen=True, slots=True)
class GcpExecutionTargetData:
    """GCP-specific data needed to prepare one project runtime."""

    project_id: str
    locations: list[str]
    credentials_path: str | None
    quota_project_id: str | None
    session_factory: "GcpSessionFactory"


@dataclass(frozen=True, slots=True)
class GcpSession:
    """Lazy GCP runtime session for one project and location."""

    project_id: str
    location: str
    credentials: object
    quota_project_id: str | None = None

    @property
    def region_name(self) -> str:
        """Compatibility alias used by existing task call signatures."""

        return self.location


class GcpSessionFactory:
    """Create GCP credentials lazily so provider validation has no SDK dependency."""

    def _build_credentials(
        self,
        *,
        credentials_path: str | None = None,
        quota_project_id: str | None = None,
    ) -> object:
        try:
            import google.auth
        except ImportError as error:
            raise RuntimeError(
                "GCP provider requires optional dependency 'google-auth' "
                "when building a GCP runtime session."
            ) from error

        try:
            scopes = ["https://www.googleapis.com/auth/cloud-platform"]
            if credentials_path is not None:
                credentials, _ = google.auth.load_credentials_from_file(
                    credentials_path,
                    scopes=scopes,
                    quota_project_id=quota_project_id,
                )
            else:
                credentials, _ = google.auth.default(
                    scopes=scopes, quota_project_id=quota_project_id
                )
            return credentials
        except Exception as error:
            raise RuntimeError(
                "GCP provider could not build a runtime session from configured "
                f"credentials: {error}"
            ) from error

    def create_session(
        self,
        *,
        project_id: str,
        location: str,
        credentials_path: str | None = None,
        quota_project_id: str | None = None,
    ) -> GcpSession:
        """Create a GCP session for a project/location pair."""

        credentials = self._build_credentials(
            credentials_path=credentials_path, quota_project_id=quota_project_id
        )
        return GcpSession(
            project_id=project_id,
            location=location,
            credentials=credentials,
            quota_project_id=quota_project_id,
        )

    def list_projects(
        self,
        *,
        credentials_path: str | None = None,
        quota_project_id: str | None = None,
    ) -> list[GcpProject]:
        """List GCP projects lazily through Cloud Resource Manager."""

        credentials = self._build_credentials(
            credentials_path=credentials_path, quota_project_id=quota_project_id
        )
        try:
            from google.cloud import resourcemanager_v3
        except ImportError as error:
            raise RuntimeError(
                "GCP project discovery requires optional dependency "
                "'google-cloud-resource-manager'."
            ) from error

        try:
            client = resourcemanager_v3.ProjectsClient(credentials=credentials)
            projects = []
            for project in client.search_projects():
                project_id = getattr(project, "project_id", None)
                display_name = getattr(project, "display_name", None)
                if isinstance(project_id, str) and project_id.strip():
                    projects.append(
                        GcpProject(
                            project_id=project_id.strip(),
                            display_name=display_name
                            if isinstance(display_name, str)
                            else None,
                        )
                    )
            return sorted(projects, key=lambda item: item.project_id)
        except RuntimeError:
            raise
        except Exception as error:
            raise RuntimeError(
                "GCP provider could not discover projects: "
                f"{error}"
            ) from error


class GcpExecutionRuntime:
    """GCP runtime adapter for one explicit project target."""

    def __init__(self, *, data: GcpExecutionTargetData) -> None:
        self._data = data

    def build_session(self, *, region: str) -> GcpSession:
        """Build a lazy GCP session for one location."""

        return self._data.session_factory.create_session(
            project_id=self._data.project_id,
            location=region,
            credentials_path=self._data.credentials_path,
            quota_project_id=self._data.quota_project_id,
        )

    def record_region_outcome(
        self, *, region: str, duration_seconds: float, failed: bool, interrupted: bool
    ) -> None:
        """GCP runtime currently has no adaptive lifecycle state."""

    def close(self) -> None:
        """GCP runtime currently has no explicit resources to release."""


class GcpProvider:
    """GCP provider for explicit and discovered project targets."""

    metadata = ProviderMetadata(
        name="gcp", display_name="GCP", description="Google Cloud provider"
    )

    def __init__(self, *, session_factory: GcpSessionFactory | None = None) -> None:
        self._session_factory = session_factory or GcpSessionFactory()

    def validate_target(self, target: TargetDescriptor) -> None:
        """Validate GCP support: project targets only."""

        if target.config_branch is not ConfigBranch.ACCOUNTS:
            raise ValueError(
                "GCP provider currently supports projects only from accounts"
            )
        if target.include is not None and target.exclude is not None:
            raise ValueError("GCP include and exclude filters are mutually exclusive")

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
        """Resolve GCP project IDs deterministically."""

        self.validate_target(target)

        if target.include is None:
            projects = self._discover_projects(provider_options=target.provider_options)
            project_ids = self._filter_discovered_project_ids(
                projects=projects,
                include=include,
                exclude=exclude,
            )
        else:
            project_ids = include or target.include
        execution_targets = [
            self._execution_target(
                project_id=project_id,
                locations=regions,
                provider_options=target.provider_options,
            )
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
        self,
        *,
        project_id: str,
        locations: list[str],
        provider_options: dict[str, object],
    ) -> ExecutionTarget:
        data = GcpExecutionTargetData(
            project_id=project_id,
            locations=list(locations),
            credentials_path=self._string_option(
                provider_options=provider_options, option_name="credentials_path"
            ),
            quota_project_id=self._string_option(
                provider_options=provider_options, option_name="quota_project_id"
            ),
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

    def _discover_projects(
        self, *, provider_options: dict[str, object]
    ) -> list[GcpProject]:
        credentials_path = self._string_option(
            provider_options=provider_options, option_name="credentials_path"
        )
        quota_project_id = self._string_option(
            provider_options=provider_options, option_name="quota_project_id"
        )
        if type(self._session_factory) is not GcpSessionFactory:
            return self._session_factory.list_projects(
                credentials_path=credentials_path,
                quota_project_id=quota_project_id,
            )

        discovery_key = self._project_discovery_cache_key(
            credentials_path=credentials_path,
            quota_project_id=quota_project_id,
        )
        return _GCP_PROJECT_DISCOVERY_CACHE.get_or_discover(
            key=discovery_key,
            discover=lambda: self._session_factory.list_projects(
                credentials_path=credentials_path,
                quota_project_id=quota_project_id,
            ),
        )

    def _project_discovery_cache_key(
        self,
        *,
        credentials_path: str | None,
        quota_project_id: str | None,
    ) -> object:
        return (
            GcpSessionFactory,
            credentials_path,
            quota_project_id,
        )

    def _filter_discovered_project_ids(
        self,
        *,
        projects: list[GcpProject],
        include: list[str] | None,
        exclude: list[str] | None,
    ) -> list[str]:
        discovered_ids = sorted(project.project_id for project in projects)
        discovered_set = set(discovered_ids)

        if include is not None:
            unknown = [project_id for project_id in include if project_id not in discovered_set]
            if unknown:
                unknown_display = ", ".join(unknown)
                raise ValueError(
                    "GCP include filter matched unknown project IDs: "
                    f"{unknown_display}"
                )
            project_ids = [project_id for project_id in include]
        else:
            project_ids = discovered_ids

        if exclude is not None:
            unknown = [project_id for project_id in exclude if project_id not in discovered_set]
            if unknown:
                unknown_display = ", ".join(unknown)
                raise ValueError(
                    "GCP exclude filter matched unknown project IDs: "
                    f"{unknown_display}"
                )
            excluded = set(exclude)
            project_ids = [
                project_id
                for project_id in project_ids
                if project_id not in excluded
            ]

        return project_ids

    def _string_option(
        self, *, provider_options: dict[str, object], option_name: str
    ) -> str | None:
        option = provider_options.get(option_name)
        return option if isinstance(option, str) else None


def create_provider() -> GcpProvider:
    """Create the first-party GCP provider."""

    return GcpProvider()
