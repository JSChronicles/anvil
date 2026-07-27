from typing import cast

from anvil.actions import ActionRecorder
from anvil.task_context import TaskCallContext


def _context(metadata: dict[str, object]) -> TaskCallContext:
    return TaskCallContext(
        provider="aws",
        execution_target_id="111111111111",
        execution_target_name="production",
        execution_target_type="account",
        region="us-east-1",
        session=object(),
        dry_run=False,
        metadata=metadata,
        actions=ActionRecorder(actions=[]),
    )


def test_task_context_keyword_names_match_runtime_kwargs():
    context = _context({})

    assert frozenset(context.to_kwargs()) == TaskCallContext.keyword_names()


def test_task_context_returns_isolated_metadata_mapping():
    metadata: dict[str, object] = {"team": "security"}
    context = _context(metadata)

    invocation_metadata = cast(dict[str, object], context.to_kwargs()["metadata"])
    invocation_metadata["team"] = "platform"

    assert metadata == {"team": "security"}
    assert context.metadata == {"team": "security"}
