from __future__ import annotations

from collections.abc import Callable
from inspect import Parameter, signature

from anvil.task_context import TaskCallContext


def invoke_task(
    task_run: Callable, *, context: TaskCallContext, legacy_kwargs: dict[str, object]
) -> object:
    """Invoke a task with compatible legacy and provider-neutral kwargs."""

    candidate_kwargs = {**context.to_kwargs(), **legacy_kwargs}
    try:
        run_signature = signature(task_run)
    except TypeError, ValueError:
        return task_run(**candidate_kwargs)

    parameters = run_signature.parameters
    accepts_extra_kwargs = any(
        parameter.kind is Parameter.VAR_KEYWORD for parameter in parameters.values()
    )
    if accepts_extra_kwargs:
        return task_run(**candidate_kwargs)

    accepted_kwargs = {
        name: value for name, value in candidate_kwargs.items() if name in parameters
    }
    return task_run(**accepted_kwargs)
