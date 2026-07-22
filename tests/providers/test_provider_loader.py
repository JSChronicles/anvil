from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from anvil import provider_loader


def test_list_providers_returns_aws_without_loading_provider(monkeypatch):
    monkeypatch.setattr(provider_loader, "entry_points", lambda *, group: [])

    providers = provider_loader.list_providers()

    assert [
        (provider.name, provider.display_name, provider.source)
        for provider in providers
    ] == [
        ("aws", "AWS", "stock"),
        ("azure", "Azure", "stock"),
        ("gcp", "GCP", "stock"),
        ("github", "GitHub", "stock"),
    ]


def test_load_provider_rejects_duplicate_package_candidates(monkeypatch, tmp_path):
    package_dir = tmp_path / "duplicate_providers"
    provider_dir = package_dir / "aws"
    provider_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    (provider_dir / "__init__.py").write_text(
        "raise AssertionError('ambiguous provider must not be imported')\n",
        encoding="utf-8",
    )

    class Distribution:
        name = "duplicate-providers"

    class EntryPoint:
        name = "duplicate-providers"
        value = "duplicate_providers"
        group = provider_loader.PROVIDER_PACKAGE_ENTRY_POINT_GROUP
        dist = Distribution()

    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    monkeypatch.setattr(
        provider_loader,
        "entry_points",
        lambda *, group: [EntryPoint()],
    )

    with pytest.raises(ValueError, match="ambiguous.*duplicate-providers.*stock"):
        provider_loader.load_provider("aws")
    assert "duplicate_providers.aws" not in sys.modules


def test_provider_package_entry_point_discovers_child_folders(
    monkeypatch, tmp_path: Path
) -> None:
    package_dir = tmp_path / "company_providers"
    provider_dir = package_dir / "custom"
    provider_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    (provider_dir / "__init__.py").write_text(
        (
            "from anvil.providers.base import ProviderMetadata\n"
            "class CustomProvider:\n"
            "    metadata = ProviderMetadata(name='custom', display_name='Custom')\n"
            "def create_provider_instance():\n"
            "    return CustomProvider()\n"
        ),
        encoding="utf-8",
    )

    class Distribution:
        name = "company-anvil"

    class EntryPoint:
        name = "company-providers"
        value = "company_providers"
        group = provider_loader.PROVIDER_PACKAGE_ENTRY_POINT_GROUP
        dist = Distribution()

    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    monkeypatch.setattr(
        provider_loader,
        "entry_points",
        lambda *, group: (
            [EntryPoint()]
            if group == provider_loader.PROVIDER_PACKAGE_ENTRY_POINT_GROUP
            else []
        ),
    )

    descriptor = next(
        item
        for item in provider_loader.discover_providers().providers
        if item.name == "custom"
    )

    assert descriptor.source == "plugin: company-anvil"
    assert "company_providers.custom" not in sys.modules

    first_provider = provider_loader.load_provider("custom")
    second_provider = provider_loader.load_provider("custom")

    assert first_provider.metadata.name == "custom"
    assert second_provider is not first_provider
