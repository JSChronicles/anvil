import importlib.resources
import json
import logging

from jsonschema import Draft202012Validator

from anvil.descriptors import OrgDescriptor

__LOGGER__ = logging.getLogger(__name__)

SCHEMA_REGISTRY: dict[int, str] = {
    1: "orgs.schema.v1.json"
    # 2: "orgs.schema.v2.json",
}


def _load_org_schema(*, schema_version: int) -> dict:
    """
    Load the organization JSON schema for a specific schema version.
    """

    try:
        schema_file = SCHEMA_REGISTRY[schema_version]
    except KeyError as error:
        raise ValueError(
            f"Unsupported schema_version: {schema_version}. "
            f"Supported versions: {sorted(SCHEMA_REGISTRY)}"
        ) from error

    with (
        importlib.resources.files("anvil.schemas")
        .joinpath(schema_file)
        .open("r", encoding="utf-8")
    ) as handle:
        return json.load(handle)


def validate_org_config_schema(*, config: dict) -> None:
    """
    Validate org configuration against the packaged JSON Schema.
    """

    schema_version = config.get("schema_version")

    if not isinstance(schema_version, int):
        raise ValueError("Missing or invalid 'schema_version' (must be an integer)")

    schema = _load_org_schema(schema_version=schema_version)

    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(config), key=lambda error: error.path)

    if errors:
        messages: list[str] = []

        for error in errors:
            location = ".".join(str(path) for path in error.path)
            messages.append(f"{location or 'root'}: {error.message}")

        raise ValueError("Org config schema validation failed:\n" + "\n".join(messages))


def validate_org_descriptors(orgs: list[OrgDescriptor]) -> None:
    """
    Validate semantic correctness across organization descriptors.

    Raises:
        ValueError: if semantic validation fails
    """

    seen_names: set[str] = set()

    for organization in orgs:
        if organization.name in seen_names:
            raise ValueError(
                f"Duplicate organization name detected: '{organization.name}'"
            )

        seen_names.add(organization.name)

        # Advisory rule (non-fatal)
        if organization.fail_fast and organization.max_workers > 10:
            __LOGGER__.warning(
                f"Org '{organization.name}' has fail_fast enabled with "
                f"max_workers={organization.max_workers}"
            )
