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


def test_load_config_descriptors_reads_max_parallel_regions():
    validators = _import_validators_or_skip()

    loaded = validators.load_config_descriptors(
        config={
            "schema_version": 1,
            "accounts": [
                {
                    "name": "group-a",
                    "include": ["111111111111"],
                    "max_parallel_regions": 4,
                }
            ],
        }
    )

    assert loaded.targets[0].max_parallel_regions == 4


def test_load_config_descriptors_reads_post_run_processors():
    validators = _import_validators_or_skip()

    loaded = validators.load_config_descriptors(
        config={
            "schema_version": 1,
            "organizations": [
                {
                    "name": "org-a",
                    "post_run": [
                        {
                            "processor": "security_summary",
                            "output": "reports/security.md",
                            "metadata": {"severity_threshold": "medium"},
                        }
                    ],
                }
            ],
        }
    )

    assert loaded.targets[0].post_run == [
        {
            "processor": "security_summary",
            "output": "reports/security.md",
            "metadata": {"severity_threshold": "medium"},
        }
    ]


def test_load_config_descriptors_defaults_legacy_aws_provider_and_mode():
    validators = _import_validators_or_skip()

    loaded = validators.load_config_descriptors(
        config={
            "schema_version": 1,
            "accounts": [{"name": "group-a", "include": ["111111111111"]}],
        }
    )

    assert loaded.targets[0].provider == "aws"
    assert loaded.targets[0].mode == "accounts"


def test_load_config_descriptors_keeps_legacy_organization_fields():
    validators = _import_validators_or_skip()

    loaded = validators.load_config_descriptors(
        config={
            "schema_version": 1,
            "organizations": [
                {
                    "name": "org-a",
                    "profile": "security",
                    "role_name": "AuditRole",
                    "include": ["111111111111"],
                    "exclude": None,
                }
            ],
        }
    )

    target = loaded.targets[0]
    assert target.provider == "aws"
    assert target.mode == "organization"
    assert target.profile == "security"
    assert target.role_name == "AuditRole"
    assert target.include == ["111111111111"]
    assert target.exclude is None


def test_load_config_descriptors_keeps_legacy_account_fields():
    validators = _import_validators_or_skip()

    loaded = validators.load_config_descriptors(
        config={
            "schema_version": 1,
            "accounts": [
                {
                    "name": "group-a",
                    "profile": "security",
                    "role_name": "AuditRole",
                    "include": ["111111111111", "222222222222"],
                }
            ],
        }
    )

    target = loaded.targets[0]
    assert target.provider == "aws"
    assert target.mode == "accounts"
    assert target.profile == "security"
    assert target.role_name == "AuditRole"
    assert target.include == ["111111111111", "222222222222"]


def test_load_config_descriptors_reads_aws_provider_options_aliases():
    validators = _import_validators_or_skip()

    loaded = validators.load_config_descriptors(
        config={
            "schema_version": 1,
            "accounts": [
                {
                    "name": "group-a",
                    "include": ["111111111111", "222222222222"],
                    "provider_options": {
                        "profile": "security",
                        "role_name": "AuditRole",
                    },
                }
            ],
        }
    )

    assert loaded.targets[0].provider == "aws"
    assert loaded.targets[0].profile == "security"
    assert loaded.targets[0].role_name == "AuditRole"


def test_load_config_descriptors_accepts_matching_aws_alias_values():
    validators = _import_validators_or_skip()

    loaded = validators.load_config_descriptors(
        config={
            "schema_version": 1,
            "accounts": [
                {
                    "name": "group-a",
                    "profile": "security",
                    "role_name": "AuditRole",
                    "include": ["111111111111", "222222222222"],
                    "provider_options": {
                        "profile": "security",
                        "role_name": "AuditRole",
                    },
                }
            ],
        }
    )

    target = loaded.targets[0]
    assert target.profile == "security"
    assert target.role_name == "AuditRole"


def test_load_config_descriptors_reads_azure_subscription_targets():
    validators = _import_validators_or_skip()

    loaded = validators.load_config_descriptors(
        config={
            "schema_version": 1,
            "accounts": [
                {
                    "name": "azure-subscriptions",
                    "provider": "azure",
                    "mode": "subscriptions",
                    "regions": ["eastus"],
                    "include": [
                        "11111111-2222-3333-4444-555555555555",
                        "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                    ],
                    "provider_options": {"tenant_id": "tenant-a"},
                }
            ],
        }
    )

    target = loaded.targets[0]
    assert target.provider == "azure"
    assert target.mode == "subscriptions"
    assert target.include == [
        "11111111-2222-3333-4444-555555555555",
        "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    ]
    assert target.provider_options == {"tenant_id": "tenant-a"}


def test_load_config_descriptors_reads_gcp_project_targets():
    validators = _import_validators_or_skip()

    loaded = validators.load_config_descriptors(
        config={
            "schema_version": 1,
            "accounts": [
                {
                    "name": "gcp-projects",
                    "provider": "gcp",
                    "mode": "projects",
                    "regions": ["us-central1"],
                    "include": ["project-a", "project-b"],
                    "provider_options": {"quota_project_id": "billing-project"},
                }
            ],
        }
    )

    target = loaded.targets[0]
    assert target.provider == "gcp"
    assert target.mode == "projects"
    assert target.include == ["project-a", "project-b"]
    assert target.provider_options == {"quota_project_id": "billing-project"}


@pytest.mark.parametrize(
    ("provider", "mode", "include", "field_name"),
    [
        (
            "azure",
            "subscriptions",
            ["11111111-2222-3333-4444-555555555555"],
            "profile",
        ),
        (
            "azure",
            "subscriptions",
            ["11111111-2222-3333-4444-555555555555"],
            "role_name",
        ),
        ("gcp", "projects", ["project-a"], "profile"),
        ("gcp", "projects", ["project-a"], "role_name"),
    ],
)
def test_validate_config_schema_rejects_non_aws_top_level_aws_fields(
    provider, mode, include, field_name
):
    validators = _import_validators_or_skip()

    with pytest.raises(ValueError, match=field_name):
        validators.validate_config_schema(
            config={
                "schema_version": 1,
                "accounts": [
                    {
                        "name": f"{provider}-targets",
                        "provider": provider,
                        "mode": mode,
                        "include": include,
                        field_name: "legacy-aws-value",
                    }
                ],
            }
        )


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


def test_validate_config_schema_accepts_max_parallel_regions_for_organizations():
    validators = _import_validators_or_skip()

    validators.validate_config_schema(
        config={
            "schema_version": 1,
            "organizations": [{"name": "org-a", "max_parallel_regions": 4}],
        }
    )


def test_validate_config_schema_accepts_max_parallel_regions_for_accounts():
    validators = _import_validators_or_skip()

    validators.validate_config_schema(
        config={
            "schema_version": 1,
            "accounts": [
                {
                    "name": "group-a",
                    "include": ["111111111111"],
                    "max_parallel_regions": 2,
                }
            ],
        }
    )


def test_validate_config_schema_rejects_assume_role_in_management_for_organizations():
    validators = _import_validators_or_skip()

    with pytest.raises(ValueError, match="assume_role_in_management"):
        validators.validate_config_schema(
            config={
                "schema_version": 1,
                "organizations": [{"name": "org-a", "assume_role_in_management": True}],
            }
        )


def test_validate_config_schema_rejects_assume_role_in_management_for_accounts():
    validators = _import_validators_or_skip()

    with pytest.raises(ValueError, match="assume_role_in_management"):
        validators.validate_config_schema(
            config={
                "schema_version": 1,
                "accounts": [
                    {
                        "name": "group-a",
                        "include": ["111111111111"],
                        "assume_role_in_management": True,
                    }
                ],
            }
        )


def test_validate_config_schema_accepts_post_run_for_organizations():
    validators = _import_validators_or_skip()

    validators.validate_config_schema(
        config={
            "schema_version": 1,
            "organizations": [
                {
                    "name": "org-a",
                    "post_run": [
                        {
                            "processor": "summary_markdown",
                            "output": "reports/summary.md",
                            "metadata": {"include_passed": False},
                        }
                    ],
                }
            ],
        }
    )


def test_validate_config_schema_accepts_post_run_run_on_failure():
    validators = _import_validators_or_skip()

    validators.validate_config_schema(
        config={
            "schema_version": 1,
            "organizations": [
                {
                    "name": "org-a",
                    "post_run": [
                        {
                            "processor": "html_report",
                            "output": "reports/status.html",
                            "run_on_failure": True,
                        }
                    ],
                }
            ],
        }
    )


def test_validate_config_schema_accepts_post_run_for_accounts():
    validators = _import_validators_or_skip()

    validators.validate_config_schema(
        config={
            "schema_version": 1,
            "accounts": [
                {
                    "name": "group-a",
                    "include": ["111111111111"],
                    "post_run": [{"processor": "summary_json"}],
                }
            ],
        }
    )


def test_validate_config_schema_accepts_azure_subscription_ids():
    validators = _import_validators_or_skip()

    validators.validate_config_schema(
        config={
            "schema_version": 1,
            "accounts": [
                {
                    "name": "azure-subscriptions",
                    "provider": "azure",
                    "mode": "subscriptions",
                    "include": ["11111111-2222-3333-4444-555555555555"],
                }
            ],
        }
    )


def test_validate_config_schema_accepts_gcp_project_ids():
    validators = _import_validators_or_skip()

    validators.validate_config_schema(
        config={
            "schema_version": 1,
            "accounts": [
                {
                    "name": "gcp-projects",
                    "provider": "gcp",
                    "mode": "projects",
                    "include": ["anvil-dev-project"],
                }
            ],
        }
    )


def test_validate_config_schema_keeps_legacy_aws_account_id_pattern():
    validators = _import_validators_or_skip()

    with pytest.raises(ValueError, match="include"):
        validators.validate_config_schema(
            config={
                "schema_version": 1,
                "accounts": [{"name": "group-a", "include": ["not-an-account-id"]}],
            }
        )


def test_validate_config_schema_rejects_invalid_provider_mode():
    validators = _import_validators_or_skip()

    with pytest.raises(ValueError, match="mode"):
        validators.validate_config_schema(
            config={
                "schema_version": 1,
                "accounts": [
                    {
                        "name": "azure-subscriptions",
                        "provider": "azure",
                        "mode": "projects",
                        "include": ["sub-a"],
                    }
                ],
            }
        )


@pytest.mark.parametrize(
    ("provider", "mode"),
    [
        ("azure", "management_groups"),
        ("gcp", "organizations"),
        ("gcp", "folders"),
    ],
)
def test_validate_config_schema_rejects_unimplemented_provider_discovery_modes(
    provider,
    mode,
):
    validators = _import_validators_or_skip()

    with pytest.raises(ValueError, match="mode"):
        validators.validate_config_schema(
            config={
                "schema_version": 1,
                "accounts": [
                    {
                        "name": f"{provider}-{mode}",
                        "provider": provider,
                        "mode": mode,
                        "include": ["target-a"],
                    }
                ],
            }
        )


def test_validate_config_schema_rejects_invalid_provider_option():
    validators = _import_validators_or_skip()

    with pytest.raises(ValueError, match="provider_options"):
        validators.validate_config_schema(
            config={
                "schema_version": 1,
                "accounts": [
                    {
                        "name": "gcp-projects",
                        "provider": "gcp",
                        "mode": "projects",
                        "include": ["project-a"],
                        "provider_options": {"tenant_id": "wrong-cloud"},
                    }
                ],
            }
        )


def test_validate_config_schema_reuses_cached_branch_schema(monkeypatch):
    validators = _import_validators_or_skip()
    validators._load_branch_schema.cache_clear()

    load_calls: list[str] = []
    original_load_schema_file = validators._load_schema_file

    def recording_load_schema_file(schema_file: str):
        load_calls.append(schema_file)
        return original_load_schema_file(schema_file)

    monkeypatch.setattr(validators, "_load_schema_file", recording_load_schema_file)

    config = {"schema_version": 1, "organizations": [{"name": "org-a"}]}
    validators.validate_config_schema(config=config)
    validators.validate_config_schema(config=config)

    assert load_calls.count("orgs.schema.v1.json") == 1


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


def test_validate_config_schema_rejects_invalid_post_run_output():
    validators = _import_validators_or_skip()

    with pytest.raises(ValueError, match="post_run"):
        validators.validate_config_schema(
            config={
                "schema_version": 1,
                "organizations": [
                    {
                        "name": "org-a",
                        "post_run": [{"processor": "summary_json", "output": False}],
                    }
                ],
            }
        )


def test_validate_config_schema_rejects_invalid_post_run_run_on_failure():
    validators = _import_validators_or_skip()

    with pytest.raises(ValueError, match="post_run"):
        validators.validate_config_schema(
            config={
                "schema_version": 1,
                "organizations": [
                    {
                        "name": "org-a",
                        "post_run": [
                            {"processor": "html_report", "run_on_failure": "yes"}
                        ],
                    }
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
                "schema_version": 1,
                "organizations": [
                    {"name": "org-a", "max_parallel_regions": max_parallel_regions}
                ],
            }
        )
