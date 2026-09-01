# Payer / Management Account-Only Tasks

Use this reference when a workflow's target selection resolves to only the
payer (management) account — not when the payer/management account is one of
several targets in a broader multi-account run. If the same workflow also
targets other accounts, treat the payer account like any other target and
follow the general guidance in `task-granularity.md` instead.

Choose task scope before applying this concurrency guidance. Payer-only target
selection does not itself imply `configured_target`. Use that scope only when
the operation belongs to the original configured target or its shared control
plane, following `task-scopes.md`.

## Why This Case Is Different

`task-granularity.md` frames concurrency pressure as:

```text
max_parallel_targets * max_workers * max_parallel_regions
```

That formula assumes multiple accounts are being worked concurrently, which
is why the general advice is to raise `max_parallel_regions` cautiously and
benchmark first. When a run's target set is only the payer/management
account, `max_parallel_targets` is fixed at 1 for that run. There is no other
account's work competing for the same connection pool, worker threads, or API
quota, so a task (or the YAML orchestration around it) has meaningfully more
headroom to use threading, async, or higher region/worker concurrency than a
typical multi-account task would.

This headroom is about contention with *other targets*, not about the target
service's own limits. AWS service throttling still applies per account and
region regardless of how many other accounts Anvil happens to be running
against in the same invocation.

## Where This Actually Helps

The benefit is largest for services that live only in the payer/management
account and require one API call per item with no batch equivalent:

- IAM Identity Center (`sso-admin`, `identitystore`) — enumerating or acting
  on permission sets, account assignments, or identity store users/groups.
- AWS Organizations — enumerating accounts, OUs, or policies attached across
  many targets from the single management account.
- Control Tower or an Organizations-wide Config aggregator, when querying
  many regions from the single aggregator account.

It does not help generic single-account inventory tasks that already run
quickly, and it does not justify adding concurrency to a task that doesn't
need it. See `python-best-practices/concurrency-and-caching.md` for the
baseline rule that concurrency should only be added when it provides a
meaningful, measurable benefit.

## Pattern: Bounded Fan-Out For No-Batch-API Calls

Use this shape whenever a payer-only task needs to make one API call per item
and the service has no batch or single-paginated-call equivalent. It works
for both mutating fan-out (deleting or updating N items) and read-heavy
fan-out (describing N items after a list call):

```python
from concurrent.futures import ThreadPoolExecutor, as_completed

from anvil.task_errors import TaskExecutionError

# No batch API for this call, so document why this number was chosen,
# e.g. an observed throttling point, or a conservative starting value.
MAX_WORKERS = 5


def _process_item(client, item: dict[str, object]) -> dict[str, object]:
    # One provider API call per item goes here.
    ...
    return item


def fan_out(
    client, items: list[dict[str, object]]
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    succeeded: list[dict[str, object]] = []
    failed: list[dict[str, object]] = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_item = {
            executor.submit(_process_item, client, item): item for item in items
        }

        for future in as_completed(future_to_item):
            item = future_to_item[future]
            try:
                succeeded.append(future.result())
            except Exception as error:  # narrow to the real provider exceptions
                failed.append({**item, "error": str(error)})

    return succeeded, failed
```

Raise `TaskExecutionError` with a `partial_result` carrying both `succeeded`
and `failed` when `failed` is non-empty, following the standard
partial-failure pattern instead of aborting the whole batch on the first
error.

## Guardrails

- Keep the worker count a named, documented module constant, never an
  unbounded pool.
- Still respect the target service's own rate limits. "No other account is
  competing" does not mean "no limits apply."
- Do not reach for this pattern just because a task is payer-only. Only use
  it when the call shape (one call per item, no batch API, a meaningful item
  count) actually benefits from concurrency.
- Preserve per-item error isolation: one failure should not abort the whole
  batch unless the task's contract requires all-or-nothing behavior.
- If the workflow's target set later expands to include other accounts
  alongside the payer account, re-evaluate. The headroom this doc describes
  no longer applies once `max_parallel_targets` is greater than 1.
