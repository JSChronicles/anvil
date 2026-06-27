"""Compatibility wrapper for `anvil.providers.aws.tasks.get_aws_inline_policies`."""

from importlib import import_module

_IMPL = import_module("anvil.providers.aws.tasks.get_aws_inline_policies")
run = _IMPL.run


def __getattr__(name: str):
    return getattr(_IMPL, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(dir(_IMPL)))