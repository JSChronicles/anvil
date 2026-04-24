from __future__ import annotations

import datetime
import logging
from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed
from concurrent.futures._base import Future

from anvil.benchmark import BenchmarkRecorder
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
    benchmark_enabled: bool = False,
    benchmark: dict[str, object] | None = None,
) -> TargetResult:
    """
    Execute a resolved set of accounts using the shared concurrent account path.
    """
    account_results: list[AccountResult] = []
    recorder = BenchmarkRecorder(enabled=benchmark_enabled)

    with recorder.phase("account_execution_seconds"):
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

                        # Fail-fast is cooperative: pending futures are cancelled
                        # here, while already-running account work observes
                        # context.cancel_event and stops at its next cancellation
                        # check.
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

    if benchmark_enabled:
        account_window_seconds = _account_window_seconds(account_results)
        sum_account_duration_seconds = sum(
            result.duration_seconds for result in account_results
        )
        recorder.update(
            {
                "submitted_account_count": len(accounts),
                "completed_account_count": len(account_results),
                "max_workers": max_workers,
                "account_execution_window_seconds": account_window_seconds,
                "sum_account_duration_seconds": sum_account_duration_seconds,
                "max_account_duration_seconds": max(
                    (result.duration_seconds for result in account_results), default=0.0
                ),
                "worker_utilization": _worker_utilization(
                    sum_account_duration_seconds=sum_account_duration_seconds,
                    max_workers=max_workers,
                    account_window_seconds=account_window_seconds,
                ),
            }
        )

    target_benchmark: dict[str, object] | None = None
    recorder_data = recorder.data
    if recorder_data is not None:
        target_benchmark = {**(benchmark or {}), **recorder_data}

    return TargetResult.create(
        config_branch=config_branch,
        target_name=name,
        dry_run=context.dry_run,
        account_results=account_results,
        benchmark=target_benchmark,
    )


def _account_window_seconds(account_results: list[AccountResult]) -> float:
    if not account_results:
        return 0.0

    starts = [
        datetime.datetime.fromisoformat(result.started_at) for result in account_results
    ]
    ends = [
        datetime.datetime.fromisoformat(result.ended_at) for result in account_results
    ]
    return (max(ends) - min(starts)).total_seconds()


def _worker_utilization(
    *,
    sum_account_duration_seconds: float,
    max_workers: int,
    account_window_seconds: float,
) -> float:
    if max_workers <= 0 or account_window_seconds <= 0:
        return 0.0

    return sum_account_duration_seconds / (max_workers * account_window_seconds)
