from anvil.descriptors import ConfigBranch
from anvil.results import (
    EngineResult,
    EngineState,
    EntityResult,
    ExecutionStatus,
    TargetResult,
)


def _entity_result(*, entity_id: str, status: ExecutionStatus) -> EntityResult:
    return EntityResult(
        id=entity_id,
        name=f"acct-{entity_id}",
        type="account",
        status=status,
        started_at="2026-03-25T00:00:00+00:00",
        ended_at="2026-03-25T00:00:01+00:00",
        duration_seconds=1.0,
        tasks=[],
    )


def test_engine_summary_counts_interrupted_entities() -> None:
    target_result = TargetResult.create(
        config_branch=ConfigBranch.TARGETS,
        target_name="org-a",
        dry_run=True,
        entities=[
            _entity_result(entity_id="111111111111", status=ExecutionStatus.SUCCESS),
            _entity_result(
                entity_id="222222222222", status=ExecutionStatus.INTERRUPTED
            ),
            _entity_result(entity_id="333333333333", status=ExecutionStatus.ERROR),
        ],
    )
    engine_result = EngineResult(
        config_branch=ConfigBranch.TARGETS,
        state=EngineState.CANCELLED,
        generated_at="2026-03-25T00:00:00+00:00",
        auth_results=[],
        target_results=[target_result],
    )

    summary = engine_result.build_summary()

    assert summary["total_failed_entities"] == 1
    assert summary["total_interrupted_entities"] == 1
    assert summary["targets"][0]["failed_entities"] == 1
    assert summary["targets"][0]["interrupted_entities"] == 1
    assert summary["targets"][0]["has_failures"] is True
