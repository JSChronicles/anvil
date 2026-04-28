# Concurrency And Caching

## Concurrency

Use threading, async execution, or concurrency only when it provides meaningful benefit. Prefer simple, predictable implementations over premature optimization.

Avoid:

```python
with ThreadPoolExecutor(max_workers=10) as executor:
    results = list(executor.map(process_account, account_ids[:2]))
```

Prefer:

```python
for account_id in account_ids:
    process_account(account_id)
```

When concurrency is actually beneficial:

```python
from concurrent.futures import ThreadPoolExecutor


with ThreadPoolExecutor(max_workers=max_workers) as executor:
    results = list(executor.map(process_account, account_ids))
```

Rules:

- Do not add concurrency by default.
- Use concurrency only for independent work that benefits from parallel execution.
- Keep concurrency bounded and easy to reason about.
- Preserve ordering, error behavior, and cancellation behavior intentionally.
- Prefer the simplest correct implementation.

## Caching

Consider caching when it is easy to implement, improves performance, and does not introduce unnecessary complexity.

Avoid:

```python
def get_account_name(account_id: str) -> str:
    return load_account_details(account_id)["name"]
```

Use `functools.cache` for small, unbounded, process-local caches where the key space is naturally limited:

```python
from functools import cache


@cache
def get_account_name(account_id: str) -> str:
    """Return the account name for the given account ID."""
    return load_account_details(account_id)["name"]
```

Use `functools.lru_cache(maxsize=...)` when the key space can grow and cached values should be bounded:

```python
from functools import lru_cache


@lru_cache(maxsize=256)
def get_account_name(account_id: str) -> str:
    """Return the account name for the given account ID."""
    return load_account_details(account_id)["name"]
```

Use manual caches when the cache must be scoped, cleared explicitly, populated from non-hashable runtime state, or keyed differently than function arguments.

Rules:

- Do not add caching without a clear reason.
- Prefer standard-library caching for pure, hashable, process-local results.
- Use `lru_cache(maxsize=...)` instead of `cache` when the key space may grow.
- Use scoped manual caches when data should not live for the full process.
- Use custom coordination caches when concurrent callers must share in-flight work instead of duplicating expensive operations.
- Avoid caching when it makes behavior harder to understand.
- Consider cache invalidation, memory growth, and thread-safety before adding a module-level cache.