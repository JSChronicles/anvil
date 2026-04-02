from __future__ import annotations

import importlib.resources
import json
import logging
from functools import lru_cache

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from anvil.descriptors import ConfigBranch, LoadedConfig, TargetDescriptor

__LOGGER__ = logging.getLogger(__name__)

SCHEMA_REGISTRY: dict[str, str] = {
    ConfigBranch.ORGANIZATIONS.value: "orgs.schema.v1.json",
    ConfigBranch.ACCOUNTS.value: "accounts.schema.v1.json",
}

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
    has_organizations = "organizations" in config
    has_accounts = "accounts" in config

    if has_organizations and has_accounts:
        raise ValueError(
            "Config must contain exactly one top-level branch: "
            "'organizations' or 'accounts'"
        )

    if has_organizations:
        return ConfigBranch.ORGANIZATIONS

    if has_accounts:
        return ConfigBranch.ACCOUNTS

    raise ValueError(
        "Config must contain exactly one top-level branch: "
        "'organizations' or 'accounts'"
    )


def _load_branch_schema(*, branch: ConfigBranch, schema_version: int) -> dict:
    if schema_version != 1:
        raise ValueError(
            f"Unsupported schema_version: {schema_version}. Supported versions: [1]"
        )

    schema_file = SCHEMA_REGISTRY[branch.value]
    return _load_schema_file(schema_file)


def _format_schema_error_location(*, config: dict, error) -> str:
    path_parts = list(error.path)
    location = ".".join(str(path) for path in path_parts) or "root"

    if len(path_parts) >= 2 and isinstance(path_parts[1], int):
        branch_name = path_parts[0]
        entry_index = path_parts[1]

        if branch_name not in {
            ConfigBranch.ORGANIZATIONS.value,
            ConfigBranch.ACCOUNTS.value,
        }:
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

        label = (
            "account_group"
            if branch_name == ConfigBranch.ACCOUNTS.value
            else "organization"
        )
        return f"{label} '{entry_name}' ({location})"

    return location


@lru_cache(maxsize=1)
def _build_schema_registry() -> Registry:
    schema_files = {
        COMMON_SCHEMA_FILE,
        SCHEMA_REGISTRY[ConfigBranch.ORGANIZATIONS.value],
        SCHEMA_REGISTRY[ConfigBranch.ACCOUNTS.value],
    }
    registry = Registry()

    for schema_file in schema_files:
        schema = _load_schema_file(schema_file)
        schema_uri = schema.get("$id")

        if not isinstance(schema_uri, str) or not schema_uri:
            schema_uri = f"{SCHEMA_BASE_URI}{schema_file}"

        registry = registry.with_resource(
            uri=schema_uri,
            resource=Resource.from_contents(schema),
        )

    return registry


def validate_config_schema(*, config: dict) -> None:
    """
    Validate config against the packaged JSON Schema selected by top-level branch.
    """
    schema_version = config.get("schema_version")

    if not isinstance(schema_version, int):
        raise ValueError("Missing or invalid 'schema_version' (must be an integer)")

    branch = _detect_config_branch(config)
    schema = _load_branch_schema(branch=branch, schema_version=schema_version)
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

    targets: list[TargetDescriptor] = []

    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"{branch.value} entry #{index} must be a mapping")

        targets.append(TargetDescriptor(config_branch=branch, **entry))

    validate_target_descriptors(targets=targets)

    return LoadedConfig(branch=branch, targets=targets)


def validate_target_descriptors(*, targets: list[TargetDescriptor]) -> None:
    """
    Validate semantic correctness across loaded target descriptors.
    """
    seen_names: set[str] = set()

    for target in targets:
        if target.name in seen_names:
            raise ValueError(f"Duplicate target name detected: '{target.name}'")

        seen_names.add(target.name)

        if target.fail_fast and target.max_workers > 10:
            __LOGGER__.warning(
                f"Target '{target.name}' has fail_fast enabled with "
                f"max_workers={target.max_workers}"
            )
