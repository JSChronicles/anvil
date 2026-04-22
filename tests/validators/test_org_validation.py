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
        ValueError, match="without role_name must include exactly one account ID"
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


def test_max_parallel_regions_defaults_to_one():
    descriptor = TargetDescriptor(config_branch=ConfigBranch.ORGANIZATIONS, name="org")

    assert descriptor.max_parallel_regions == 1


def test_max_parallel_regions_accepts_maximum_value():
    descriptor = TargetDescriptor(
        config_branch=ConfigBranch.ORGANIZATIONS, name="org", max_parallel_regions=4
    )

    assert descriptor.max_parallel_regions == 4


@pytest.mark.parametrize("max_parallel_regions", [0, 5])
def test_max_parallel_regions_rejects_out_of_range_values(max_parallel_regions):
    with pytest.raises(ValueError, match="max_parallel_regions"):
        TargetDescriptor(
            config_branch=ConfigBranch.ORGANIZATIONS,
            name="org",
            max_parallel_regions=max_parallel_regions,
        )


def test_fail_fast_warns_when_combined_concurrency_is_high(caplog):
    from anvil.validators import validate_target_descriptors

    target = TargetDescriptor(
        config_branch=ConfigBranch.ORGANIZATIONS,
        name="org",
        max_workers=3,
        max_parallel_regions=4,
        fail_fast=True,
    )

    validate_target_descriptors(targets=[target])

    assert "combined account-region concurrency=12" in caplog.text


def test_fail_fast_does_not_warn_when_combined_concurrency_is_low(caplog):
    from anvil.validators import validate_target_descriptors

    target = TargetDescriptor(
        config_branch=ConfigBranch.ORGANIZATIONS,
        name="org",
        max_workers=2,
        max_parallel_regions=4,
        fail_fast=True,
    )

    validate_target_descriptors(targets=[target])

    assert "combined account-region concurrency" not in caplog.text
