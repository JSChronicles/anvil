"""AWS provider adapter."""

from typing import Any

__all__ = ["AwsProvider", "create_provider"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from anvil.providers.aws.provider import AwsProvider, create_provider

        return {"AwsProvider": AwsProvider, "create_provider": create_provider}[name]

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
