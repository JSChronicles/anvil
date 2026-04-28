# Python Review Checklist

Before finishing Python changes, check:

- Code targets Python 3.14 or newer.
- Functions and classes have Google-style docstrings.
- Public APIs and structured objects have native type hints.
- Exceptions are specific, visible, logged with context when useful, and re-raised when appropriate.
- Logs include actionable context and use f-strings.
- External dependencies were not introduced without explicit approval.
- CLIs use `argparse` when argument flexibility is useful.
- Dataclasses are used for stable structured objects where they reduce boilerplate.
- Concurrency and caching are justified by clear benefit.
- Variable names explain intent and avoid single-character names outside true mathematical contexts.
- Short dictionary accesses were not replaced by meaningless aliases or pass-through variables.

## Review Response Shape

Lead with findings ordered by severity. Use exact file and line references when possible.

Prioritize:

- Bugs and behavioral regressions
- Hidden exceptions or swallowed failures
- Unsafe import-time side effects
- Missing validation for external inputs
- Unjustified external dependencies
- Unbounded concurrency or caching
- Missing tests for changed behavior
- Public interfaces missing type hints or clear contracts

If no issues are found, say that clearly and list any commands that were not run.