"""
Processor validation for Anvil.

This module performs structural validation of post-run processor definitions.
It loads processor modules to inspect run(...) signatures, but does not execute
processors against result data.
"""

from __future__ import annotations

from collections.abc import Callable
from inspect import getdoc, getmodule

from anvil._components import validate_keyword_only_invocation
from anvil.processor_loader import ProcessorDescriptor

PROCESSOR_RUN_KWARGS = frozenset({"context", "output", "metadata"})


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
            _validate_processor_detail_docstring(name=processor.name, run=run)

        except Exception as exc:
            errors.append(f"{processor.name} ({processor.source}): {exc}")

    return errors


def _validate_processor_run_signature(*, name: str, run: Callable) -> None:
    try:
        validate_keyword_only_invocation(run, keyword_names=PROCESSOR_RUN_KWARGS)
    except ValueError as exc:
        raise ProcessorValidationError(
            f"processor '{name}' has incompatible run() signature: {exc}"
        ) from exc


def _validate_processor_detail_docstring(*, name: str, run: Callable) -> None:
    doc = getdoc(run)
    if doc is None:
        module = getmodule(run)
        if module is not None:
            doc = getdoc(module)

    if doc is None:
        raise ProcessorValidationError(
            f"processor '{name}' is missing detail documentation; add a "
            "Google-style run() docstring for 'anvil list --processors --detail'"
        )
