"""GCP provider adapter."""

from typing import Any

__all__ = ["GcpProvider", "create_provider"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from anvil.providers.gcp.provider import GcpProvider, create_provider

        return {"GcpProvider": GcpProvider, "create_provider": create_provider}[name]

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
