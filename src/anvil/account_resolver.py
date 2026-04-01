from __future__ import annotations

import logging

from anvil.account import Account
from anvil.descriptors import TargetDescriptor
from anvil.execution_context import ExecutionContext
from anvil.session import SessionFactory

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
        self.descriptor = descriptor
        self.context = context
        self._session_factory = session_factory or SessionFactory()

    def resolve_accounts(self) -> list[Account]:
        __LOGGER__.info(
            f"Resolving explicit accounts "
            f"(name={self.descriptor.name}, count={len(self.descriptor.include or [])}, "
            f"regions={self.context.regions})"
        )

        base_session = self._session_factory.create_base_session(
            profile_name=self.descriptor.profile, region_name=self.context.regions[0]
        )

        accounts: list[Account] = []

        for account_id in self.descriptor.include or []:
            accounts.append(
                Account(
                    account_id=account_id,
                    account_alias=account_id,
                    is_management=False,
                    assume_role=self.descriptor.role_name is not None,
                    base_session=base_session,
                    context=self.context,
                    regions=list(self.context.regions),
                    session_factory=self._session_factory,
                )
            )

        return accounts
