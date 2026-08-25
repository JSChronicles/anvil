from pathlib import Path

import pytest

EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples"
CONFIG_DIR = Path(__file__).resolve().parents[2] / "yaml"
CONFIG_PATHS = sorted([*EXAMPLES_DIR.glob("*.yaml"), *CONFIG_DIR.glob("*.yaml")])
INVALID_MULTI_ACCOUNT_CONFIG = (
    EXAMPLES_DIR / "invalid" / "aws-configured-target-multiple-accounts.yaml"
)
ADVANCED_CONFIG_PATHS = [
    EXAMPLES_DIR / "04-aws-advanced.yaml",
    EXAMPLES_DIR / "08-azure-advanced.yaml",
    EXAMPLES_DIR / "12-gcp-advanced.yaml",
    EXAMPLES_DIR / "16-github-advanced.yaml",
    EXAMPLES_DIR / "20-cloudflare-advanced.yaml",
    EXAMPLES_DIR / "24-datadog-advanced.yaml",
    EXAMPLES_DIR / "28-gitlab-advanced.yaml",
    EXAMPLES_DIR / "32-pagerduty-advanced.yaml",
]
NEW_PROVIDER_EXAMPLE_PREFIXES = {
    "cloudflare": range(17, 21),
    "datadog": range(21, 25),
    "gitlab": range(25, 29),
    "pagerduty": range(29, 33),
}
NEW_PROVIDER_CONFIG_PATHS = [
    path
    for provider in NEW_PROVIDER_EXAMPLE_PREFIXES
    for path in sorted(EXAMPLES_DIR.glob(f"*-{provider}-*.yaml"))
]


@pytest.mark.parametrize("config_path", CONFIG_PATHS)
def test_example_configs_load(config_path: Path) -> None:
    try:
        from anvil.cli import _load_targets_from_config_file
    except PermissionError as error:
        pytest.skip(f"jsonschema package resources unavailable in test env: {error}")

    loaded_config = _load_targets_from_config_file(config_path)
    assert loaded_config.targets


def test_each_new_provider_has_four_numbered_examples() -> None:
    for provider, numbers in NEW_PROVIDER_EXAMPLE_PREFIXES.items():
        matching_paths = [
            path
            for path in EXAMPLES_DIR.glob("*.yaml")
            if path.name.split("-", maxsplit=2)[1] == provider
        ]

        assert sorted(int(path.name[:2]) for path in matching_paths) == list(numbers)


@pytest.mark.parametrize("config_path", NEW_PROVIDER_CONFIG_PATHS)
def test_new_provider_example_tasks_resolve(config_path: Path) -> None:
    from anvil.cli import _load_targets_from_config_file
    from anvil.provider_loader import load_provider
    from anvil.task_loader import resolve_tasks

    loaded_config = _load_targets_from_config_file(config_path)

    for target in loaded_config.targets:
        provider = load_provider(target.provider)
        execution = resolve_tasks(
            task_specs=target.tasks,
            provider_name=target.provider,
            supported_task_scopes=provider.metadata.supported_task_scopes,
        )
        assert execution.ordered


def test_invalid_multi_account_configured_target_example_fails_offline() -> None:
    from anvil.cli import _load_targets_from_config_file
    from anvil.providers.aws.provider import AwsProvider

    loaded_config = _load_targets_from_config_file(INVALID_MULTI_ACCOUNT_CONFIG)

    with pytest.raises(ValueError, match="exactly one explicit account"):
        AwsProvider().validate_task_configuration(
            target=loaded_config.targets[0],
            task_scopes={"snapshot_org_config": "configured_target"},
        )


@pytest.mark.parametrize("config_path", ADVANCED_CONFIG_PATHS)
def test_advanced_examples_use_descriptive_task_ids(config_path: Path) -> None:
    from anvil.cli import _load_targets_from_config_file

    loaded_config = _load_targets_from_config_file(config_path)

    assert all(
        isinstance(task.get("id"), str) and task["id"]
        for target in loaded_config.targets
        for task in target.tasks
    )


def test_aws_advanced_sarif_example_avoids_duplicate_lambda_inventory() -> None:
    from anvil.cli import _load_targets_from_config_file

    loaded_config = _load_targets_from_config_file(
        EXAMPLES_DIR / "04-aws-advanced.yaml"
    )
    task_names = {task["name"] for task in loaded_config.targets[0].tasks}

    assert "detect_deprecated_lambda_runtimes" in task_names
    assert "list_lambdas_by_runtime" not in task_names
