from __future__ import annotations

import builtins
import sys

from anvil import provider_loader, task_loader, validators
from anvil.providers.base import validate_provider_contract


def test_offline_datadog_paths_do_not_import_optional_sdk(monkeypatch) -> None:
    """Keep provider discovery and schema validation independent of the SDK."""

    original_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "datadog_api_client" or name.startswith("datadog_api_client."):
            raise AssertionError("offline provider paths must not import Datadog SDK")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(provider_loader, "entry_points", lambda *, group: [])
    provider_loader._clear_provider_caches()

    providers = provider_loader.list_providers()
    datadog_descriptor = next(
        provider for provider in providers if provider.name == "datadog"
    )

    validators.validate_config_schema(
        config={
            "schema_version": 2,
            "targets": [
                {
                    "name": "production-observability",
                    "provider": {
                        "name": "datadog",
                        "mode": "organization",
                        "options": {
                            "site": "datadoghq.eu",
                            "api_key_env": "PROD_DD_API_KEY",
                            "app_key_env": "PROD_DD_APP_KEY",
                        },
                    },
                    "regions": ["global"],
                    "tasks": [{"name": "noop"}],
                }
            ],
        }
    )
    validate_provider_contract(datadog_descriptor.load())
    task_loader.discover_tasks()

    first_provider = provider_loader.load_provider("datadog")
    second_provider = provider_loader.load_provider("datadog")

    assert not any(
        name == "datadog_api_client" or name.startswith("datadog_api_client.")
        for name in sys.modules
    )
    assert first_provider.metadata.name == "datadog"
    assert second_provider is not first_provider
