"""Cloudflare provider adapter."""

from typing import Any

__all__ = ["CloudflareProvider", "create_provider_instance"]


def __getattr__(name: str) -> Any:
    """Load public provider objects without importing the optional SDK."""

    if name in __all__:
        from anvil.providers.cloudflare.provider import (
            CloudflareProvider,
            create_provider_instance,
        )

        return {
            "CloudflareProvider": CloudflareProvider,
            "create_provider_instance": create_provider_instance,
        }[name]

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
