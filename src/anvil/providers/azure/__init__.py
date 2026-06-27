"""Azure provider adapter."""

from typing import Any

__all__ = ["AzureProvider", "create_provider"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from anvil.providers.azure.provider import AzureProvider, create_provider

        return {"AzureProvider": AzureProvider, "create_provider": create_provider}[
            name
        ]

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
