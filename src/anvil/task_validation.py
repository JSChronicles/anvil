"""
Task validation for Anvil.

This module performs *structural* validation of task definitions.
It imports task callables for inspection but does not execute them.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from inspect import getdoc, getmodule

from anvil._components import (
    ComponentCatalog,
    ComponentDescriptor,
    validate_keyword_only_invocation,
)
from anvil.task_context import TaskCallContext


class TaskValidationError(ValueError):
    """Raised when a task fails structural validation."""


TaskDescriptor = ComponentDescriptor[Callable]


def validate_tasks(tasks: Sequence[TaskDescriptor]) -> None:
    """Validate discovered task descriptors without executing tasks."""

    errors = task_validation_errors(tasks)
    if errors:
        raise TaskValidationError("\n  - " + "\n  - ".join(errors))


def task_validation_errors(tasks: Sequence[TaskDescriptor]) -> list[str]:
    """Return structural validation errors for task descriptors."""

    errors: list[str] = []

    for task in tasks:
        try:
            if not isinstance(task.name, str) or not task.name:
                raise TaskValidationError("task name must be a non-empty string")

            if not callable(task.load):
                raise TaskValidationError(f"task '{task.name}'.load is not callable")

            run = task.load()
            if not callable(run):
                raise TaskValidationError(
                    f"task '{task.name}' is missing required run() function"
                )

            _validate_task_run_signature(name=task.name, run=run)
            _validate_task_detail_docstring(name=task.name, run=run)

        except Exception as exc:
            errors.append(f"{task.name} ({task.source}): {exc}")

    return errors


def task_catalog_ambiguity_errors(
    provider_catalogs: Mapping[str, ComponentCatalog[Callable]],
    *,
    task_names: set[str] | None = None,
) -> list[str]:
    """Return provider-scoped task ambiguity errors."""

    errors: list[str] = []
    for provider_name, catalog in sorted(provider_catalogs.items()):
        for name, candidates in catalog.inventory.items():
            if task_names is not None and name not in task_names:
                continue
            if len(candidates) < 2:
                continue

            sources = ", ".join(str(candidate.source) for candidate in candidates)
            errors.append(
                f"task '{name}' is ambiguous for provider '{provider_name}'; "
                f"found in multiple sources: {sources}"
            )
    return errors


def _validate_task_run_signature(*, name: str, run: Callable) -> None:
    try:
        validate_keyword_only_invocation(
            run, keyword_names=TaskCallContext.keyword_names()
        )
    except ValueError as exc:
        raise TaskValidationError(
            f"task '{name}' has incompatible run() signature: {exc}"
        ) from exc


def _validate_task_detail_docstring(*, name: str, run: Callable) -> None:
    doc = getdoc(run)
    if doc is None:
        module = getmodule(run)
        if module is not None:
            doc = getdoc(module)

    if doc is None:
        raise TaskValidationError(
            f"task '{name}' is missing detail documentation; add a "
            "Google-style run() docstring for 'anvil list --tasks --detail'"
        )
