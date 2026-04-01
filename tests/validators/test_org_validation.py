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


def test_accounts_direct_mode_requires_single_account():
    with pytest.raises(
        ValueError,
        match="without role_name must include exactly one account ID",
    ):
        TargetDescriptor(
            config_branch=ConfigBranch.ACCOUNTS,
            name="direct-rollout",
            include=["111111111111", "222222222222"],
        )


def test_accounts_assume_role_mode_allows_multiple_accounts():
    descriptor = TargetDescriptor(
        config_branch=ConfigBranch.ACCOUNTS,
        name="assume-role-rollout",
        role_name="OrganizationAccountAccessRole",
        include=["111111111111", "222222222222"],
    )

    assert descriptor.include == ["111111111111", "222222222222"]
