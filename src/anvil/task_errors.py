from __future__ import annotations

import json


class TaskExecutionError(RuntimeError):
    """Report task failure while retaining JSON-serializable partial output."""

    def __init__(self, message: str, *, partial_result: object) -> None:
        """Initialize a task execution failure.

        Args:
            message: Actionable failure detail recorded on the task result.
            partial_result: JSON-serializable output produced before failure.

        Raises:
            TypeError: If the partial result is not JSON-serializable.
        """

        try:
            json.dumps(partial_result)
        except (TypeError, ValueError) as error:
            raise TypeError(
                "TaskExecutionError partial_result must be JSON-serializable"
            ) from error

        super().__init__(message)
        self.partial_result = partial_result
