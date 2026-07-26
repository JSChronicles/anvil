from __future__ import annotations

from anvil.providers.aws.account import AccountAccessStrategy
from anvil.providers.aws.account_resolver import AccountResolver
from anvil.descriptors import ConfigBranch, TargetDescriptor
from anvil.execution_context import ExecutionContext


class FakeSessionFactory:
    def create_base_session(self, **kwargs):
        return type("_BaseSession", (), {"profile_name": kwargs["profile_name"]})()


def _context() -> ExecutionContext:
    return ExecutionContext(regions=["us-east-1"], dry_run=True, tasks=[], metadata={})


def test_resolve_accounts_uses_assume_role_strategy_when_role_name_is_configured():
    descriptor = TargetDescriptor(
        config_branch=ConfigBranch.TARGETS,
        name="selected",
        provider="aws",
        mode="accounts",
        provider_options={"profile": "tooling", "role_name": "SecurityAccessRole"},
        include=["111111111111", "222222222222"],
    )
    context = _context()

    accounts = AccountResolver(
        descriptor=descriptor, context=context, session_factory=FakeSessionFactory()
    ).resolve_accounts()

    assert [account.account_id for account in accounts] == [
        "111111111111",
        "222222222222",
    ]
    assert [account.access_strategy for account in accounts] == [
        AccountAccessStrategy.ASSUME_ROLE,
        AccountAccessStrategy.ASSUME_ROLE,
    ]
    assert [account.role_name for account in accounts] == [
        "SecurityAccessRole",
        "SecurityAccessRole",
    ]


def test_resolve_accounts_uses_direct_profile_strategy_without_role_name():
    descriptor = TargetDescriptor(
        config_branch=ConfigBranch.TARGETS,
        name="current",
        provider="aws",
        mode="accounts",
        provider_options={"profile": "dev-admin"},
        include=["111111111111"],
    )
    context = _context()

    accounts = AccountResolver(
        descriptor=descriptor, context=context, session_factory=FakeSessionFactory()
    ).resolve_accounts()

    assert [account.account_id for account in accounts] == ["111111111111"]
    assert accounts[0].access_strategy is AccountAccessStrategy.DIRECT_PROFILE
    assert accounts[0].role_name is None
