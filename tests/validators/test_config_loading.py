import pytest


def _import_validators_or_skip():
    try:
        from anvil import validators
    except PermissionError as error:
        pytest.skip(f"jsonschema package resources unavailable in test env: {error}")

    return validators


def _v2_target(*, provider_name: str, mode: str, **overrides):
    target = {
        "name": f"{provider_name}-{mode}",
        "provider": {"name": provider_name, "mode": mode, "options": {}},
        "regions": [
            "global"
            if provider_name in {"gcp", "github"}
            else "eastus"
            if provider_name == "azure"
            else "us-east-1"
        ],
        "tasks": [{"name": "noop"}],
    }
    target.update(overrides)
    return target


@pytest.mark.parametrize(
    "config",
    [
        {"schema_version": 1, "organizations": [{"name": "org-a"}]},
        {
            "schema_version": 1,
            "accounts": [{"name": "group-a", "include": ["111111111111"]}],
        },
        {"organizations": [{"name": "org-a"}]},
        {"accounts": [{"name": "group-a", "include": ["111111111111"]}]},
    ],
)
def test_v1_and_legacy_top_level_branches_are_unsupported(config):
    validators = _import_validators_or_skip()

    with pytest.raises(ValueError, match="schema_version: 2.*targets"):
        validators.validate_config_schema(config=config)

    with pytest.raises(ValueError, match="schema_version: 2.*targets"):
        validators.load_config_descriptors(config=config)


@pytest.mark.parametrize("legacy_branch", ["organizations", "accounts"])
def test_v2_rejects_legacy_top_level_branches(legacy_branch):
    validators = _import_validators_or_skip()

    with pytest.raises(
        ValueError, match="top-level 'targets'.*organizations.*accounts"
    ):
        validators.validate_config_schema(
            config={
                "schema_version": 2,
                "targets": [_v2_target(provider_name="aws", mode="organization")],
                legacy_branch: [{"name": "legacy"}],
            }
        )


def test_load_config_descriptors_reads_v2_run_controls_and_post_run():
    validators = _import_validators_or_skip()

    loaded = validators.load_config_descriptors(
        config={
            "schema_version": 2,
            "max_parallel_targets": 3,
            "targets": [
                _v2_target(
                    provider_name="aws",
                    mode="organization",
                    max_parallel_regions=4,
                    post_run=[
                        {
                            "processor": "security_summary",
                            "output": "reports/security.md",
                            "metadata": {"severity_threshold": "medium"},
                        }
                    ],
                )
            ],
        }
    )

    assert loaded.max_parallel_targets == 3
    assert loaded.targets[0].max_parallel_regions == 4
    assert loaded.targets[0].post_run == [
        {
            "processor": "security_summary",
            "output": "reports/security.md",
            "metadata": {"severity_threshold": "medium"},
        }
    ]


def test_v2_schema_accepts_final_multicloud_target_shapes():
    validators = _import_validators_or_skip()

    config = {
        "schema_version": 2,
        "targets": [
            _v2_target(
                provider_name="aws",
                mode="organization",
                name="aws-org",
                provider={
                    "name": "aws",
                    "mode": "organization",
                    "options": {
                        "profile": "prod",
                        "role_name": "OrganizationAccountAccessRole",
                    },
                },
                include=["111111111111"],
                tasks=[{"name": "count_vpc"}],
            ),
            _v2_target(
                provider_name="aws",
                mode="accounts",
                name="aws-accounts",
                provider={
                    "name": "aws",
                    "mode": "accounts",
                    "options": {"profile": "prod", "role_name": "AuditRole"},
                },
                include=["111111111111"],
                tasks=[{"name": "count_vpc"}],
            ),
            _v2_target(
                provider_name="azure",
                mode="tenant",
                name="azure-tenant",
                provider={
                    "name": "azure",
                    "mode": "tenant",
                    "options": {
                        "tenant_id": "${AZURE_TENANT_ID}",
                        "client_id": "${AZURE_CLIENT_ID}",
                        "client_secret": "${AZURE_CLIENT_SECRET}",
                    },
                },
                exclude=["00000000-0000-0000-0000-000000000000"],
                tasks=[{"name": "count_resource_groups"}],
            ),
            _v2_target(
                provider_name="azure",
                mode="subscriptions",
                name="azure-subscriptions",
                include=["00000000-0000-0000-0000-000000000000"],
                tasks=[{"name": "count_resource_groups"}],
            ),
            _v2_target(
                provider_name="gcp",
                mode="organization",
                name="gcp-org",
                provider={
                    "name": "gcp",
                    "mode": "organization",
                    "options": {
                        "organization_id": "123456789012",
                        "quota_project_id": "billing-project",
                    },
                },
                include=["my-project"],
                tasks=[{"name": "get_project_info"}],
            ),
            _v2_target(
                provider_name="gcp",
                mode="projects",
                name="gcp-projects",
                provider={
                    "name": "gcp",
                    "mode": "projects",
                    "options": {"credentials_path": "./gcp.json"},
                },
                include=["my-project"],
                tasks=[{"name": "get_project_info"}],
            ),
            _v2_target(
                provider_name="github",
                mode="organizations",
                name="github-organizations",
                provider={
                    "name": "github",
                    "mode": "organizations",
                    "options": {
                        "api_url": "https://api.github.com",
                        "api_version": "2022-11-28",
                        "token_env": "GITHUB_TOKEN",
                    },
                },
                include=["octo-org"],
            ),
            _v2_target(
                provider_name="github",
                mode="repositories",
                name="github-repositories",
                provider={
                    "name": "github",
                    "mode": "repositories",
                    "options": {
                        "app_id": "12345",
                        "private_key_env": "GITHUB_PRIVATE_KEY",
                    },
                },
                include=["octo-org/example"],
            ),
        ],
    }

    validators.validate_config_schema(config=config)
    loaded = validators.load_config_descriptors(config=config)

    assert loaded.branch.value == "targets"
    assert [(target.provider, target.mode) for target in loaded.targets] == [
        ("aws", "organization"),
        ("aws", "accounts"),
        ("azure", "tenant"),
        ("azure", "subscriptions"),
        ("gcp", "organization"),
        ("gcp", "projects"),
        ("github", "organizations"),
        ("github", "repositories"),
    ]
    assert loaded.targets[3].provider_options == {}
    assert loaded.targets[5].provider_options == {"credentials_path": "./gcp.json"}
    assert loaded.targets[6].provider_options["token_env"] == "GITHUB_TOKEN"
    assert loaded.targets[7].provider_options["app_id"] == "12345"


@pytest.mark.parametrize("provider", [{}, {"mode": "accounts"}, {"name": "aws"}])
def test_v2_rejects_provider_missing_name_or_mode(provider):
    validators = _import_validators_or_skip()

    with pytest.raises(ValueError, match="provider"):
        validators.validate_config_schema(
            config={
                "schema_version": 2,
                "targets": [{"name": "bad", "provider": provider}],
            }
        )


@pytest.mark.parametrize(
    ("provider", "mode"),
    [
        ("aws", "organization"),
        ("azure", "tenant"),
        ("gcp", "organization"),
        ("github", "organizations"),
        ("aws", "accounts"),
        ("azure", "subscriptions"),
        ("gcp", "projects"),
        ("github", "repositories"),
    ],
)
def test_v2_rejects_include_and_exclude_together_for_all_modes(provider, mode):
    validators = _import_validators_or_skip()

    with pytest.raises(ValueError, match="include|exclude"):
        validators.validate_config_schema(
            config={
                "schema_version": 2,
                "targets": [
                    _v2_target(
                        provider_name=provider,
                        mode=mode,
                        include=["111111111111" if provider == "aws" else "target-a"],
                        exclude=["222222222222" if provider == "aws" else "target-b"],
                    )
                ],
            }
        )


@pytest.mark.parametrize(
    ("provider", "mode"),
    [("aws", "organization"), ("azure", "tenant"), ("gcp", "organization")],
)
def test_v2_discovery_modes_allow_omitted_include(provider, mode):
    validators = _import_validators_or_skip()

    config = {
        "schema_version": 2,
        "targets": [_v2_target(provider_name=provider, mode=mode)],
    }

    validators.validate_config_schema(config=config)
    loaded = validators.load_config_descriptors(config=config)

    assert loaded.targets[0].include is None
    assert loaded.targets[0].exclude is None


@pytest.mark.parametrize(
    ("provider", "mode"),
    [
        ("aws", "accounts"),
        ("azure", "subscriptions"),
        ("gcp", "projects"),
        ("github", "organizations"),
        ("github", "repositories"),
    ],
)
def test_v2_explicit_modes_require_include_and_reject_exclude(provider, mode):
    validators = _import_validators_or_skip()

    with pytest.raises(ValueError, match="include"):
        validators.validate_config_schema(
            config={
                "schema_version": 2,
                "targets": [_v2_target(provider_name=provider, mode=mode)],
            }
        )

    with pytest.raises(ValueError, match="exclude"):
        validators.validate_config_schema(
            config={
                "schema_version": 2,
                "targets": [
                    _v2_target(
                        provider_name=provider,
                        mode=mode,
                        include=["111111111111" if provider == "aws" else "target-a"],
                        exclude=["222222222222" if provider == "aws" else "target-b"],
                    )
                ],
            }
        )


@pytest.mark.parametrize(
    ("mode", "include"),
    [
        ("organizations", ["octo-org/example"]),
        ("repositories", ["example"]),
    ],
)
def test_v2_github_modes_validate_include_shape(mode, include):
    validators = _import_validators_or_skip()

    with pytest.raises(ValueError, match="include"):
        validators.validate_config_schema(
            config={
                "schema_version": 2,
                "targets": [
                    _v2_target(
                        provider_name="github",
                        mode=mode,
                        include=include,
                    )
                ],
            }
        )


@pytest.mark.parametrize("field_name", ["provider_options", "profile", "role_name"])
def test_v2_rejects_legacy_public_provider_fields(field_name):
    validators = _import_validators_or_skip()
    target = _v2_target(provider_name="aws", mode="accounts", include=["111111111111"])
    target[field_name] = (
        {"role_name": "AuditRole"} if field_name == "provider_options" else "legacy"
    )

    with pytest.raises(ValueError, match=field_name):
        validators.validate_config_schema(
            config={"schema_version": 2, "targets": [target]}
        )


def test_validate_config_schema_reuses_cached_targets_schema(monkeypatch):
    validators = _import_validators_or_skip()
    validators._load_targets_schema.cache_clear()

    load_calls: list[str] = []
    original_load_schema_file = validators._load_schema_file

    def recording_load_schema_file(schema_file: str):
        load_calls.append(schema_file)
        return original_load_schema_file(schema_file)

    monkeypatch.setattr(validators, "_load_schema_file", recording_load_schema_file)

    config = {
        "schema_version": 2,
        "targets": [_v2_target(provider_name="aws", mode="organization")],
    }
    validators.validate_config_schema(config=config)
    validators.validate_config_schema(config=config)

    assert load_calls.count("targets.schema.v2.json") == 1


def test_validate_config_schema_rejects_invalid_max_parallel_targets():
    validators = _import_validators_or_skip()

    with pytest.raises(ValueError, match="max_parallel_targets"):
        validators.validate_config_schema(
            config={
                "schema_version": 2,
                "max_parallel_targets": 0,
                "targets": [_v2_target(provider_name="aws", mode="organization")],
            }
        )


def test_validate_config_schema_rejects_invalid_post_run_output():
    validators = _import_validators_or_skip()

    with pytest.raises(ValueError, match="post_run"):
        validators.validate_config_schema(
            config={
                "schema_version": 2,
                "targets": [
                    _v2_target(
                        provider_name="aws",
                        mode="organization",
                        post_run=[{"processor": "summary_json", "output": False}],
                    )
                ],
            }
        )


@pytest.mark.parametrize("max_parallel_regions", [True, 0, 5])
def test_validate_config_schema_rejects_invalid_max_parallel_regions(
    max_parallel_regions,
):
    validators = _import_validators_or_skip()

    with pytest.raises(ValueError, match="max_parallel_regions"):
        validators.validate_config_schema(
            config={
                "schema_version": 2,
                "targets": [
                    _v2_target(
                        provider_name="aws",
                        mode="organization",
                        max_parallel_regions=max_parallel_regions,
                    )
                ],
            }
        )
