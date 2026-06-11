"""
Processor validation for Anvil.

This module performs structural validation of post-run processor definitions.
It loads processor modules to inspect run(...) signatures, but does not execute
processors against result data.
"""

from __future__ import annotations

from collections.abc import Callable
from inspect import Parameter, signature

from anvil.processor_loader import ProcessorDescriptor

REQUIRED_RUN_KWARGS: set[str] = {"context", "output", "metadata"}


class ProcessorValidationError(ValueError):
    """Raised when a processor fails structural validation."""


def validate_processors(processors: list[ProcessorDescriptor]) -> None:
    """Validate discovered processors without executing them."""
    errors = processor_validation_errors(processors)
    if errors:
        raise ProcessorValidationError("\n  - " + "\n  - ".join(errors))


def processor_validation_errors(processors: list[ProcessorDescriptor]) -> list[str]:
    """Return structural validation errors for processor definitions."""
    errors: list[str] = []
    seen_names: set[str] = set()

    for processor in processors:
        try:
            if not isinstance(processor.name, str) or not processor.name:
                raise ProcessorValidationError(
                    "processor name must be a non-empty string"
                )

            if processor.name in seen_names:
                raise ProcessorValidationError(
                    f"duplicate processor name detected: {processor.name}"
                )

            seen_names.add(processor.name)

            if not callable(processor.load):
                raise ProcessorValidationError(
                    f"processor '{processor.name}'.load is not callable"
                )

            run = processor.load()
            if not callable(run):
                raise ProcessorValidationError(
                    f"processor '{processor.name}' is missing required run() function"
                )

            _validate_processor_run_signature(name=processor.name, run=run)

        except Exception as exc:
            errors.append(f"{processor.name} ({processor.source}): {exc}")

    return errors


def _validate_processor_run_signature(*, name: str, run: Callable) -> None:
    try:
        sig = signature(run)
    except (TypeError, ValueError) as exc:
        raise ProcessorValidationError(
            f"unable to inspect run() signature for processor '{name}'"
        ) from exc

    parameters = sig.parameters
    accepts_extra_kwargs = any(
        parameter.kind is Parameter.VAR_KEYWORD for parameter in parameters.values()
    )
    missing = REQUIRED_RUN_KWARGS - set(parameters)
    if missing and not accepts_extra_kwargs:
        raise ProcessorValidationError(
            f"processor '{name}' is missing required run() parameters: "
            f"{sorted(missing)}"
        )

    for parameter in parameters.values():
        if parameter.kind is Parameter.POSITIONAL_ONLY:
            raise ProcessorValidationError(
                f"processor '{name}' uses positional-only parameter "
                f"'{parameter.name}', which is not supported"
            )
