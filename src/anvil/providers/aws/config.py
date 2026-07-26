from __future__ import annotations

from anvil.descriptors import TargetDescriptor


DEFAULT_ORGANIZATION_ROLE_NAME = "OrganizationAccountAccessRole"


def aws_option(target: TargetDescriptor, name: str) -> str | None:
    """Return one validated AWS provider string option."""

    value = target.provider_options.get(name)
    return value if isinstance(value, str) else None
