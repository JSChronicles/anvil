from __future__ import annotations

import boto3

from anvil.providers.base import ProviderRegion
from anvil.regions import (
    ENABLED_REGION_STATUSES,
    get_bootstrap_region,
    resolve_region_selectors,
)
from anvil.session import BOTO_CONFIG


class AwsRegionService:
    """AWS-owned region defaulting, discovery, and selector resolution."""

    def default_regions(self, *, configured_regions: list[str]) -> list[str]:
        """Return the configured AWS regions after descriptor defaults apply."""

        return list(configured_regions)

    def bootstrap_region(self, *, configured_regions: list[str]) -> str:
        """Return the concrete AWS region used for discovery calls."""

        return get_bootstrap_region(configured_regions)

    def discover_region_statuses(self, *, session: boto3.Session) -> dict[str, str]:
        """Discover AWS region opt-in statuses."""

        account_client = session.client("account", config=BOTO_CONFIG)
        paginator = account_client.get_paginator("list_regions")

        region_statuses: dict[str, str] = {}

        for page in paginator.paginate():
            for region in page.get("Regions", []):
                region_name = region.get("RegionName")
                region_status = region.get("RegionOptStatus")

                if region_name and region_status:
                    region_statuses[region_name] = region_status

        return dict(sorted(region_statuses.items()))

    def provider_regions_from_statuses(
        self, *, region_statuses: dict[str, str]
    ) -> list[ProviderRegion]:
        """Adapt AWS region statuses to provider-neutral region objects."""

        return [
            ProviderRegion(
                name=region, available=status in ENABLED_REGION_STATUSES, status=status
            )
            for region, status in sorted(region_statuses.items())
        ]

    def resolve_regions(
        self,
        *,
        target_name: str,
        configured_regions: list[str],
        region_statuses: dict[str, str],
    ) -> list[str]:
        """Resolve configured AWS regions/selectors against discovered statuses."""

        return resolve_region_selectors(
            target_name=target_name,
            configured_regions=list(configured_regions),
            region_statuses=region_statuses,
        )
