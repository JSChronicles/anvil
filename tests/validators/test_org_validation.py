import pytest

from anvil.descriptors import ConfigBranch, TargetDescriptor


def test_duplicate_org_names():
    try:
        from anvil.validators import validate_target_descriptors
    except PermissionError as error:
        pytest.skip(f"jsonschema package resources unavailable in test env: {error}")

    targets = [
        TargetDescriptor(config_branch=ConfigBranch.TARGETS, name="a"),
        TargetDescriptor(config_branch=ConfigBranch.TARGETS, name="a"),
    ]

    with pytest.raises(ValueError):
        validate_target_descriptors(targets=targets)


def test_accounts_direct_mode_requires_single_account():
    with pytest.raises(
        ValueError, match="without role_name must include exactly one account ID"
    ):
        TargetDescriptor(
            config_branch=ConfigBranch.TARGETS,
            name="direct-rollout",
            include=["111111111111", "222222222222"],
        )


def test_accounts_assume_role_mode_allows_multiple_accounts():
    descriptor = TargetDescriptor(
        config_branch=ConfigBranch.TARGETS,
        name="assume-role-rollout",
        role_name="OrganizationAccountAccessRole",
        include=["111111111111", "222222222222"],
    )

    assert descriptor.include == ["111111111111", "222222222222"]


def test_azure_subscription_mode_allows_multiple_target_ids():
    descriptor = TargetDescriptor(
        config_branch=ConfigBranch.TARGETS,
        name="azure-subscriptions",
        provider="azure",
        mode="subscriptions",
        include=["sub-a", "sub-b"],
    )

    assert descriptor.provider == "azure"
    assert descriptor.mode == "subscriptions"
    assert descriptor.include == ["sub-a", "sub-b"]


def test_gcp_project_mode_allows_multiple_target_ids():
    descriptor = TargetDescriptor(
        config_branch=ConfigBranch.TARGETS,
        name="gcp-projects",
        provider="gcp",
        mode="projects",
        include=["project-a", "project-b"],
    )

    assert descriptor.provider == "gcp"
    assert descriptor.mode == "projects"
    assert descriptor.include == ["project-a", "project-b"]


def test_github_organization_mode_allows_multiple_org_logins():
    descriptor = TargetDescriptor(
        config_branch=ConfigBranch.TARGETS,
        name="github-organizations",
        provider="github",
        mode="organizations",
        include=["octo-org", "another-org"],
    )

    assert descriptor.provider == "github"
    assert descriptor.mode == "organizations"
    assert descriptor.include == ["octo-org", "another-org"]


def test_github_repository_mode_allows_owner_repo_values():
    descriptor = TargetDescriptor(
        config_branch=ConfigBranch.TARGETS,
        name="github-repositories",
        provider="github",
        mode="repositories",
        include=["octo-org/example"],
        provider_options={"app_id": "12345", "private_key_path": "./app.pem"},
    )

    assert descriptor.provider == "github"
    assert descriptor.mode == "repositories"
    assert descriptor.provider_options == {
        "app_id": "12345",
        "private_key_path": "./app.pem",
    }


def test_github_modes_require_include():
    with pytest.raises(ValueError, match="requires include"):
        TargetDescriptor(
            config_branch=ConfigBranch.TARGETS,
            name="github-repositories",
            provider="github",
            mode="repositories",
        )


def test_invalid_provider_is_rejected():
    with pytest.raises(ValueError, match="Unsupported provider"):
        TargetDescriptor(
            config_branch=ConfigBranch.TARGETS,
            name="unknown",
            provider="do",
            include=["target-a"],
        )


def test_invalid_provider_mode_is_rejected():
    with pytest.raises(ValueError, match="Unsupported mode"):
        TargetDescriptor(
            config_branch=ConfigBranch.TARGETS,
            name="azure-subscriptions",
            provider="azure",
            mode="projects",
            include=["sub-a"],
        )


def test_invalid_provider_options_are_rejected():
    with pytest.raises(ValueError, match="Unsupported provider.options"):
        TargetDescriptor(
            config_branch=ConfigBranch.TARGETS,
            name="gcp-projects",
            provider="gcp",
            mode="projects",
            include=["project-a"],
            provider_options={"tenant_id": "wrong-cloud"},
        )


def test_provider_options_profile_conflict_is_rejected():
    with pytest.raises(ValueError, match="provider.options.profile"):
        TargetDescriptor(
            config_branch=ConfigBranch.TARGETS,
            name="aws-accounts",
            profile="dev",
            include=["111111111111"],
            provider_options={"profile": "prod"},
        )


def test_provider_options_role_name_conflict_is_rejected():
    with pytest.raises(ValueError, match="provider.options.role_name"):
        TargetDescriptor(
            config_branch=ConfigBranch.TARGETS,
            name="aws-accounts",
            role_name="AuditRole",
            include=["111111111111"],
            provider_options={"role_name": "ReadOnlyRole"},
        )


def test_matching_top_level_and_provider_options_profile_is_accepted():
    descriptor = TargetDescriptor(
        config_branch=ConfigBranch.TARGETS,
        name="aws-accounts",
        profile="dev",
        include=["111111111111"],
        provider_options={"profile": "dev"},
    )

    assert descriptor.profile == "dev"


def test_matching_top_level_and_provider_options_role_name_is_accepted():
    descriptor = TargetDescriptor(
        config_branch=ConfigBranch.TARGETS,
        name="aws-accounts",
        role_name="AuditRole",
        include=["111111111111", "222222222222"],
        provider_options={"role_name": "AuditRole"},
    )

    assert descriptor.role_name == "AuditRole"


def test_max_parallel_regions_defaults_to_one():
    descriptor = TargetDescriptor(config_branch=ConfigBranch.TARGETS, name="org")

    assert descriptor.max_parallel_regions == 1


def test_max_parallel_regions_accepts_maximum_value():
    descriptor = TargetDescriptor(
        config_branch=ConfigBranch.TARGETS, name="org", max_parallel_regions=4
    )

    assert descriptor.max_parallel_regions == 4


def test_organization_regions_accepts_all_selector():
    descriptor = TargetDescriptor(
        config_branch=ConfigBranch.TARGETS, name="org", regions=["all"]
    )

    assert descriptor.regions == ["all"]


def test_organization_regions_accepts_globs_and_explicit_regions():
    descriptor = TargetDescriptor(
        config_branch=ConfigBranch.TARGETS, name="org", regions=["us-*", "ca-central-1"]
    )

    assert descriptor.regions == ["us-*", "ca-central-1"]


def test_post_run_defaults_to_empty_list():
    descriptor = TargetDescriptor(config_branch=ConfigBranch.TARGETS, name="org")

    assert descriptor.post_run == []


def test_post_run_normalizes_processor_and_metadata():
    descriptor = TargetDescriptor(
        config_branch=ConfigBranch.TARGETS,
        name="org",
        post_run=[
            {"processor": " summary_markdown ", "metadata": {"include_passed": False}}
        ],
    )

    assert descriptor.post_run == [
        {"processor": "summary_markdown", "metadata": {"include_passed": False}}
    ]


def test_post_run_normalizes_run_on_failure():
    descriptor = TargetDescriptor(
        config_branch=ConfigBranch.TARGETS,
        name="org",
        post_run=[{"processor": "html_report", "run_on_failure": True}],
    )

    assert descriptor.post_run == [
        {"processor": "html_report", "metadata": {}, "run_on_failure": True}
    ]


def test_regions_rejects_all_mixed_with_other_regions():
    with pytest.raises(ValueError, match="'all' must be the only region value"):
        TargetDescriptor(
            config_branch=ConfigBranch.TARGETS, name="org", regions=["all", "us-east-1"]
        )


@pytest.mark.parametrize("regions", [["all"], ["us-*"]])
def test_accounts_regions_reject_selectors(regions):
    with pytest.raises(ValueError, match="selectors are not allowed"):
        TargetDescriptor(
            config_branch=ConfigBranch.TARGETS,
            name="group",
            include=["111111111111"],
            regions=regions,
        )


@pytest.mark.parametrize("max_parallel_regions", [0, 5])
def test_max_parallel_regions_rejects_out_of_range_values(max_parallel_regions):
    with pytest.raises(ValueError, match="max_parallel_regions"):
        TargetDescriptor(
            config_branch=ConfigBranch.TARGETS,
            name="org",
            max_parallel_regions=max_parallel_regions,
        )


def test_fail_fast_warns_when_combined_concurrency_is_high(caplog):
    from anvil.validators import validate_target_descriptors

    target = TargetDescriptor(
        config_branch=ConfigBranch.TARGETS,
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
        config_branch=ConfigBranch.TARGETS,
        name="org",
        max_workers=2,
        max_parallel_regions=4,
        fail_fast=True,
    )

    validate_target_descriptors(targets=[target])

    assert "combined account-region concurrency" not in caplog.text
