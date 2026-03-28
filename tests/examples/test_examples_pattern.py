from pathlib import Path

import pytest

from anvil.cli import _load_orgs_from_file

EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples"


@pytest.mark.parametrize("config_path", sorted(EXAMPLES_DIR.glob("*.yaml")))
def test_example_configs_load(config_path: Path) -> None:
    orgs = _load_orgs_from_file(config_path)
    assert orgs
