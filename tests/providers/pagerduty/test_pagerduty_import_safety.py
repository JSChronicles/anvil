from __future__ import annotations

import builtins
import sys

import pytest

from anvil import provider_loader, task_loader, validators
from anvil.providers.base import validate_provider_contract
from anvil.providers.pagerduty.session import PagerDutySessionFactory


def test_offline_pagerduty_paths_do_not_import_sdk(monkeypatch) -> None:
    """Keep discovery, schema validation, and task loading SDK-independent."""

    original_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "pagerduty" or name.startswith("pagerduty."):
            raise AssertionError("offline provider paths must not import pagerduty")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(provider_loader, "entry_points", lambda *, group: [])
    provider_loader._clear_provider_caches()

    providers = provider_loader.list_providers()
    descriptor = next(
        provider for provider in providers if provider.name == "pagerduty"
    )
    validators.validate_config_schema(
        config={
            "schema_version": 2,
            "targets": [
                {
                    "name": "pagerduty-account",
                    "provider": {
                        "name": "pagerduty",
                        "mode": "account",
                        "options": {"token_env": "PAGERDUTY_API_TOKEN"},
                    },
                    "tasks": [{"name": "noop"}],
                }
            ],
        }
    )
    validate_provider_contract(descriptor.load())
    task_loader.discover_tasks()

    assert "pagerduty" not in sys.modules


def test_session_factory_maps_import_failure_to_actionable_error(monkeypatch) -> None:
    """Map the actual lazy import boundary, not only injected factory failures."""

    original_import = builtins.__import__

    def missing_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "pagerduty":
            raise ImportError("pagerduty is unavailable")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", missing_import)

    with pytest.raises(RuntimeError, match=r"anvil\[pagerduty\]"):
        PagerDutySessionFactory._load_pagerduty()
