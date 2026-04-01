import pytest

from anvil.descriptors import ConfigBranch, TargetDescriptor


def test_duplicate_org_names():
    try:
        from anvil.validators import validate_target_descriptors
    except PermissionError as error:
        pytest.skip(f"jsonschema package resources unavailable in test env: {error}")

    targets = [
        TargetDescriptor(config_branch=ConfigBranch.ORGANIZATIONS, name="a"),
        TargetDescriptor(config_branch=ConfigBranch.ORGANIZATIONS, name="a"),
    ]

    with pytest.raises(ValueError):
        validate_target_descriptors(targets=targets)
