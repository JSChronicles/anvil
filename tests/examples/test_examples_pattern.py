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
]


@pytest.mark.parametrize("config_path", CONFIG_PATHS)
def test_example_configs_load(config_path: Path) -> None:
    try:
        from anvil.cli import _load_targets_from_config_file
    except PermissionError as error:
        pytest.skip(f"jsonschema package resources unavailable in test env: {error}")

    loaded_config = _load_targets_from_config_file(config_path)
    assert loaded_config.targets


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
