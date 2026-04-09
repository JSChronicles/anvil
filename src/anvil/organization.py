from __future__ import annotations

import logging

import boto3
from boto3.session import Session

from anvil.account import Account
from anvil.descriptors import TargetDescriptor
from anvil.execution_context import ExecutionContext
from anvil.session import BOTO_CONFIG, SessionFactory

__LOGGER__ = logging.getLogger(__name__)


class OrganizationResolver:
    """
    Resolve executable accounts from an AWS Organizations-backed config entry.
    """

    def __init__(
        self,
        *,
        descriptor: TargetDescriptor,
        context: ExecutionContext,
        management_account_id: str | None = None,
        session_factory: SessionFactory | None = None,
        base_session: Session | None = None,
        discovered_accounts: dict[str, dict[str, str]] | None = None,
        enabled_regions: list[str] | None = None,
    ) -> None:
        self.descriptor = descriptor
        self.context = context
        self._management_account_id: str | None = management_account_id
        self._session_factory = session_factory or SessionFactory()
        self._base_session = base_session
        self._discovered_accounts = discovered_accounts
        self._enabled_regions = enabled_regions

    def resolve_accounts(self) -> list[Account]:
        __LOGGER__.info(
            f"Resolving organization accounts "
            f"(org={self.descriptor.name}, regions={self.context.regions})"
        )

        base_session = self._base_session or self._session_factory.create_base_session(
            profile_name=self.descriptor.profile, region_name=self.context.regions[0]
        )

        effective_regions = self._get_effective_regions(
            base_session, discovered_regions=self._enabled_regions
        )
        if not effective_regions:
            raise ValueError("No effective configured regions remain after validation.")

        # The runner may preflight organization identity up front and pass the
        # management account ID here so we do not need a second
        # describe_organization() call during execution.
        if self._management_account_id is not None:
            management_account_id = self._management_account_id
        else:
            _, management_account_id = self.describe_organization(base_session)

        return self._build_accounts(
            base_session=base_session,
            management_account_id=management_account_id,
            effective_regions=effective_regions,
            discovered_accounts=self._discovered_accounts,
        )

    @staticmethod
    def describe_organization(session: boto3.Session) -> tuple[str, str]:
        """
        Return the organization ID and management account ID.
        """
        org_client = session.client("organizations", config=BOTO_CONFIG)
        org = org_client.describe_organization()["Organization"]
        return org["Id"], org["MasterAccountId"]

    def _build_accounts(
        self,
        *,
        base_session: boto3.Session,
        management_account_id: str,
        effective_regions: list[str],
        discovered_accounts: dict[str, dict[str, str]] | None = None,
    ) -> list[Account]:
        """
        Build executable account objects for all selected target accounts.
        """
        all_accounts = discovered_accounts or self.discover_accounts(base_session)
        target_accounts = self._filter_accounts(all_accounts)

        accounts: list[Account] = []

        for info in target_accounts.values():
            account_id = info["account_number"]
            accounts.append(
                Account(
                    account_id=account_id,
                    account_alias=info["account_alias"],
                    is_management=account_id == management_account_id,
                    assume_role=account_id != management_account_id,
                    base_session=base_session,
                    context=self.context,
                    regions=effective_regions,
                    session_factory=self._session_factory,
                )
            )

        return accounts

    @staticmethod
    def discover_accounts(session: boto3.Session) -> dict[str, dict[str, str]]:
        """
        Discover all active accounts in the organization.
        """
        org_client = session.client("organizations", config=BOTO_CONFIG)
        paginator = org_client.get_paginator("list_accounts")

        accounts: dict[str, dict[str, str]] = {}

        for page in paginator.paginate():
            for account in page.get("Accounts", []):
                if account.get("Status") != "ACTIVE":
                    continue

                account_id = account["Id"]
                accounts[account_id] = {
                    "account_number": account_id,
                    "account_alias": account.get("Name", account_id),
                }

        return accounts

    @staticmethod
    def discover_enabled_regions(session: boto3.Session) -> list[str]:
        """
        Discover enabled AWS regions available to this organization context.
        """
        account_client = session.client("account", config=BOTO_CONFIG)
        paginator = account_client.get_paginator("list_regions")

        enabled_regions: set[str] = set()

        for page in paginator.paginate():
            for region in page.get("Regions", []):
                region_name = region.get("RegionName")
                region_status = region.get("RegionOptStatus")

                if region_name and region_status in {"ENABLED", "ENABLED_BY_DEFAULT"}:
                    enabled_regions.add(region_name)

        return sorted(enabled_regions)

    def _filter_accounts(
        self, all_accounts: dict[str, dict[str, str]]
    ) -> dict[str, dict[str, str]]:
        """
        Apply include/exclude account filters to discovered organization accounts.
        """
        discovered_ids = set(all_accounts.keys())

        if self.descriptor.include:
            include_set = set(self.descriptor.include)
            unknown_include_ids = sorted(include_set - discovered_ids)
            if unknown_include_ids:
                __LOGGER__.warning(
                    f"Org '{self.descriptor.name}' include list contains unknown "
                    f"account IDs: {', '.join(unknown_include_ids)}"
                )

            selected_ids = sorted(include_set & discovered_ids)
            return {account_id: all_accounts[account_id] for account_id in selected_ids}

        exclude_set = set(self.descriptor.exclude or [])
        unknown_exclude_ids = sorted(exclude_set - discovered_ids)
        if unknown_exclude_ids:
            __LOGGER__.warning(
                f"Org '{self.descriptor.name}' exclude list contains unknown "
                f"account IDs: {', '.join(unknown_exclude_ids)}"
            )

        remaining_ids = sorted(discovered_ids - exclude_set)
        return {account_id: all_accounts[account_id] for account_id in remaining_ids}

    def _get_effective_regions(
        self, session: boto3.Session, *, discovered_regions: list[str] | None = None
    ) -> list[str]:
        """
        Intersect configured regions with discovered enabled regions and warn on
        configured regions that are unavailable.
        """
        discovered_regions = set(
            discovered_regions or self.discover_enabled_regions(session)
        )
        configured_regions = list(self.context.regions)

        unavailable_regions = sorted(set(configured_regions) - discovered_regions)
        if unavailable_regions:
            __LOGGER__.warning(
                f"Org '{self.descriptor.name}' configured unavailable regions: "
                f"{', '.join(unavailable_regions)}"
            )

        return [region for region in configured_regions if region in discovered_regions]
