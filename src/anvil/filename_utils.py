"""Shared helpers for filesystem-safe filename components."""


def safe_filename_component(name: str) -> str:
    """Return a sanitized filename component using Anvil's canonical rules.

    Args:
        name: Untrusted or provider-derived filename component.
    Returns:
        A filename component containing only alphanumeric characters, periods,
        hyphens, and underscores, without leading or trailing periods or
        underscores.
    """

    safe_name = "".join(
        character if character.isalnum() or character in {".", "-", "_"} else "_"
        for character in name
    )
    return safe_name.strip("._") or "target"
