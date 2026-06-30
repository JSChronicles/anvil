from __future__ import annotations

import importlib.resources
import json
import logging
from functools import lru_cache

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from anvil.descriptors import ConfigBranch, LoadedConfig, TargetDescriptor

__LOGGER__ = logging.getLogger(__name__)

TARGETS_SCHEMA_FILE = "targets.schema.v2.json"

COMMON_SCHEMA_FILE = "common.schema.v1.json"
SCHEMA_BASE_URI = "https://anvil.local/schemas/"


def _load_schema_file(schema_file: str) -> dict:
    with (
        importlib.resources.files("anvil.schemas")
        .joinpath(schema_file)
        .open("r", encoding="utf-8")
    ) as handle:
        return json.load(handle)


def _detect_config_branch(config: dict) -> ConfigBranch:
    if config.get("schema_version") != 2:
        raise ValueError(
            "Unsupported config schema. Anvil v0.30 requires "
            "schema_version: 2 with top-level 'targets'."
        )

    if "targets" not in config:
        raise ValueError("schema_version 2 configs must contain top-level 'targets'")

    if "organizations" in config or "accounts" in config:
        raise ValueError(
            "Anvil v0.30 configs use top-level 'targets', not "
            "'organizations' or 'accounts'."
        )

    return ConfigBranch.TARGETS


@lru_cache(maxsize=1)
def _load_targets_schema() -> dict:
    return _load_schema_file(TARGETS_SCHEMA_FILE)


def _format_schema_error_location(*, config: dict, error) -> str:
    path_parts = list(error.path)
    location = ".".join(str(path) for path in path_parts) or "root"

    if len(path_parts) >= 2 and isinstance(path_parts[1], int):
        branch_name = path_parts[0]
        entry_index = path_parts[1]

        if branch_name not in {branch.value for branch in ConfigBranch}:
            return location

        entries = config.get(branch_name, [])
        if not isinstance(entries, list) or not (0 <= entry_index < len(entries)):
            return location

        entry = entries[entry_index]
        if not isinstance(entry, dict):
            return location

        entry_name = entry.get("name")
        if not isinstance(entry_name, str) or not entry_name.strip():
            return location

        label = {
            ConfigBranch.ACCOUNTS.value: "account_group",
            ConfigBranch.ORGANIZATIONS.value: "organization",
            ConfigBranch.TARGETS.value: "target",
        }[branch_name]
        return f"{label} '{entry_name}' ({location})"

    return location


@lru_cache(maxsize=1)
def _build_schema_registry() -> Registry:
    schema_files = {COMMON_SCHEMA_FILE, TARGETS_SCHEMA_FILE}
    registry = Registry()

    for schema_file in schema_files:
        schema = _load_schema_file(schema_file)
        schema_uri = schema.get("$id")

        if not isinstance(schema_uri, str) or not schema_uri:
            schema_uri = f"{SCHEMA_BASE_URI}{schema_file}"

        registry = registry.with_resource(
            uri=schema_uri, resource=Resource.from_contents(schema)
        )

    return registry


def validate_config_schema(*, config: dict) -> None:
    """
    Validate config against the packaged JSON Schema selected by top-level branch.
    """
    schema_version = config.get("schema_version")

    if not isinstance(schema_version, int):
        raise ValueError(
            "Missing or invalid 'schema_version'. Anvil v0.30 requires "
            "schema_version: 2 with top-level 'targets'."
        )

    _detect_config_branch(config)
    schema = _load_targets_schema()
    registry = _build_schema_registry()

    validator = Draft202012Validator(schema, registry=registry)
    errors = sorted(validator.iter_errors(config), key=lambda error: error.path)

    if errors:
        messages: list[str] = []

        for error in errors:
            location = _format_schema_error_location(config=config, error=error)
            messages.append(f"{location}: {error.message}")

        raise ValueError("Config schema validation failed:\n" + "\n".join(messages))


def load_config_descriptors(*, config: dict) -> LoadedConfig:
    """
    Load and semantically validate target descriptors from config.

    Assumes schema validation has already succeeded.
    """
    branch = _detect_config_branch(config)
    entries = config[branch.value]
    max_parallel_targets = config.get("max_parallel_targets", 1)

    targets: list[TargetDescriptor] = []

    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"{branch.value} entry #{index} must be a mapping")

        normalized_entry = _normalize_v2_target_entry(entry=entry, index=index)
        targets.append(TargetDescriptor(config_branch=branch, **normalized_entry))

    validate_target_descriptors(targets=targets)

    return LoadedConfig(
        branch=branch, max_parallel_targets=max_parallel_targets, targets=targets
    )


def _normalize_v2_target_entry(*, entry: dict, index: int) -> dict:
    provider = entry.get("provider")
    if not isinstance(provider, dict):
        raise ValueError(f"targets entry #{index} requires provider mapping")

    provider_name = provider.get("name")
    provider_mode = provider.get("mode")
    provider_options = provider.get("options", {})

    if not isinstance(provider_name, str) or not provider_name.strip():
        raise ValueError(f"targets entry #{index} provider.name is required")
    if not isinstance(provider_mode, str) or not provider_mode.strip():
        raise ValueError(f"targets entry #{index} provider.mode is required")
    if provider_options is None:
        provider_options = {}
    if not isinstance(provider_options, dict):
        raise ValueError(f"targets entry #{index} provider.options must be a mapping")

    normalized = dict(entry)
    normalized["provider"] = provider_name
    normalized["mode"] = provider_mode
    normalized["provider_options"] = dict(provider_options)
    return normalized


def validate_target_descriptors(*, targets: list[TargetDescriptor]) -> None:
    """
    Validate semantic correctness across loaded target descriptors.
    """
    seen_names: set[str] = set()

    for target in targets:
        if target.name in seen_names:
            raise ValueError(f"Duplicate target name detected: '{target.name}'")

        seen_names.add(target.name)

        combined_concurrency = target.max_workers * target.max_parallel_regions
        if target.fail_fast and combined_concurrency > 10:
            __LOGGER__.warning(
                f"Target '{target.name}' has fail_fast enabled with "
                f"combined account-region concurrency={combined_concurrency} "
                f"(max_workers={target.max_workers}, "
                f"max_parallel_regions={target.max_parallel_regions})"
            )
