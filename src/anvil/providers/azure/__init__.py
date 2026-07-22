"""Azure provider adapter."""

from typing import Any

__all__ = ["AzureProvider", "create_provider_instance"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from anvil.providers.azure.provider import (
            AzureProvider,
            create_provider_instance,
        )

        return {
            "AzureProvider": AzureProvider,
            "create_provider_instance": create_provider_instance,
        }[name]

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
