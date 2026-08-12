from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path

import pytest

from anvil.task_context import TaskCallContext


RESULT_EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples" / "Results"


@pytest.mark.parametrize("example_path", sorted(RESULT_EXAMPLES_DIR.glob("*.py")))
def test_result_task_examples_use_current_runtime_contract(example_path: Path) -> None:
    spec = importlib.util.spec_from_file_location(
        f"anvil_result_example_{example_path.stem}", example_path
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    parameters = inspect.signature(module.run).parameters

    assert frozenset(parameters) == TaskCallContext.keyword_names()
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in parameters.values()
    )
