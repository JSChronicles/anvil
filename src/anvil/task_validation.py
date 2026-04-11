"""
Task validation for Anvil.

This module performs *structural* validation of task definitions.
It does not execute tasks or perform any AWS interactions.
"""

from __future__ import annotations

from inspect import Parameter, signature

# Required keyword arguments for all task run() functions
REQUIRED_RUN_KWARGS: set[str] = {
    "account_id",
    "account_alias",
    "session",
    "dry_run",
    "metadata",
    "actions",
}


class TaskValidationError(ValueError):
    """Raised when a task fails structural validation."""


def validate_tasks(tasks: list) -> None:
    errors: list[str] = []
    seen_names: set[str] = set()

    for task in tasks:
        try:
            if not isinstance(task.name, str) or not task.name:
                raise TaskValidationError("task name must be a non-empty string")

            if task.name in seen_names:
                raise TaskValidationError(f"duplicate task name: {task.name}")

            seen_names.add(task.name)

            if not hasattr(task, "run"):
                raise TaskValidationError(f"task '{task.name}' is missing run()")

            if not callable(task.run):
                raise TaskValidationError(f"task '{task.name}'.run is not callable")

            _validate_task_run_signature(task)

        except TaskValidationError as exc:
            errors.append(str(exc))

    if errors:
        raise TaskValidationError("\n  - " + "\n  - ".join(errors))


def _validate_task_run_signature(task) -> None:
    try:
        sig = signature(task.run)
    except (TypeError, ValueError) as exc:
        raise TaskValidationError(
            f"unable to inspect run() signature for task '{task.name}'"
        ) from exc

    parameters = sig.parameters

    accepts_extra_kwargs = any(
        param.kind is Parameter.VAR_KEYWORD for param in parameters.values()
    )
    missing = REQUIRED_RUN_KWARGS - set(parameters)
    if missing:
        if not accepts_extra_kwargs:
            raise TaskValidationError(
                f"task '{task.name}' is missing required run() parameters: "
                f"{sorted(missing)}"
            )

    for param in parameters.values():
        if param.kind is Parameter.POSITIONAL_ONLY:
            raise TaskValidationError(
                f"task '{task.name}' uses positional-only parameter "
                f"'{param.name}', which is not supported"
            )
