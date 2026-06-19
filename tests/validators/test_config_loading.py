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
                            {
                                "processor": "html_report",
                                "run_on_failure": "yes",
                            }
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
