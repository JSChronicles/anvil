"""PagerDuty provider adapter."""

from typing import Any

__all__ = ["PagerDutyProvider", "create_provider_instance"]


def __getattr__(name: str) -> Any:
    """Load PagerDuty provider objects without importing the optional SDK."""

    if name in __all__:
        from anvil.providers.pagerduty.provider import (
            PagerDutyProvider,
            create_provider_instance,
        )

        return {
            "PagerDutyProvider": PagerDutyProvider,
            "create_provider_instance": create_provider_instance,
        }[name]

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
