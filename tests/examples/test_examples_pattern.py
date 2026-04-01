from pathlib import Path

import pytest

EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples"


@pytest.mark.parametrize("config_path", sorted(EXAMPLES_DIR.glob("*.yaml")))
def test_example_configs_load(config_path: Path) -> None:
    try:
        from anvil.cli import _load_targets_from_config_file
    except PermissionError as error:
        pytest.skip(f"jsonschema package resources unavailable in test env: {error}")

    loaded_config = _load_targets_from_config_file(config_path)
    assert loaded_config.targets
