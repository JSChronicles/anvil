import pytest


def _import_validators_or_skip():
    try:
        from anvil import validators
    except PermissionError as error:
        pytest.skip(f"jsonschema package resources unavailable in test env: {error}")

    return validators


def test_load_config_descriptors_defaults_max_parallel_targets():
    validators = _import_validators_or_skip()

    loaded = validators.load_config_descriptors(
        config={"schema_version": 1, "organizations": [{"name": "org-a"}]}
    )

    assert loaded.max_parallel_targets == 1


def test_load_config_descriptors_reads_max_parallel_targets():
    validators = _import_validators_or_skip()

    loaded = validators.load_config_descriptors(
        config={
            "schema_version": 1,
            "max_parallel_targets": 3,
            "accounts": [{"name": "group-a", "include": ["111111111111"]}],
        }
    )

    assert loaded.max_parallel_targets == 3


def test_validate_config_schema_accepts_max_parallel_targets_for_organizations():
    validators = _import_validators_or_skip()

    validators.validate_config_schema(
        config={
            "schema_version": 1,
            "max_parallel_targets": 4,
            "organizations": [{"name": "org-a"}],
        }
    )


def test_validate_config_schema_accepts_max_parallel_targets_for_accounts():
    validators = _import_validators_or_skip()

    validators.validate_config_schema(
        config={
            "schema_version": 1,
            "max_parallel_targets": 2,
            "accounts": [{"name": "group-a", "include": ["111111111111"]}],
        }
    )


def test_validate_config_schema_rejects_invalid_max_parallel_targets():
    validators = _import_validators_or_skip()

    with pytest.raises(ValueError, match="max_parallel_targets"):
        validators.validate_config_schema(
            config={
                "schema_version": 1,
                "max_parallel_targets": 0,
                "organizations": [{"name": "org-a"}],
            }
        )
