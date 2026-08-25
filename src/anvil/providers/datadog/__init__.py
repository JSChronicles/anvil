"""Datadog provider adapter."""

from typing import Any

__all__ = ["DatadogProvider", "create_provider_instance"]


def __getattr__(name: str) -> Any:
    """Load the Datadog provider without importing its optional SDK."""

    if name in __all__:
        from anvil.providers.datadog.provider import (
            DatadogProvider,
            create_provider_instance,
        )

        return {
            "DatadogProvider": DatadogProvider,
            "create_provider_instance": create_provider_instance,
        }[name]

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
