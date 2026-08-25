"""GitLab provider adapter."""

from typing import Any

__all__ = ["GitLabProvider", "create_provider_instance"]


def __getattr__(name: str) -> Any:
    """Load the GitLab provider implementation only when selected."""

    if name in __all__:
        from anvil.providers.gitlab.provider import (
            GitLabProvider,
            create_provider_instance,
        )

        return {
            "GitLabProvider": GitLabProvider,
            "create_provider_instance": create_provider_instance,
        }[name]

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
