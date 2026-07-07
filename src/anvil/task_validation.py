"""
Task validation for Anvil.

This module performs *structural* validation of task definitions.
It does not execute tasks or perform any AWS interactions.
"""

from __future__ import annotations

from inspect import Parameter, getdoc, getmodule, signature

# Required keyword arguments for all task run() functions
REQUIRED_RUN_KWARGS: set[str] = {
    "account_id",
    "account_alias",
    "session",
    "dry_run",
    "metadata",
    "actions",
}
PROVIDER_NEUTRAL_RUN_KWARGS: set[str] = {
    "provider",
    "execution_target_id",
    "execution_target_name",
    "execution_target_type",
    "region",
    "session",
    "dry_run",
    "metadata",
    "actions",
}


class TaskValidationError(ValueError):
    """Raised when a task fails structural validation."""


def validate_tasks(tasks: list) -> None:
    errors = task_validation_errors(tasks)
    if errors:
        raise TaskValidationError("\n  - " + "\n  - ".join(errors))


def task_validation_errors(tasks: list) -> list[str]:
    """Return structural validation errors for task definitions."""
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
            _validate_task_detail_docstring(task)

        except TaskValidationError as exc:
            errors.append(str(exc))

    return errors


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
    parameter_names = set(parameters)
    missing_legacy = REQUIRED_RUN_KWARGS - parameter_names
    missing_provider_neutral = PROVIDER_NEUTRAL_RUN_KWARGS - parameter_names
    if missing_legacy and missing_provider_neutral:
        if not accepts_extra_kwargs:
            raise TaskValidationError(
                f"task '{task.name}' is missing required run() parameters: "
                f"{sorted(missing_legacy)} or provider-neutral parameters: "
                f"{sorted(missing_provider_neutral)}"
            )

    for param in parameters.values():
        if param.kind is Parameter.POSITIONAL_ONLY:
            raise TaskValidationError(
                f"task '{task.name}' uses positional-only parameter "
                f"'{param.name}', which is not supported"
            )


def _validate_task_detail_docstring(task) -> None:
    doc = getdoc(task.run)
    if doc is None:
        module = getmodule(task.run)
        if module is not None:
            doc = getdoc(module)

    if doc is None:
        raise TaskValidationError(
            f"task '{task.name}' is missing detail documentation; add a "
            "Google-style run() docstring for 'anvil list --tasks --detail'"
        )
