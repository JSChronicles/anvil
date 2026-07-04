"""GitHub provider adapter."""

from typing import Any

__all__ = ["GithubProvider", "create_provider"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from anvil.providers.github.provider import GithubProvider, create_provider

        return {"GithubProvider": GithubProvider, "create_provider": create_provider}[
            name
        ]

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
