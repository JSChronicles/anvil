# Errors And Logging

## Error Handling

Use structured error handling with appropriate exceptions.

Avoid:

```python
try:
    load_config(path)
except Exception:
    pass
```

Prefer:

```python
try:
    load_config(path)
except FileNotFoundError as error:
    __LOGGER__.error(f"Configuration file not found: {path}")
    raise

try:
    run_task()
except Exception as error:
    __LOGGER__.exception(f"Unhandled error during task execution: {error}")
    raise
```

Rules:

- Catch specific exceptions whenever possible.
- Do not silently swallow exceptions.
- Only use `try`/`except` when adding value such as logging, context, cleanup, or controlled recovery.
- If catching a generic `Exception`, log the error and re-raise it.
- Preserve the original traceback by using `raise` without arguments.

Structured exception handling keeps errors visible, logged with context, and propagated correctly.

## Logging

Incorporate basic logging for debugging and informational output.

Logging statements must use f-strings for message formatting.

Avoid:

```python
__LOGGER__.error("Project failed: %s", project_name)
__LOGGER__.info("Account %s processed", account_id)
```

Prefer:

```python
__LOGGER__.error(f"Project failed: {project_name}")
__LOGGER__.info(f"Account {account_id} processed")
```

Rules:

- Use module-level loggers: `__LOGGER__ = logging.getLogger(__name__)`.
- Use f-strings for logging messages.
- Include useful context in log messages.
- Do not log and suppress errors unless controlled recovery is intentional.
