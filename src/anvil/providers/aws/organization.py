from __future__ import annotations

import logging

import boto3
from boto3.session import Session

from anvil.providers.aws.account import Account, AccountAccessStrategy
from anvil.descriptors import TargetDescriptor
from anvil.execution_context import ExecutionContext
from anvil.providers.aws.regions import AwsRegionService
from anvil.providers.aws.config import DEFAULT_ORGANIZATION_ROLE_NAME, aws_option
from anvil.providers.aws.session import BOTO_CONFIG, SessionFactory

__LOGGER__ = logging.getLogger(__name__)

MANAGEMENT_ACCOUNT_KEYWORDS = frozenset({"management", "payer"})


def is_management_account_keyword(value: str) -> bool:
    """Return whether an AWS account filter names the management account."""

    return value.casefold() in MANAGEMENT_ACCOUNT_KEYWORDS


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
        base_session_account_id: str | None = None,
        session_factory: SessionFactory | None = None,
        base_session: Session | None = None,
        discovered_accounts: dict[str, dict[str, str]] | None = None,
        region_statuses: dict[str, str] | None = None,
    ) -> None:
        self.descriptor = descriptor
        self.context = context
        self._management_account_id: str | None = management_account_id
        self._base_session_account_id: str | None = base_session_account_id
        self._session_factory = session_factory or SessionFactory()
        self._region_service = AwsRegionService()
        self._base_session = base_session
        self._discovered_accounts = discovered_accounts
        self._region_statuses = region_statuses

    def resolve_accounts(self) -> list[Account]:
        __LOGGER__.info(
            f"Resolving organization accounts "
            f"(org={self.descriptor.name}, regions={self.context.regions})"
        )

        base_session = self._base_session or self._session_factory.create_base_session(
            profile_name=aws_option(self.descriptor, "profile"),
            region_name=self._region_service.bootstrap_region(
                configured_regions=self.context.regions
            ),
        )

        effective_regions = self._get_effective_regions(
            base_session, region_statuses=self._region_statuses
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

        if self._base_session_account_id is not None:
            base_session_account_id = self._base_session_account_id
        else:
            base_session_account_id = self.describe_base_session_account(base_session)

        return self._build_accounts(
            base_session=base_session,
            management_account_id=management_account_id,
            base_session_account_id=base_session_account_id,
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

    @staticmethod
    def describe_base_session_account(session: boto3.Session) -> str:
        """
        Return the AWS account ID backing the base session credentials.
        """
        sts_client = session.client("sts", config=BOTO_CONFIG)
        return sts_client.get_caller_identity()["Account"]

    def _build_accounts(
        self,
        *,
        base_session: boto3.Session,
        management_account_id: str,
        base_session_account_id: str,
        effective_regions: list[str],
        discovered_accounts: dict[str, dict[str, str]] | None = None,
    ) -> list[Account]:
        """
        Build executable account objects for all selected target accounts.
        """
        all_accounts = discovered_accounts or self.discover_accounts(base_session)
        target_accounts = self._filter_accounts(
            all_accounts, management_account_id=management_account_id
        )

        accounts: list[Account] = []

        for info in target_accounts.values():
            account_id = info["account_number"]
            is_management = account_id == management_account_id
            access_strategy = (
                AccountAccessStrategy.BASE_SESSION
                if account_id == base_session_account_id
                else AccountAccessStrategy.ASSUME_ROLE
            )
            accounts.append(
                Account(
                    account_id=account_id,
                    account_alias=info["account_alias"],
                    is_management=is_management,
                    access_strategy=access_strategy,
                    role_name=(
                        aws_option(self.descriptor, "role_name")
                        or DEFAULT_ORGANIZATION_ROLE_NAME
                    ),
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

    def _filter_accounts(
        self, all_accounts: dict[str, dict[str, str]], *, management_account_id: str
    ) -> dict[str, dict[str, str]]:
        """
        Apply include/exclude account filters to discovered organization accounts.
        """
        discovered_ids = set(all_accounts.keys())

        if self.descriptor.include:
            include_set = self._resolve_account_filter_keywords(
                values=self.descriptor.include,
                management_account_id=management_account_id,
            )
            unknown_include_ids = sorted(include_set - discovered_ids)
            if unknown_include_ids:
                __LOGGER__.warning(
                    f"Org '{self.descriptor.name}' include list contains unknown "
                    f"account IDs: {', '.join(unknown_include_ids)}"
                )

            selected_ids = sorted(include_set & discovered_ids)
            return {account_id: all_accounts[account_id] for account_id in selected_ids}

        exclude_set = self._resolve_account_filter_keywords(
            values=self.descriptor.exclude or [],
            management_account_id=management_account_id,
        )
        unknown_exclude_ids = sorted(exclude_set - discovered_ids)
        if unknown_exclude_ids:
            __LOGGER__.warning(
                f"Org '{self.descriptor.name}' exclude list contains unknown "
                f"account IDs: {', '.join(unknown_exclude_ids)}"
            )

        remaining_ids = sorted(discovered_ids - exclude_set)
        return {account_id: all_accounts[account_id] for account_id in remaining_ids}

    @staticmethod
    def _resolve_account_filter_keywords(
        *, values: list[str], management_account_id: str
    ) -> set[str]:
        """Expand AWS-owned account filter keywords to concrete account IDs."""

        return {
            management_account_id if is_management_account_keyword(value) else value
            for value in values
        }

    def _get_effective_regions(
        self, session: boto3.Session, *, region_statuses: dict[str, str] | None = None
    ) -> list[str]:
        """
        Resolve configured regions and selectors against discovered region statuses.
        """
        if region_statuses is None:
            region_statuses = self._region_service.discover_region_statuses(
                session=session
            )

        return self._region_service.resolve_regions(
            target_name=self.descriptor.name,
            configured_regions=list(self.context.regions),
            region_statuses=region_statuses,
        )
