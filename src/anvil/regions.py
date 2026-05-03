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
    known_regions = sorted(region_statuses)
    selected_regions: set[str] = set()
    selected_order: list[str] = []
    unmatched_selectors: list[str] = []

    def add_region(region: str) -> None:
        if region in selected_regions:
            return

        selected_regions.add(region)
        selected_order.append(region)

    if configured_regions == [ALL_REGION_SELECTOR]:
        for region in known_regions:
            add_region(region)
    else:
        for configured_region in configured_regions:
            if is_region_glob(configured_region):
                matches = [
                    region
                    for region in known_regions
                    if fnmatch.fnmatchcase(region, configured_region)
                ]
                if not matches:
                    unmatched_selectors.append(configured_region)
                    continue

                for region in matches:
                    add_region(region)
                continue

            add_region(configured_region)

    if unmatched_selectors:
        raise ValueError(
            f"Target '{target_name}' region selector(s) matched no known regions: "
            f"{', '.join(unmatched_selectors)}"
        )

    unavailable_regions = sorted(
        region
        for region in selected_regions
        if region_statuses.get(region) not in ENABLED_REGION_STATUSES
    )
    if unavailable_regions:
        __LOGGER__.warning(
            f"Target '{target_name}' configured unavailable regions: "
            f"{', '.join(unavailable_regions)}"
        )

    effective_regions = [
        region
        for region in selected_order
        if region_statuses.get(region) in ENABLED_REGION_STATUSES
    ]

    if not effective_regions:
        raise ValueError("No effective configured regions remain after validation.")

    return effective_regions
