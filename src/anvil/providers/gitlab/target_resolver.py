"""GitLab group and project discovery and canonicalization."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Protocol, cast

from anvil.providers.base import ProviderPreparationCache
from anvil.providers.gitlab.auth import GitLabAuthSettings
from anvil.providers.gitlab.config import MODE_GROUPS, MODE_PROJECTS
from anvil.providers.gitlab.session import GitLabSessionFactory


@dataclass(frozen=True, slots=True)
class GitLabResource:
    """Canonical identity for one GitLab group or project."""

    id: int
    full_path: str
    type: str
    metadata: dict[str, object] = field(default_factory=dict)


class _GitLabResourceManager(Protocol):
    """python-gitlab manager operations used for target resolution."""

    def get(self, selector: str | int) -> object:
        """Retrieve one resource by numeric ID or full path."""

        ...

    def list(self, **kwargs: object) -> Iterable[object]:
        """List resources through a lazy paginated iterator."""

        ...


class GitLabTargetResolver:
    """Resolve canonical GitLab resources before scheduler admission."""

    def __init__(self, *, session_factory: GitLabSessionFactory) -> None:
        """Initialize the resolver with an injectable client factory."""

        self._session_factory = session_factory

    def resolve(
        self,
        *,
        mode: str,
        include: list[str] | None,
        exclude: list[str] | None,
        settings: GitLabAuthSettings,
        cache: ProviderPreparationCache,
    ) -> list[GitLabResource]:
        """Resolve selected GitLab groups or projects deterministically.

        Args:
            mode: Provider-owned ``groups`` or ``projects`` mode.
            include: Explicit numeric IDs or full paths.
            exclude: IDs or full paths excluded from discovery.
            settings: Resolved instance and authentication settings.
            cache: Provider preparation single-flight cache.

        Returns:
            Canonical resources sorted by numeric GitLab ID.

        Raises:
            RuntimeError: If GitLab discovery returns malformed or duplicate data.
            ValueError: If selectors are unknown or resolve to duplicate resources.
        """

        target_type = _target_type_for_mode(mode)
        client: object | None = None

        def get_client() -> object:
            nonlocal client
            if client is None:
                client = self._session_factory.create_client(settings=settings)
            return client

        try:
            if include is not None:
                resources = [
                    self._resolve_explicit_resource(
                        client=get_client,
                        selector=selector,
                        target_type=target_type,
                        settings=settings,
                        cache=cache,
                    )
                    for selector in include
                ]
            else:
                resources = self._discover_resources(
                    client=get_client,
                    target_type=target_type,
                    settings=settings,
                    cache=cache,
                )
                resources = self._apply_exclude(
                    resources=resources, exclude=exclude, target_type=target_type
                )
        finally:
            if client is not None:
                self._session_factory.close_client(client)

        _validate_unique_resources(resources=resources, source="selection")
        return sorted(resources, key=lambda resource: resource.id)

    def _resolve_explicit_resource(
        self,
        *,
        client: Callable[[], object],
        selector: str,
        target_type: str,
        settings: GitLabAuthSettings,
        cache: ProviderPreparationCache,
    ) -> GitLabResource:
        cache_key = (
            "gitlab",
            "resource",
            settings.cache_identity(),
            target_type,
            "get",
            selector,
        )

        def retrieve() -> object:
            manager = _resource_manager(client=client(), target_type=target_type)
            try:
                resource = manager.get(_selector_value(selector))
            except Exception as error:
                raise RuntimeError(
                    f"GitLab {target_type} selector '{selector}' could not be "
                    f"resolved on '{settings.url}': {settings.redact(str(error))}"
                ) from error
            return _resource_from_api(resource=resource, target_type=target_type)

        cached, _cache_hit, _cache_waited = cache.get_or_create(
            key=cache_key, create=retrieve
        )
        if not isinstance(cached, GitLabResource):
            raise RuntimeError("GitLab resource cache returned an unexpected value")
        return cached

    def _discover_resources(
        self,
        *,
        client: Callable[[], object],
        target_type: str,
        settings: GitLabAuthSettings,
        cache: ProviderPreparationCache,
    ) -> list[GitLabResource]:
        cache_key = (
            "gitlab",
            "resource",
            settings.cache_identity(),
            target_type,
            "list",
        )

        def discover() -> object:
            manager = _resource_manager(client=client(), target_type=target_type)
            list_options: dict[str, object] = {
                "iterator": True,
                "order_by": "id",
                "sort": "asc",
            }
            if target_type == "project":
                list_options.update({"membership": True, "pagination": "keyset"})
            else:
                list_options["all_available"] = False
            try:
                listed = manager.list(**list_options)
                resources = [
                    _resource_from_api(resource=item, target_type=target_type)
                    for item in listed
                ]
            except Exception as error:
                raise RuntimeError(
                    f"GitLab {target_type} discovery failed on '{settings.url}': "
                    f"{settings.redact(str(error))}"
                ) from error
            _validate_unique_resources(resources=resources, source="discovery")
            return sorted(resources, key=lambda resource: resource.id)

        cached, _cache_hit, _cache_waited = cache.get_or_create(
            key=cache_key, create=discover
        )
        if not isinstance(cached, list) or any(
            not isinstance(resource, GitLabResource) for resource in cached
        ):
            raise RuntimeError("GitLab discovery cache returned an unexpected value")
        return cast(list[GitLabResource], list(cached))

    @staticmethod
    def _apply_exclude(
        *, resources: list[GitLabResource], exclude: list[str] | None, target_type: str
    ) -> list[GitLabResource]:
        if exclude is None:
            return resources

        matched = {
            selector
            for selector in exclude
            if any(_selector_matches(resource, selector) for resource in resources)
        }
        unknown = [selector for selector in exclude if selector not in matched]
        if unknown:
            raise ValueError(
                f"GitLab exclude filter matched unknown {target_type} selectors: "
                f"{', '.join(unknown)}"
            )
        return [
            resource
            for resource in resources
            if not any(_selector_matches(resource, selector) for selector in exclude)
        ]


def _target_type_for_mode(mode: str) -> str:
    """Return the execution-target type for a GitLab provider mode."""

    if mode == MODE_GROUPS:
        return "group"
    if mode == MODE_PROJECTS:
        return "project"
    raise ValueError(f"Unsupported GitLab target mode: {mode}")


def _resource_manager(*, client: object, target_type: str) -> _GitLabResourceManager:
    """Return a GitLab group or project manager from a client."""

    manager_name = "groups" if target_type == "group" else "projects"
    manager = getattr(client, manager_name, None)
    if (
        manager is None
        or not callable(getattr(manager, "get", None))
        or not callable(getattr(manager, "list", None))
    ):
        raise RuntimeError(
            f"python-gitlab client does not expose a usable {manager_name} manager"
        )
    return cast(_GitLabResourceManager, manager)


def _selector_value(selector: str) -> str | int:
    """Use an integer for decimal GitLab IDs and preserve path selectors."""

    return int(selector) if selector.isdigit() else selector


def _resource_from_api(*, resource: object, target_type: str) -> GitLabResource:
    """Convert a python-gitlab object into a canonical resource identity."""

    raw_id = _api_attribute(resource, "id")
    if isinstance(raw_id, bool):
        raw_id = None
    if isinstance(raw_id, str) and raw_id.isdigit():
        raw_id = int(raw_id)
    if not isinstance(raw_id, int) or raw_id <= 0:
        raise RuntimeError(f"GitLab {target_type} response is missing a numeric id")

    path_attribute = "full_path" if target_type == "group" else "path_with_namespace"
    raw_path = _api_attribute(resource, path_attribute)
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise RuntimeError(
            f"GitLab {target_type} {raw_id} response is missing {path_attribute}"
        )

    metadata: dict[str, object] = {}
    if target_type == "group":
        parent_id = _api_attribute(resource, "parent_id")
        if isinstance(parent_id, int) and not isinstance(parent_id, bool):
            metadata["parent_id"] = parent_id
    else:
        namespace = _api_attribute(resource, "namespace")
        if isinstance(namespace, Mapping):
            namespace_id = namespace.get("id")
            namespace_path = namespace.get("full_path")
            if isinstance(namespace_id, int) and not isinstance(namespace_id, bool):
                metadata["namespace_id"] = namespace_id
            if isinstance(namespace_path, str) and namespace_path.strip():
                metadata["namespace_path"] = namespace_path.strip()

    return GitLabResource(
        id=raw_id, full_path=raw_path.strip(), type=target_type, metadata=metadata
    )


def _api_attribute(resource: object, name: str) -> object:
    """Read one attribute from a python-gitlab object or structural test double."""

    value = getattr(resource, name, None)
    if value is not None:
        return value
    attributes = getattr(resource, "attributes", None)
    if isinstance(attributes, Mapping):
        return attributes.get(name)
    return None


def _selector_matches(resource: GitLabResource, selector: str) -> bool:
    """Return whether an ID or path selector identifies a resource."""

    return selector == str(resource.id) or selector == resource.full_path


def _validate_unique_resources(*, resources: list[GitLabResource], source: str) -> None:
    """Reject duplicate canonical resource IDs from selection or discovery."""

    seen: set[int] = set()
    duplicates: set[int] = set()
    for resource in resources:
        if resource.id in seen:
            duplicates.add(resource.id)
        seen.add(resource.id)
    if duplicates:
        duplicate_display = ", ".join(
            str(resource_id) for resource_id in sorted(duplicates)
        )
        raise ValueError(
            f"GitLab {source} resolved duplicate canonical IDs: {duplicate_display}"
        )
