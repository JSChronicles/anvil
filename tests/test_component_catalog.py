from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from anvil._components import (
    ComponentCatalog,
    ComponentDescriptor,
    ComponentKind,
    ComponentOrigin,
    ComponentResolutionError,
    ComponentResolver,
    ComponentSource,
    PackageComponentSource,
)


def _source(label: str, package: str = "components") -> ComponentSource:
    return ComponentSource(origin=ComponentOrigin.STOCK, package=package, label=label)


def test_catalog_is_immutable_and_retains_duplicate_candidates() -> None:
    first = ComponentDescriptor(name="shared", source=_source("stock"), load=object)
    second = ComponentDescriptor(
        name="shared",
        source=ComponentSource(
            origin=ComponentOrigin.PLUGIN,
            package="plugin_components",
            label="plugin: example",
            distribution="example",
        ),
        load=object,
    )

    catalog = ComponentCatalog.build([second, first])

    assert catalog.descriptors == (second, first)
    assert catalog.inventory["shared"] == (second, first)
    with pytest.raises(TypeError):
        catalog.inventory["other"] = ()  # type: ignore[index]


def test_resolver_rejects_missing_and_ambiguous_names() -> None:
    descriptors = [
        ComponentDescriptor(name="shared", source=_source("one"), load=object),
        ComponentDescriptor(name="shared", source=_source("two"), load=object),
        ComponentDescriptor(name="unique", source=_source("one"), load=lambda: 42),
    ]
    resolver = ComponentResolver(
        kind=ComponentKind.PROCESSOR, catalog=ComponentCatalog.build(descriptors)
    )

    assert resolver.load("unique") == 42
    with pytest.raises(ComponentResolutionError, match="ambiguous.*one.*two"):
        resolver.load("shared")
    with pytest.raises(ComponentResolutionError, match="Available processors"):
        resolver.load("missing")


def test_package_source_discovers_children_without_importing_them(
    monkeypatch, tmp_path: Path
) -> None:
    package_dir = tmp_path / "synthetic_components"
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    (package_dir / "alpha.py").write_text(
        "raise AssertionError('alpha imported during discovery')\n", encoding="utf-8"
    )
    (package_dir / "_private.py").write_text("", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    source = PackageComponentSource(
        package_name="synthetic_components",
        source=_source("stock", "synthetic_components"),
        component_loader=lambda package, name, origin: (package, name, origin.label),
    )
    descriptors, issues = source.discover()

    assert [descriptor.name for descriptor in descriptors] == ["alpha"]
    assert not issues
    assert "synthetic_components.alpha" not in sys.modules
    assert descriptors[0].load() == ("synthetic_components", "alpha", "stock")


def test_package_source_isolates_broken_package_root(
    tmp_path: Path, monkeypatch
) -> None:
    package_dir = tmp_path / "broken_components"
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text(
        "raise RuntimeError('broken package')\n", encoding="utf-8"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    descriptors, issues = PackageComponentSource(
        package_name="broken_components",
        source=_source("plugin: broken", "broken_components"),
        component_loader=lambda package, name, source: object(),
    ).discover(issue_name="broken-plugin")

    assert not descriptors
    assert issues[0].name == "broken-plugin"
    assert "broken package" in issues[0].error
