from __future__ import annotations

import pytest

from anvil.descriptors import ConfigBranch, TargetDescriptor
from anvil.providers.github import create_provider
from anvil.providers.github.provider import GithubExecutionTargetData
from anvil.results import ExecutionStatus


def _target(**overrides) -> TargetDescriptor:
    values = {
        "config_branch": ConfigBranch.TARGETS,
        "name": "github-repositories",
        "provider": "github",
        "mode": "repositories",
        "include": ["octo-org/example"],
        "provider_options": {"auth_type": "token", "token_env": "GITHUB_TOKEN"},
    }
    values.update(overrides)
    return TargetDescriptor(**values)


def test_github_provider_metadata_and_default_location():
    provider = create_provider()
    target = _target()

    assert provider.metadata.name == "github"
    assert provider.metadata.display_name == "GitHub"
    assert provider.default_regions(target) == ["global"]
    assert provider.discover_regions(target)[0].name == "global"


def test_github_provider_requires_explicit_include():
    with pytest.raises(ValueError, match="requires include"):
        _target(include=None)


def test_github_provider_rejects_exclude():
    with pytest.raises(ValueError, match="does not allow exclude|include and exclude"):
        _target(exclude=["octo-org/skip"])


def test_github_provider_rejects_unknown_auth_type():
    with pytest.raises(ValueError, match="auth_type"):
        _target(provider_options={"auth_type": "basic"})


def test_github_provider_rejects_repository_include_without_owner():
    provider = create_provider()
    target = _target(include=["example"])

    with pytest.raises(ValueError, match="owner/repo"):
        provider.validate_target(target)


def test_github_provider_rejects_organization_include_with_repo_path():
    provider = create_provider()
    target = _target(mode="organizations", include=["octo-org/example"])

    with pytest.raises(ValueError, match="organization logins"):
        provider.validate_target(target)


def test_github_provider_resolves_organization_targets_offline():
    provider = create_provider()
    target = _target(
        name="github-organizations",
        mode="organizations",
        include=["octo-org", "another-org"],
    )

    plan = provider.resolve_execution_targets(
        target=target,
        regions=["global"],
        include=target.include,
        exclude=None,
    )

    assert [(item.id, item.type, item.provider) for item in plan.execution_targets] == [
        ("octo-org", "organization", "github"),
        ("another-org", "organization", "github"),
    ]
    assert isinstance(
        plan.execution_targets[0].provider_data, GithubExecutionTargetData
    )


def test_github_provider_resolves_repository_targets_offline():
    provider = create_provider()
    target = _target(include=["octo-org/example", "octo-org/other"])

    plan = provider.resolve_execution_targets(
        target=target,
        regions=["global"],
        include=target.include,
        exclude=None,
    )

    assert [(item.id, item.type) for item in plan.execution_targets] == [
        ("octo-org/example", "repository"),
        ("octo-org/other", "repository"),
    ]


def test_github_auth_check_is_deferred_offline():
    provider = create_provider()
    result = provider.auth_check(_target())

    assert result.status is ExecutionStatus.SUCCESS
    assert result.source == "deferred"
