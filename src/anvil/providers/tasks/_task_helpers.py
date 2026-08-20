"""Shared import-safe helpers for first-party provider tasks."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import Enum

DEFAULT_MAX_RESULTS = 1_000


def require_provider(*, task_name: str, provider: str, expected: str) -> None:
    """Require a task to run with its owning provider."""

    if provider != expected:
        raise RuntimeError(f"{task_name} requires the {expected} provider")


def require_target_type(
    *, task_name: str, execution_target_type: str, expected: str
) -> None:
    """Require the concrete provider target type expected by a task."""

    if execution_target_type != expected:
        raise RuntimeError(f"{task_name} requires a {expected} target")


def metadata_int(
    *, metadata: dict[str, object], key: str, default: int = DEFAULT_MAX_RESULTS
) -> int:
    """Return a positive integer metadata value."""

    value = metadata.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise RuntimeError(f"metadata.{key} must be a positive integer")
    return value


def metadata_string(
    *, task_name: str, metadata: dict[str, object], key: str, required: bool = False
) -> str | None:
    """Return a normalized optional or required string metadata value."""

    value = metadata.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        requirement = "requires" if required else "expects"
        raise RuntimeError(
            f"{task_name} {requirement} metadata.{key} to be a non-empty string"
        )
    return value.strip()


def metadata_string_array(
    *, task_name: str, metadata: dict[str, object], key: str, required: bool = False
) -> list[str] | None:
    """Return a normalized array of unique non-empty string selectors."""

    value = metadata.get(key)
    if value is None:
        if required:
            raise RuntimeError(
                f"{task_name} requires metadata.{key} to be a non-empty array"
            )
        return None
    if not isinstance(value, list) or not value:
        raise RuntimeError(
            f"{task_name} expects metadata.{key} to be a non-empty array"
        )
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise RuntimeError(
                f"{task_name} expects metadata.{key} to contain only non-empty strings"
            )
        selector = item.strip()
        if selector not in seen:
            normalized.append(selector)
            seen.add(selector)
    return normalized


def json_safe(value: object) -> object:
    """Convert SDK models and nested containers into JSON-serializable values."""

    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Enum):
        return json_safe(value.value)
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [json_safe(item) for item in value]
    for method_name in ("to_dict", "as_dict"):
        method = getattr(value, method_name, None)
        if callable(method):
            return json_safe(method())
    attributes = getattr(value, "__dict__", None)
    if isinstance(attributes, dict):
        return {
            key: json_safe(item)
            for key, item in attributes.items()
            if not key.startswith("_")
        }
    return str(value)


def mapping_identifier(value: object) -> str | None:
    """Return a normalized ``id`` from a serialized SDK mapping."""

    if not isinstance(value, Mapping):
        return None
    identifier = value.get("id")
    return None if identifier is None else str(identifier)


def bounded(items: Iterable[object], *, max_results: int) -> list[object]:
    """Consume at most ``max_results`` items from an SDK iterable."""

    results: list[object] = []
    for item in items:
        results.append(json_safe(item))
        if len(results) >= max_results:
            break
    return results
