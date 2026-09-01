import pytest

from anvil.filename_utils import safe_filename_component


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("org/account", "org_account"),
        ("report-2026.08_json", "report-2026.08_json"),
        ("._hidden_.", "hidden"),
        ("...", "target"),
    ],
)
def test_safe_filename_component_uses_canonical_rules(name: str, expected: str) -> None:
    assert safe_filename_component(name) == expected
