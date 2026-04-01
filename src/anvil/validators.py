from __future__ import annotations

import importlib.resources
import json
import logging
from pathlib import Path

from jsonschema import Draft202012Validator, RefResolver

from anvil.descriptors import ConfigBranch, LoadedConfig, TargetDescriptor

__LOGGER__ = logging.getLogger(__name__)

SCHEMA_REGISTRY: dict[str, str] = {
    ConfigBranch.ORGANIZATIONS.value: "orgs.schema.v1.json",
    ConfigBranch.ACCOUNTS.value: "accounts.schema.v1.json",
}

COMMON_SCHEMA_FILE = "common.schema.v1.json"


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


def _build_schema_resolver(*, branch_schema: dict) -> RefResolver:
    schema_dir = importlib.resources.files("anvil.schemas")
    base_uri = Path(schema_dir).as_uri() + "/"

    store = {
        f"{base_uri}{COMMON_SCHEMA_FILE}": _load_schema_file(COMMON_SCHEMA_FILE),
        f"{base_uri}{SCHEMA_REGISTRY[ConfigBranch.ORGANIZATIONS.value]}": _load_schema_file(
            SCHEMA_REGISTRY[ConfigBranch.ORGANIZATIONS.value]
        ),
        f"{base_uri}{SCHEMA_REGISTRY[ConfigBranch.ACCOUNTS.value]}": _load_schema_file(
            SCHEMA_REGISTRY[ConfigBranch.ACCOUNTS.value]
        ),
        "common.schema.v1.json": _load_schema_file(COMMON_SCHEMA_FILE),
        "orgs.schema.v1.json": _load_schema_file(
            SCHEMA_REGISTRY[ConfigBranch.ORGANIZATIONS.value]
        ),
        "accounts.schema.v1.json": _load_schema_file(
            SCHEMA_REGISTRY[ConfigBranch.ACCOUNTS.value]
        ),
    }

    return RefResolver(base_uri=base_uri, referrer=branch_schema, store=store)


def validate_config_schema(*, config: dict) -> None:
    """
    Validate config against the packaged JSON Schema selected by top-level branch.
    """
    schema_version = config.get("schema_version")

    if not isinstance(schema_version, int):
        raise ValueError("Missing or invalid 'schema_version' (must be an integer)")

    branch = _detect_config_branch(config)
    schema = _load_branch_schema(branch=branch, schema_version=schema_version)
    resolver = _build_schema_resolver(branch_schema=schema)

    validator = Draft202012Validator(schema, resolver=resolver)
    errors = sorted(validator.iter_errors(config), key=lambda error: error.path)

    if errors:
        messages: list[str] = []

        for error in errors:
            location = ".".join(str(path) for path in error.path)
            messages.append(f"{location or 'root'}: {error.message}")

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
