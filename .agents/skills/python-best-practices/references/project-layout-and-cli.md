# Project Layout, CLI, Dependencies, And Code Quality

## Project Layout

- Keep modules focused and cohesive.
- Put reusable logic in functions or classes instead of top-level scripts.
- Keep import-time behavior safe. Do not perform network calls, filesystem mutations, or expensive work during import.
- Prefer explicit dependencies passed into functions over hidden global state.
- Keep command-line entry points thin; parse arguments, call a typed function, and return or exit cleanly.

## No External Imports

Avoid external libraries unless explicitly allowed.

```python
# Avoid:
import requests

# Prefer:
import urllib.request
```

Prefer the standard library unless an external dependency is explicitly justified and approved.

## argparse For CLI

Use `argparse` for command-line arguments where flexibility is useful.

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

`argparse` provides clearer interfaces, built-in help text, and more maintainable CLI behavior.

### Required `--version` Flag

Every installable Python application that exposes a console command must provide
a root-level `--version` flag. It must work without a subcommand, configuration,
logging setup, network access, or other application initialization.

Read the version from the installed distribution metadata so release tooling has
one authoritative version source. Do not duplicate the version in CLI code or a
separate `__version__` constant unless the project has an explicit compatibility
requirement for that constant.

```python
import argparse
from importlib import metadata as importlib_metadata


def _package_version(distribution_name: str) -> str:
    """Return the installed distribution version."""
    try:
        return importlib_metadata.version(distribution_name)
    except importlib_metadata.PackageNotFoundError:
        return "unknown"


parser = argparse.ArgumentParser(description="Run the application.")
parser.add_argument(
    "--version",
    action="version",
    version=f"command-name {_package_version('distribution-name')}",
)
```

Use a stable, explicit command name in the output rather than `%(prog)s` when
the command may run through Python launchers or module execution; newer Python
versions can render `%(prog)s` as a launcher-qualified name. Prefer the concise
format `command-name X.Y.Z`.

Add a CLI regression test that supplies `--version` and verifies:

- exit status `0`;
- exact standard output, including the stable command name and version;
- no subcommand or configuration is required.

## Code Quality

Ensure the code is clean, maintainable, PEP 8 compliant, and high quality.

Avoid:

```python
def run(x, y):
    return x + y
```

Prefer:

```python
def add_values(left_value: int, right_value: int) -> int:
    """Return the sum of two integer values."""
    return left_value + right_value
```

Readable, consistently formatted code is easier to maintain, review, and extend.
