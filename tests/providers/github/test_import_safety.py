from __future__ import annotations

import builtins
import sys

from anvil import provider_loader, task_loader, validators
from anvil.providers.base import validate_provider_contract


def test_offline_github_paths_do_not_import_pygithub(monkeypatch):
    original_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "github" or name.startswith("github."):
            raise AssertionError("offline provider paths must not import PyGithub")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(provider_loader, "entry_points", lambda *, group: [])

    providers = provider_loader.list_providers()
    github_descriptor = next(
        provider for provider in providers if provider.name == "github"
    )

    validators.validate_config_schema(
        config={
            "schema_version": 2,
            "targets": [
                {
                    "name": "github-repositories",
                    "provider": {
                        "name": "github",
                        "mode": "repositories",
                        "options": {"auth_type": "token"},
                    },
                    "regions": ["global"],
                    "include": ["octo-org/example"],
                    "tasks": [{"name": "noop"}],
                }
            ],
        }
    )
    validate_provider_contract(github_descriptor.load())
    task_loader.discover_tasks()

    assert "github" not in sys.modules
