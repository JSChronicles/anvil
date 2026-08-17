from __future__ import annotations

import builtins
import sys

from anvil import provider_loader, task_loader, validators
from anvil.providers.base import validate_provider_contract


def test_offline_gitlab_paths_do_not_import_python_gitlab(monkeypatch) -> None:
    original_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "gitlab" or name.startswith("gitlab."):
            raise AssertionError("offline provider paths must not import python-gitlab")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.delitem(sys.modules, "gitlab", raising=False)
    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(provider_loader, "entry_points", lambda *, group: [])
    provider_loader._clear_provider_caches()

    providers = provider_loader.list_providers()
    gitlab_descriptor = next(
        provider for provider in providers if provider.name == "gitlab"
    )
    validators.validate_config_schema(
        config={
            "schema_version": 2,
            "targets": [
                {
                    "name": "gitlab-projects",
                    "provider": {
                        "name": "gitlab",
                        "mode": "projects",
                        "options": {"token_env": "GITLAB_TOKEN"},
                    },
                    "regions": ["global"],
                    "include": ["group/project"],
                    "tasks": [{"name": "noop"}],
                }
            ],
        }
    )
    validate_provider_contract(gitlab_descriptor.load())
    task_loader.discover_tasks()

    assert "gitlab" not in sys.modules
