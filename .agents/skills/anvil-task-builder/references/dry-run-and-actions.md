# Dry-Run, Actions, Logging, And Results

## Task Conventions

- Check `dry_run` before every mutating API call.
- Prefix dry-run log and action messages with `(dry-run)` when describing work that would happen but is intentionally not executed.
- Use logger calls for detailed task progress and per-resource cleanup details.
- Use `actions.record(...)` for concise audit-level planned or completed actions, usually at the end of `run()` or after a meaningful operation group completes.
- Do not log generic account-processing start messages; the engine already logs which account is being processed.
- Task logs should describe task-specific work or task-specific outcomes.
- Return task-specific data only.
- Do not duplicate execution context already included by the engine, such as target identity, `region`, or `dry_run`, unless the task needs a renamed or transformed value for its own result schema.

## Python And Provider Hygiene

- Keep task modules import-safe. Do not make provider SDK calls at import time.
- Use f-strings for task log messages to match the existing task style.
- Return dictionaries, lists, strings, numbers, booleans, or `None`; results should be JSON-serializable.
- Validate required `metadata` values before using them, including type checks.
- Raise a clear `RuntimeError` when required metadata is missing or invalid.
- Prefer provider SDK pagination helpers for list and describe APIs that can paginate.
- Let unexpected provider SDK errors surface unless the task can add useful context.

When a task requires metadata, validate it before using it. Fail early with a clear `RuntimeError` that names the task and required metadata key:

```python
user_name = metadata.get("user_name")
if not isinstance(user_name, str):
    raise RuntimeError("remove_iam_user requires metadata.user_name to be a string")
```

Prefer explicit type checks over assuming YAML input shape. Validate lists, booleans, and nested objects before passing values into provider SDK APIs.

## Dry-Run Shape

```python
if dry_run:
    __LOGGER__.info(
        f"(dry-run) Would delete IAM user {user_name} in target {execution_target_id}"
    )
    actions.record(
        f"(dry-run) Would delete IAM user {user_name} in target {execution_target_id}"
    )
    return {"planned": True, "deleted": False, "user_name": user_name}

iam.delete_user(UserName=user_name)
__LOGGER__.info(f"Deleted IAM user {user_name} in target {execution_target_id}")
actions.record(f"Deleted IAM user {user_name} in target {execution_target_id}")
return {"planned": False, "deleted": True, "user_name": user_name}
```
