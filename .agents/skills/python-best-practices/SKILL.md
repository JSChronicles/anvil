---
name: python-best-practices
description: Apply modern Python coding standards when Codex writes, edits, or reviews Python code, especially for project layout, type hints, docstrings, dataclasses, logging, error handling, CLI design, concurrency, caching, and maintainable Python 3.14+ implementations.
---

# Python Best Practices

Write all Python code for Python 3.14 or newer. Follow modern Python best
practices for project layout, code organization, docstrings, type hints,
logging, and structured error handling.

Prefer simple, maintainable solutions first. Use concurrency only when it
provides clear value. Consider caching when it is easy to implement and
meaningfully beneficial. When structured objects are needed, prefer
`@dataclass`, and use `frozen=True` when immutability is appropriate.

Follow these guidelines strictly when writing, editing, or reviewing Python
code.

## Project Layout

- Keep modules focused and cohesive.
- Put reusable logic in functions or classes instead of top-level scripts.
- Keep import-time behavior safe. Do not perform network calls, filesystem
  mutations, or expensive work during import.
- Prefer explicit dependencies passed into functions over hidden global state.
- Keep command-line entry points thin; parse arguments, call a typed function,
  and return or exit cleanly.

## Error Handling

Use structured error handling with appropriate exceptions.

Avoid:

```python
try:
    load_config(path)
except Exception:
    pass
```

Preferred:

```python
try:
    load_config(path)
except FileNotFoundError as error:
    logger.error(f"Configuration file not found: {path}")
    raise

try:
    run_task()
except Exception as error:
    logger.exception(f"Unhandled error during task execution: {error}")
    raise
```

Rationale: Structured exception handling ensures errors are visible, logged with
context, and propagated correctly. This preserves debugging information,
prevents hidden failures, and maintains predictable program behavior.

Rules:

- Catch specific exceptions whenever possible.
- Do not silently swallow exceptions.
- Only use `try`/`except` when adding value such as logging, context, cleanup,
  or controlled recovery.
- If catching a generic `Exception`, log the error and re-raise it.
- Preserve the original traceback by using `raise` without arguments.

## Google-Style Docstrings

Use Google-style docstrings to document all functions and classes.

Avoid:

```python
def load_config(path: str) -> dict:
    """Load config."""
```

Preferred:

```python
def load_config(path: str) -> dict[str, object]:
    """Load configuration from a JSON file.

    Args:
        path: Path to the configuration file.

    Returns:
        The parsed configuration data.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the file content is invalid.
    """
```

Rationale: Google-style docstrings improve readability, maintainability, and
consistency across the codebase.

## Logging

Incorporate basic logging for debugging and informational output.

Logging statements must use f-strings for message formatting.

Avoid:

```python
logger.error("Project failed: %s", project_name)
logger.info("Account %s processed", account_id)
```

Prefer:

```python
logger.error(f"Project failed: {project_name}")
logger.info(f"Account {account_id} processed")
```

Rationale: F-strings are more readable, consistent with this code style, and
make log messages easier to understand and maintain.

Rules:

- Use module-level loggers: `logger = logging.getLogger(__name__)`.
- Use f-strings for logging messages.
- Include useful context in log messages.
- Do not log and suppress errors unless controlled recovery is intentional.

## Type Hinting

Use native type hinting without importing types from `typing` unless absolutely
necessary.

Avoid:

```python
from typing import Dict, List


def build_index(items: List[str]) -> Dict[str, int]:
    return {item: index for index, item in enumerate(items)}
```

Prefer:

```python
def build_index(items: list[str]) -> dict[str, int]:
    return {item: index for index, item in enumerate(items)}
```

Rationale: Modern Python supports native generic type syntax, which is clearer
and avoids unnecessary imports.

Rules:

- Use `list[str]`, `dict[str, int]`, `tuple[str, ...]`, and `str | None`.
- Type public functions, methods, dataclass fields, and class attributes.
- Let obvious local variables infer naturally unless an explicit type improves
  clarity.

## No External Imports

Avoid external libraries unless explicitly allowed.

```python
# Avoid:
import requests

# Prefer:
import urllib.request
```

Rationale: Prefer the standard library unless an external dependency is
explicitly justified and approved.

## argparse for CLI

Use `argparse` for handling command-line arguments where flexibility is useful.

Avoid:

```python
import sys

config_path = sys.argv[1]
```

Prefer:

```python
import argparse

parser = argparse.ArgumentParser(description="Run the project task.")
parser.add_argument("--config", required=True, help="Path to the config file.")
arguments = parser.parse_args()
```

Rationale: `argparse` provides clearer interfaces, built-in help text, and more
maintainable CLI behavior.

## Code Quality

Ensure the code is clean, maintainable, PEP 8 compliant, and high quality.

Avoid:

```python
def run(x,y): return x+y
```

Prefer:

```python
def add_values(left_value: int, right_value: int) -> int:
    """Return the sum of two integer values."""
    return left_value + right_value
```

Rationale: Readable, consistently formatted code is easier to maintain, review,
and extend.

## Avoid Dictionary Aliasing and Pass-Through Variables

Do not introduce variables that simply mirror a dictionary access when the
original structure is already short and readable.

Avoid:

```python
project = proj_data["project"]
name = project["name"]
```

Prefer:

```python
project_name = proj_data["project"]["name"]
state = proj_data["project"]["person"]["state"]
```

Aliases are only acceptable when:

- The access path is deeply nested or complex.
- The value represents an expensive computation.
- The value must capture moment-in-time state.
- The variable introduces meaningful semantic meaning not already present.

Rationale: Repeated access to the original structure is preferred over creating
aliases for simple lookups.

## Enforce Descriptive Variable Names

Variable names must clearly describe the value they contain. Single-character
names are not allowed except for mathematical expressions.

Avoid:

```python
x = proj_data["project"]["name"]
d = data["items"]
i = 0
```

Prefer:

```python
project_name = proj_data["project"]["name"]
items = data["items"]
index = 0
```

Rationale: Single-character variable names reduce readability and obscure
intent. Descriptive names make the code self-documenting and easier to
maintain.

## Prefer @dataclass for Structured Objects

When structured objects are needed, prefer `@dataclass` over manual boilerplate
classes. Use `frozen=True` when immutability is appropriate.

Avoid:

```python
class AccountResult:
    def __init__(self, account_id: str, region: str, success: bool) -> None:
        self.account_id = account_id
        self.region = region
        self.success = success
```

Prefer:

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class AccountResult:
    """Result of processing an account in a region."""

    account_id: str
    region: str
    success: bool
```

Rationale: `@dataclass` reduces boilerplate, improves readability, and makes
structured data containers easier to maintain. Use `frozen=True` when mutation
is not required.

Rules:

- Prefer dataclasses for stable data containers.
- Use dictionaries for dynamic external data, JSON-like payloads, and
  schema-less mappings.
- Use `frozen=True` when the value should not change after construction.
- Consider `slots=True` for large numbers of instances when it improves memory
  behavior without hurting clarity.

## Use Concurrency Only When It Clearly Helps

Use threading, async execution, or concurrency only when it provides meaningful
benefit. Prefer simple, predictable implementations over premature
optimization.

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

Rationale: Concurrency adds complexity and should only be used when tasks are
independent and the benefit is clear.

Rules:

- Do not add concurrency by default.
- Use concurrency only for independent work that benefits from parallel
  execution.
- Keep concurrency bounded and easy to reason about.
- Preserve ordering, error behavior, and cancellation behavior intentionally.
- Prefer the simplest correct implementation.

## Use Caching When It Is Simple and Beneficial

Consider caching when it is easy to implement, improves performance, and does
not introduce unnecessary complexity.

Avoid:

```python
def get_account_name(account_id: str) -> str:
    return load_account_details(account_id)["name"]
```

Prefer simple standard-library caching when the function is pure enough, inputs
are hashable, and cached results can safely live for the needed scope.

Use `functools.cache` for small, unbounded, process-local caches where the key
space is naturally limited:

```python
from functools import cache


@cache
def get_account_name(account_id: str) -> str:
    """Return the account name for the given account ID."""
    return load_account_details(account_id)["name"]
```

Use `functools.lru_cache(maxsize=...)` when the key space can grow and cached
values should be bounded:

```python
from functools import lru_cache


@lru_cache(maxsize=256)
def get_account_name(account_id: str) -> str:
    """Return the account name for the given account ID."""
    return load_account_details(account_id)["name"]
```

Use manual caches when the cache must be scoped, cleared explicitly, populated
from non-hashable runtime state, or keyed differently than the function
arguments:

```python
class AccountNameCache:
    """Scoped cache for account names discovered during one run."""

    def __init__(self) -> None:
        self._account_names: dict[str, str] = {}

    def get_account_name(self, account_id: str) -> str:
        """Return the account name for the given account ID."""
        if account_id not in self._account_names:
            self._account_names[account_id] = load_account_details(account_id)["name"]
        return self._account_names[account_id]
```

Use a custom cache for complex runtime coordination, such as organization
preflight discovery. Prefer this when the cache must be scoped to one run,
avoid duplicate concurrent work, expose hit/wait information, or cache a result
under a runtime-derived key.

```python
from dataclasses import dataclass, field
import threading
from collections.abc import Callable


@dataclass(slots=True)
class _Flight:
    event: threading.Event = field(default_factory=threading.Event)
    value: object | None = None
    error: BaseException | None = None


class SingleFlightCache:
    """Run one creator per key and let concurrent callers share the result."""

    def __init__(self) -> None:
        self._values: dict[object, object] = {}
        self._flights: dict[object, _Flight] = {}
        self._lock = threading.Lock()

    def get_or_create(
        self, *, key: object, create: Callable[[], object]
    ) -> tuple[object, bool, bool]:
        """Return a cached value, creating it once when missing.

        Returns:
            A tuple of the value, whether the cache hit, and whether this caller
            waited for another creator.
        """
        with self._lock:
            if key in self._values:
                return self._values[key], True, False

            flight = self._flights.get(key)
            if flight is None:
                flight = _Flight()
                self._flights[key] = flight
                owns_create = True
            else:
                owns_create = False

        if owns_create:
            try:
                value = create()
            except BaseException as error:
                with self._lock:
                    flight.error = error
                    self._flights.pop(key, None)
                    flight.event.set()
                raise

            with self._lock:
                self._values[key] = value
                flight.value = value
                self._flights.pop(key, None)
                flight.event.set()
            return value, False, False

        flight.event.wait()
        if flight.error is not None:
            raise flight.error
        return flight.value, True, True
```

Rationale: Caching can improve performance, but it should only be used when the
benefit is clear and the implementation remains simple and safe.

Rules:

- Do not add caching without a clear reason.
- Prefer standard-library caching for pure, hashable, process-local results.
- Use `lru_cache(maxsize=...)` instead of `cache` when the key space may grow.
- Use scoped manual caches when data should not live for the full process.
- Use custom coordination caches when concurrent callers must share in-flight
  work instead of duplicating expensive operations.
- Use caching only when it improves performance or avoids repeated expensive
  work.
- Avoid caching when it makes behavior harder to understand.
- Consider cache invalidation, memory growth, and thread-safety before adding a
  module-level cache.

## Review Checklist

Before finishing Python changes, check:

- Code targets Python 3.14 or newer.
- Functions and classes have Google-style docstrings.
- Public APIs and structured objects have native type hints.
- Exceptions are specific, visible, logged with context when useful, and
  re-raised when appropriate.
- Logs include actionable context and use f-strings.
- External dependencies were not introduced without explicit approval.
- CLIs use `argparse` when argument flexibility is useful.
- Dataclasses are used for stable structured objects where they reduce
  boilerplate.
- Concurrency and caching are justified by clear benefit.
- Variable names explain intent and avoid single-character names outside true
  mathematical contexts.
- Short dictionary accesses were not replaced by meaningless aliases or
  pass-through variables.
