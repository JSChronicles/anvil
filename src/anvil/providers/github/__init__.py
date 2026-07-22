"""GitHub provider adapter."""

from typing import Any

__all__ = ["GithubProvider", "create_provider_instance"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from anvil.providers.github.provider import (
            GithubProvider,
            create_provider_instance,
        )

        return {
            "GithubProvider": GithubProvider,
            "create_provider_instance": create_provider_instance,
        }[name]

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
