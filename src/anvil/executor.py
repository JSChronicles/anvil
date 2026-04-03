from __future__ import annotations

import logging
from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed
from concurrent.futures._base import Future

from anvil.account import Account
from anvil.descriptors import ConfigBranch
from anvil.execution_context import ExecutionContext
from anvil.results import AccountResult, TargetResult

__LOGGER__ = logging.getLogger(__name__)


def execute_accounts(
    *,
    name: str,
    config_branch: ConfigBranch,
    max_workers: int,
    context: ExecutionContext,
    accounts: list[Account],
) -> TargetResult:
    """
    Execute a resolved set of accounts using the shared concurrent account path.
    """
    account_results: list[AccountResult] = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures: dict[Future[AccountResult], Account] = {
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
                    context.fail_fast
                    and account_result.status.is_unsuccessful
                    and not fail_fast_triggered
                ):
                    __LOGGER__.critical(f"Fail-fast triggered in '{name}'")

                    context.cancel_event.set()
                    fail_fast_triggered = True

                    for pending in futures:
                        if not pending.done():
                            pending.cancel()

        except Exception:
            executor.shutdown(cancel_futures=True)
            raise

    account_results.sort(
        key=lambda result: (result.account_alias.lower(), result.account_id)
    )

    return TargetResult.create(
        config_branch=config_branch,
        target_name=name,
        dry_run=context.dry_run,
        account_results=account_results,
    )
