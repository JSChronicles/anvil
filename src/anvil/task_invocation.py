from __future__ import annotations

from collections.abc import Callable
from anvil.task_context import TaskCallContext


def invoke_task(task_run: Callable, *, context: TaskCallContext) -> object:
    """Invoke a task with provider-neutral kwargs."""

    return task_run(**context.to_kwargs())
