from types import MappingProxyType

import pytest

from anvil.providers.tasks._task_helpers import (
    metadata_bool,
    metadata_int,
    metadata_string,
    metadata_string_array,
)


def test_metadata_helpers_accept_read_only_mappings_and_normalize_values() -> None:
    metadata = MappingProxyType(
        {
            "enabled": True,
            "maximum": 5,
            "name": "  example  ",
            "users": [" alice ", "bob", "alice"],
        }
    )

    assert metadata_bool(
        task_name="example", metadata=metadata, key="enabled", default=False
    )
    assert (
        metadata_int(task_name="example", metadata=metadata, key="maximum", default=10)
        == 5
    )
    assert (
        metadata_string(
            task_name="example", metadata=metadata, key="name", required=True
        )
        == "example"
    )
    assert metadata_string_array(
        task_name="example", metadata=metadata, key="users", required=True
    ) == ["alice", "bob"]


@pytest.mark.parametrize("value", [True, 0, 11, "5"])
def test_metadata_int_rejects_boolean_type_and_out_of_bounds(value: object) -> None:
    with pytest.raises(RuntimeError, match=r"example metadata\.maximum"):
        metadata_int(
            task_name="example",
            metadata={"maximum": value},
            key="maximum",
            default=5,
            minimum=1,
            maximum=10,
        )


def test_required_metadata_helpers_reject_missing_values() -> None:
    with pytest.raises(RuntimeError, match=r"metadata\.name"):
        metadata_string(task_name="example", metadata={}, key="name", required=True)
    with pytest.raises(RuntimeError, match=r"metadata\.users"):
        metadata_string_array(
            task_name="example", metadata={}, key="users", required=True
        )


def test_metadata_bool_rejects_non_boolean_values() -> None:
    with pytest.raises(RuntimeError, match=r"example metadata\.enabled"):
        metadata_bool(
            task_name="example", metadata={"enabled": 1}, key="enabled", default=False
        )
