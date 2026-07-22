from __future__ import annotations

import fnmatch
import logging

__LOGGER__ = logging.getLogger(__name__)

ALL_REGION_SELECTOR = "all"
ENABLED_REGION_STATUSES = {"ENABLED", "ENABLED_BY_DEFAULT"}
REGION_GLOB_CHARS = frozenset("*?[")
DEFAULT_BOOTSTRAP_REGION = "us-east-1"


def is_region_glob(region: str) -> bool:
    """Return whether a configured region uses glob selector syntax."""
    return any(char in region for char in REGION_GLOB_CHARS)


def is_region_selector(region: str) -> bool:
    """Return whether a configured region must be expanded before execution.

    Concrete region names, such as us-east-1, can be passed directly to boto3.
    Selectors, such as all or us-*, describe a set of regions and must be
    resolved against discovered AWS regions first.
    """
    return region == ALL_REGION_SELECTOR or is_region_glob(region)


def get_bootstrap_region(configured_regions: list[str]) -> str:
    """Return a concrete region for preflight AWS discovery calls.

    Organization preflight needs a real boto3 region before region selectors can
    be expanded. Prefer the first explicit configured region when present;
    otherwise fall back to us-east-1 for discovery-only calls.
    """
    for region in configured_regions:
        if not is_region_selector(region):
            return region

    return DEFAULT_BOOTSTRAP_REGION


def resolve_region_selectors(
    *, target_name: str, configured_regions: list[str], region_statuses: dict[str, str]
) -> list[str]:
    """Resolve configured region selectors to concrete enabled regions.

    Args:
        target_name: Target name for log and error messages.
        configured_regions: Regions or selectors from YAML.
        region_statuses: Region name to AWS opt-in status from Account list_regions.

    Returns:
        Concrete enabled regions to execute.

    Raises:
        ValueError: If a selector matches no known region or no enabled regions remain.
    """
    return resolve_location_selectors(
        target_name=target_name,
        configured_locations=configured_regions,
        location_statuses=region_statuses,
        available_statuses=ENABLED_REGION_STATUSES,
        label="region",
    )


def resolve_location_selectors(
    *,
    target_name: str,
    configured_locations: list[str],
    location_statuses: dict[str, str],
    available_statuses: set[str],
    label: str = "location",
) -> list[str]:
    """Resolve location selectors to concrete available provider locations.

    Args:
        target_name: Target name for log and error messages.
        configured_locations: Locations or selectors from YAML.
        location_statuses: Location name to provider-owned availability status.
        available_statuses: Status values considered executable.
        label: Human-readable location kind for messages.

    Returns:
        Concrete available locations to execute.

    Raises:
        ValueError: If a selector matches no known location or no available
            locations remain.
    """
    known_locations = sorted(location_statuses)
    selected_locations: set[str] = set()
    selected_order: list[str] = []
    unmatched_selectors: list[str] = []

    def add_location(location: str) -> None:
        if location in selected_locations:
            return

        selected_locations.add(location)
        selected_order.append(location)

    if configured_locations == [ALL_REGION_SELECTOR]:
        for location in known_locations:
            add_location(location)
    else:
        for configured_location in configured_locations:
            if is_region_glob(configured_location):
                matches = [
                    location
                    for location in known_locations
                    if fnmatch.fnmatchcase(location, configured_location)
                ]
                if not matches:
                    unmatched_selectors.append(configured_location)
                    continue

                for location in matches:
                    add_location(location)
                continue

            add_location(configured_location)

    if unmatched_selectors:
        raise ValueError(
            f"Target '{target_name}' {label} selector(s) matched no known {label}s: "
            f"{', '.join(unmatched_selectors)}"
        )

    unavailable_locations = sorted(
        location
        for location in selected_locations
        if location_statuses.get(location) not in available_statuses
    )
    if unavailable_locations:
        __LOGGER__.warning(
            f"Target '{target_name}' configured unavailable {label}s: "
            f"{', '.join(unavailable_locations)}"
        )

    effective_locations = [
        location
        for location in selected_order
        if location_statuses.get(location) in available_statuses
    ]

    if not effective_locations:
        raise ValueError(f"No effective configured {label}s remain after validation.")

    return effective_locations
