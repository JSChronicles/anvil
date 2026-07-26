import pytest

from anvil.descriptors import ConfigBranch, TargetDescriptor
from anvil.providers.aws.provider import AwsProvider
from anvil.providers.azure.provider import AzureProvider
from anvil.providers.gcp.provider import GcpProvider
from anvil.providers.github.provider import GithubProvider


def _aws_org(**overrides) -> TargetDescriptor:
    values = {
        "config_branch": ConfigBranch.TARGETS,
        "name": "org",
        "provider": "aws",
        "mode": "organization",
    }
    values.update(overrides)
    return TargetDescriptor(**values)


def test_duplicate_org_names():
    try:
        from anvil.validators import validate_target_descriptors
    except PermissionError as error:
        pytest.skip(f"jsonschema package resources unavailable in test env: {error}")

    targets = [_aws_org(name="a"), _aws_org(name="a")]

    with pytest.raises(ValueError):
        validate_target_descriptors(targets=targets)


def test_accounts_direct_mode_requires_single_account():
    with pytest.raises(
        ValueError, match="without role_name must include exactly one account ID"
    ):
        descriptor = TargetDescriptor(
            config_branch=ConfigBranch.TARGETS,
            name="direct-rollout",
            provider="aws",
            mode="accounts",
            include=["111111111111", "222222222222"],
        )
        AwsProvider().validate_target(descriptor)


def test_accounts_assume_role_mode_allows_multiple_accounts():
    descriptor = TargetDescriptor(
        config_branch=ConfigBranch.TARGETS,
        name="assume-role-rollout",
        provider="aws",
        mode="accounts",
        provider_options={"role_name": "OrganizationAccountAccessRole"},
        include=["111111111111", "222222222222"],
    )

    AwsProvider().validate_target(descriptor)
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
        descriptor = TargetDescriptor(
            config_branch=ConfigBranch.TARGETS,
            name="github-repositories",
            provider="github",
            mode="repositories",
        )
        GithubProvider().validate_target(descriptor)


def test_unknown_provider_is_rejected_during_component_resolution():
    from anvil.validators import validate_target_descriptors

    with pytest.raises(ValueError, match="Unknown provider"):
        validate_target_descriptors(
            targets=[
                TargetDescriptor(
                    config_branch=ConfigBranch.TARGETS,
                    name="unknown",
                    provider="do",
                    mode="custom",
                    include=["target-a"],
                )
            ]
        )


def test_invalid_provider_mode_is_rejected():
    with pytest.raises(ValueError, match="Unsupported Azure target mode"):
        descriptor = TargetDescriptor(
            config_branch=ConfigBranch.TARGETS,
            name="azure-subscriptions",
            provider="azure",
            mode="projects",
            include=["sub-a"],
        )
        AzureProvider().validate_target(descriptor)


def test_invalid_provider_options_are_rejected():
    with pytest.raises(ValueError, match="Unsupported provider.options"):
        descriptor = TargetDescriptor(
            config_branch=ConfigBranch.TARGETS,
            name="gcp-projects",
            provider="gcp",
            mode="projects",
            include=["project-a"],
            provider_options={"tenant_id": "wrong-cloud"},
        )
        GcpProvider().validate_target(descriptor)


def test_max_parallel_regions_defaults_to_one():
    descriptor = _aws_org()

    assert descriptor.max_parallel_regions == 1


def test_max_parallel_regions_accepts_maximum_value():
    descriptor = _aws_org(max_parallel_regions=4)

    assert descriptor.max_parallel_regions == 4


def test_organization_regions_accepts_all_selector():
    descriptor = _aws_org(regions=["all"])

    AwsProvider().validate_target(descriptor)
    assert descriptor.regions == ["all"]


def test_organization_regions_accepts_globs_and_explicit_regions():
    descriptor = _aws_org(regions=["us-*", "ca-central-1"])

    AwsProvider().validate_target(descriptor)
    assert descriptor.regions == ["us-*", "ca-central-1"]


def test_post_run_defaults_to_empty_list():
    descriptor = _aws_org()

    assert descriptor.post_run == []


def test_post_run_normalizes_processor_and_metadata():
    descriptor = _aws_org(
        post_run=[
            {"processor": " summary_markdown ", "metadata": {"include_passed": False}}
        ]
    )

    assert descriptor.post_run == [
        {"processor": "summary_markdown", "metadata": {"include_passed": False}}
    ]


def test_post_run_normalizes_run_on_failure():
    descriptor = _aws_org(
        post_run=[{"processor": "html_report", "run_on_failure": True}]
    )

    assert descriptor.post_run == [
        {"processor": "html_report", "metadata": {}, "run_on_failure": True}
    ]


def test_regions_rejects_all_mixed_with_other_regions():
    with pytest.raises(ValueError, match="'all' must be the only region value"):
        AwsProvider().validate_target(_aws_org(regions=["all", "us-east-1"]))


@pytest.mark.parametrize("regions", [["all"], ["us-*"]])
def test_accounts_regions_reject_selectors(regions):
    with pytest.raises(ValueError, match="selectors are not allowed"):
        descriptor = TargetDescriptor(
            config_branch=ConfigBranch.TARGETS,
            name="group",
            provider="aws",
            mode="accounts",
            include=["111111111111"],
            regions=regions,
        )
        AwsProvider().validate_target(descriptor)


@pytest.mark.parametrize(
    ("provider", "mode", "include"),
    [
        ("azure", "subscriptions", ["sub-a"]),
        ("azure", "tenant", None),
        ("gcp", "projects", ["project-a"]),
    ],
)
def test_provider_location_discovery_modes_accept_selectors(provider, mode, include):
    descriptor = TargetDescriptor(
        config_branch=ConfigBranch.TARGETS,
        name="target",
        provider=provider,
        mode=mode,
        include=include,
        regions=["us-*"],
    )

    {"azure": AzureProvider(), "gcp": GcpProvider()}[provider].validate_target(
        descriptor
    )
    assert descriptor.regions == ["us-*"]


def test_github_repository_regions_reject_selectors():
    with pytest.raises(ValueError, match="selectors are not allowed"):
        descriptor = TargetDescriptor(
            config_branch=ConfigBranch.TARGETS,
            name="github-repos",
            provider="github",
            mode="repositories",
            include=["octo-org/example"],
            regions=["all"],
        )
        GithubProvider().validate_target(descriptor)


@pytest.mark.parametrize("max_parallel_regions", [0, 5])
def test_max_parallel_regions_rejects_out_of_range_values(max_parallel_regions):
    with pytest.raises(ValueError, match="max_parallel_regions"):
        _aws_org(max_parallel_regions=max_parallel_regions)


def test_fail_fast_warns_when_combined_concurrency_is_high(caplog):
    from anvil.validators import validate_target_descriptors

    target = _aws_org(max_workers=3, max_parallel_regions=4, fail_fast=True)

    validate_target_descriptors(targets=[target])

    assert "combined account-region concurrency=12" in caplog.text


def test_fail_fast_does_not_warn_when_combined_concurrency_is_low(caplog):
    from anvil.validators import validate_target_descriptors

    target = _aws_org(max_workers=2, max_parallel_regions=4, fail_fast=True)

    validate_target_descriptors(targets=[target])

    assert "combined account-region concurrency" not in caplog.text
