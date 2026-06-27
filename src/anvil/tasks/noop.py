"""Compatibility wrapper for `anvil.providers.tasks.noop`."""

from importlib import import_module

_IMPL = import_module("anvil.providers.tasks.noop")
run = _IMPL.run


def __getattr__(name: str):
    return getattr(_IMPL, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(dir(_IMPL)))