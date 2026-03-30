from __future__ import annotations

import logging
from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed

import boto3

from anvil.account import Account
from anvil.execution_context import ExecutionContext
from anvil.results import AccountResult, OrgResult
from anvil.session import BOTO_CONFIG, SessionFactory

__LOGGER__ = logging.getLogger(__name__)


class Organization:
    """
    Executable AWS Organization.
    """

    def __init__(
        self,
        *,
        name: str,
        profile_name: str | None,
        max_workers: int,
        include_ids: list[str] | None,
        exclude_ids: list[str] | None,
        context: ExecutionContext,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self.name = name
        self.profile_name = profile_name
        self.max_workers = max_workers
        self.include_ids = include_ids
        self.exclude_ids = exclude_ids
        self.context = context
        self._session_factory = session_factory or SessionFactory()

    def execute(self) -> OrgResult:
        """
        Execute this organization across all selected accounts.
        """
        __LOGGER__.info(
            f"Starting organization processing "
            f"(org={self.name}, regions={self.context.regions})"
        )

        base_session = self._session_factory.create_base_session(
            profile_name=self.profile_name, region_name=self.context.regions[0]
        )

        effective_regions = self._get_effective_regions(base_session)
        if not effective_regions:
            return OrgResult.create(
                org_name=self.name,
                dry_run=self.context.dry_run,
                account_results=[],
                error="No effective configured regions remain after validation.",
            )

        management_account_id = self._get_management_account_id(base_session)
        accounts: list[Account] = self._build_accounts(
            base_session=base_session,
            management_account_id=management_account_id,
            effective_regions=effective_regions,
        )

        account_results: list[AccountResult] = []

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {
                executor.submit(account.execute): account for account in accounts
            }
            fail_fast_triggered = False

            try:
                for future in as_completed(futures):
                    try:
                        account_result: AccountResult = future.result()
                    except CancelledError:
                        continue

                    account_results.append(account_result)

                    if (
                        self.context.fail_fast
                        and account_result.status.is_unsuccessful
                        and not fail_fast_triggered
                    ):
                        __LOGGER__.critical(f"Fail-fast triggered in org '{self.name}'")

                        # Signal cooperative cancellation to all running tasks
                        self.context.cancel_event.set()
                        fail_fast_triggered = True

                        # Cancel all pending futures
                        for pending in futures:
                            if not pending.done():
                                pending.cancel()

            except Exception:
                executor.shutdown(cancel_futures=True)
                raise

        account_results.sort(
            key=lambda result: (result.account_alias.lower(), result.account_id)
        )

        return OrgResult.create(
            org_name=self.name,
            dry_run=self.context.dry_run,
            account_results=account_results,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _get_management_account_id(self, session: boto3.Session) -> str:
        """
        Return the management account ID for this AWS Organization.
        """
        org_client = session.client("organizations", config=BOTO_CONFIG)
        org = org_client.describe_organization()["Organization"]
        return org["MasterAccountId"]

    def _build_accounts(
        self,
        *,
        base_session: boto3.Session,
        management_account_id: str,
        effective_regions: list[str],
    ) -> list[Account]:
        """
        Build executable account objects for all selected target accounts.
        """
        all_accounts = self._discover_accounts(base_session)
        target_accounts = self._filter_accounts(all_accounts)

        accounts: list[Account] = []

        for info in target_accounts.values():
            account_id = info["account_number"]
            accounts.append(
                Account(
                    account_id=account_id,
                    account_alias=info["account_alias"],
                    is_management=account_id == management_account_id,
                    base_session=base_session,
                    context=self.context,
                    regions=effective_regions,
                    session_factory=self._session_factory,
                )
            )

        return accounts

    def _discover_accounts(self, session: boto3.Session) -> dict[str, dict[str, str]]:
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

    def _discover_enabled_regions(self, session: boto3.Session) -> list[str]:
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

        if self.include_ids:
            include_set = set(self.include_ids)
            unknown_include_ids = sorted(include_set - discovered_ids)
            if unknown_include_ids:
                __LOGGER__.warning(
                    f"Org '{self.name}' include list contains unknown account IDs: {', '.join(unknown_include_ids)}"
                )

            selected_ids = sorted(include_set & discovered_ids)
            return {account_id: all_accounts[account_id] for account_id in selected_ids}

        exclude_set = set(self.exclude_ids or [])
        unknown_exclude_ids = sorted(exclude_set - discovered_ids)
        if unknown_exclude_ids:
            __LOGGER__.warning(
                f"Org '{self.name}' exclude list contains unknown account IDs: {', '.join(unknown_exclude_ids)}"
            )

        remaining_ids = sorted(discovered_ids - exclude_set)
        return {account_id: all_accounts[account_id] for account_id in remaining_ids}

    def _get_effective_regions(self, session: boto3.Session) -> list[str]:
        """
        Intersect configured regions with discovered enabled regions and warn on
        configured regions that are unavailable.
        """
        discovered_regions = set(self._discover_enabled_regions(session))
        configured_regions = list(self.context.regions)

        unavailable_regions = sorted(set(configured_regions) - discovered_regions)
        if unavailable_regions:
            __LOGGER__.warning(
                f"Org '{self.name}' configured unavailable regions: {', '.join(unavailable_regions)}"
            )

        return [region for region in configured_regions if region in discovered_regions]
