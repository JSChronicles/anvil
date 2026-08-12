from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, fields
from typing import cast
from anvil.actions import ActionRecorder
from anvil.results import TaskResult


class TaskInputResolutionError(ValueError):
    """Raised when configured dependency data cannot be resolved."""


def merge_task_metadata(
    *, target_metadata: Mapping[str, object], task_metadata: Mapping[str, object]
) -> dict[str, object]:
    """Recursively merge task metadata over target metadata.

    Args:
        target_metadata: Metadata inherited from the configured target.
        task_metadata: Static metadata declared for one task invocation.

    Returns:
        A recursively merged metadata mapping. ``TaskCallContext.to_kwargs()``
        performs the invocation's required deep copy.
    """

    merged = dict(target_metadata)
    for key, task_value in task_metadata.items():
        target_value = merged.get(key)
        if isinstance(target_value, Mapping) and isinstance(task_value, Mapping):
            merged[key] = merge_task_metadata(
                target_metadata=cast(Mapping[str, object], target_value),
                task_metadata=cast(Mapping[str, object], task_value),
            )
        else:
            merged[key] = task_value
    return merged


def _select_dependency_path(
    *, local_name: str, path: str, task_result: TaskResult
) -> object:
    """Select one configured dotted path from a task result."""

    root, *nested_keys = path.split(".")
    selected: object = getattr(task_result, root)
    for key in nested_keys:
        if not isinstance(selected, Mapping) or key not in selected:
            raise TaskInputResolutionError(
                f"dependency_data.{local_name} path '{path}' does not exist"
            )
        selected = cast(Mapping[str, object], selected)[key]
    return selected


def resolve_dependency_data(
    *,
    references: Mapping[str, Mapping[str, str]],
    dependency_results: Mapping[str, TaskResult | Sequence[TaskResult]],
) -> dict[str, object]:
    """Resolve configured dependency-data references.

    Args:
        references: Local input names mapped to producer IDs and optional paths.
        dependency_results: Available results keyed by effective producer ID.

    Returns:
        Dependency values keyed by the configured local input names.

    Raises:
        TaskInputResolutionError: If a producer result or selected path is missing.
    """

    resolved: dict[str, object] = {}
    for local_name, reference in references.items():
        task_id = reference["task_id"]
        if task_id not in dependency_results:
            raise TaskInputResolutionError(
                f"dependency_data.{local_name} has no result for task '{task_id}'"
            )

        available = dependency_results[task_id]
        is_multiple = not isinstance(available, TaskResult)
        results = list(available) if is_multiple else [available]
        path = reference.get("path")
        values: list[object] = [
            (
                result
                if path is None
                else _select_dependency_path(
                    local_name=local_name, path=path, task_result=result
                )
            )
            for result in results
        ]
        resolved[local_name] = values if is_multiple else values[0]
    return resolved


@dataclass(frozen=True, slots=True)
class TaskCallContext:
    """Provider-neutral task invocation context."""

    provider: str
    execution_target_id: str
    execution_target_name: str
    execution_target_type: str
    region: str
    session: object
    dry_run: bool
    metadata: dict[str, object]
    dependency_data: dict[str, object]
    actions: ActionRecorder

    @classmethod
    def keyword_names(cls) -> frozenset[str]:
        """Return the canonical task invocation keyword names."""

        return frozenset(field.name for field in fields(cls))

    def to_kwargs(self) -> dict[str, object]:
        """Return provider-neutral task keyword arguments."""

        invocation_kwargs = {
            field.name: getattr(self, field.name) for field in fields(self)
        }
        invocation_kwargs["metadata"] = deepcopy(self.metadata)
        invocation_kwargs["dependency_data"] = deepcopy(self.dependency_data)
        return invocation_kwargs
