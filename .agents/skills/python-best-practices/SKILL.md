---
name: python-best-practices
description: Applies modern Python coding standards for maintainable .py files, CLIs, tests, and package structure. Use when user asks to "write Python", "review Python", "refactor this Python", "add type hints", "improve error handling", "add logging", "write tests", "build an argparse CLI", or improve concurrency, caching, dataclasses, or project layout.
---


# Python Best Practices

Write all Python code for Python 3.14 or newer. Prefer simple, maintainable solutions first. Use concurrency only when it provides clear value. Use caching only when it is easy to implement and meaningfully beneficial.

## Core Rules

- Keep modules focused and cohesive.
- Keep import-time behavior safe. Do not perform network calls, filesystem mutations, or expensive work during import.
- Prefer explicit dependencies passed into functions over hidden global state.
- Use native type hints such as `list[str]`, `dict[str, object]`, and `str | None`.
- Type public functions, methods, dataclass fields, and class attributes.
- Use Google-style docstrings for functions and classes.
- Use module-level loggers: `__LOGGER__ = logging.getLogger(__name__)`.
- Use f-strings for logging messages.
- Catch specific exceptions whenever possible.
- Do not silently swallow exceptions.
- Avoid external libraries unless explicitly allowed.
- Prefer `@dataclass` for stable structured objects, and use `frozen=True` when immutability is appropriate.
- Use descriptive variable names. Avoid single-character names except for true mathematical expressions.
- Avoid pass-through aliases for simple dictionary access.
- For CLIs, prefer one real console entrypoint that owns argument parsing, logging setup, and the top-level error boundary.
- Map subcommands to `_cmd_*` handler functions instead of rebuilding argv and calling other module CLIs.
- Keep library modules as typed functions and data objects, not alternate command-line scripts, unless a standalone script is explicitly needed.
- Return clean operational CLI failures by catching expected exceptions at the top-level entrypoint and logging one useful message.

## Workflow

1. Inspect nearby code and follow stronger local conventions when they exist.
2. Keep the implementation simple before adding abstractions, concurrency, or caching.
3. Add or preserve native type hints and Google-style docstrings.
4. Use structured exceptions and logging with useful context.
5. Avoid new external dependencies unless the user or project already permits them.
6. Run the relevant formatter, linter, type checker, and tests when available.
7. State any validation that could not be run.

## Reference Loading

Load only the reference files needed for the current task:

- For project structure, import-time safety, CLIs, dependencies, and code quality, read `references/project-layout-and-cli.md`.
- For type hints and Google-style docstrings, read `references/typing-and-docstrings.md`.
- For exceptions and logging, read `references/errors-and-logging.md`.
- For dataclasses, variable naming, and dictionary aliasing, read `references/data-modeling-and-naming.md`.
- For concurrency and caching, read `references/concurrency-and-caching.md`.
- For Python code reviews, read `references/review-checklist.md`.

## Review Behavior

When reviewing Python code, lead with concrete bugs, maintainability risks, missing validation, hidden errors, unsafe import-time behavior, unjustified dependencies, unjustified concurrency, and missing tests.

If no issues are found, say so directly and mention any validation gaps.
