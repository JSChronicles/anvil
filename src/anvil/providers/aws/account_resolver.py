from __future__ import annotations

import logging

from boto3.session import Session

from anvil.providers.aws.account import Account, AccountAccessStrategy
from anvil.descriptors import TargetDescriptor
from anvil.execution_context import ExecutionContext
from anvil.providers.aws.config import aws_option
from anvil.providers.aws.session import SessionFactory

__LOGGER__ = logging.getLogger(__name__)


class AccountResolver:
    """
    Resolve executable accounts from an explicit account-list config entry.
    """

    def __init__(
        self,
        *,
        descriptor: TargetDescriptor,
        context: ExecutionContext,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self.descriptor: TargetDescriptor = descriptor
        self.context: ExecutionContext = context
        self._session_factory: SessionFactory = session_factory or SessionFactory()

    def resolve_accounts(self) -> list[Account]:
        __LOGGER__.info(
            f"Resolving explicit accounts "
            f"(name={self.descriptor.name}, count={len(self.descriptor.include or [])}, "
            f"regions={self.context.regions})"
        )

        base_session: Session = self._session_factory.create_base_session(
            profile_name=aws_option(self.descriptor, "profile"),
            region_name=self.context.regions[0],
        )

        accounts: list[Account] = []

        for account_id in self.descriptor.include or []:
            access_strategy = (
                AccountAccessStrategy.ASSUME_ROLE
                if aws_option(self.descriptor, "role_name") is not None
                else AccountAccessStrategy.DIRECT_PROFILE
            )
            accounts.append(
                Account(
                    account_id=account_id,
                    account_alias=account_id,
                    is_management=False,
                    access_strategy=access_strategy,
                    role_name=aws_option(self.descriptor, "role_name"),
                    base_session=base_session,
                    context=self.context,
                    regions=list(self.context.regions),
                    session_factory=self._session_factory,
                )
            )

        return accounts
