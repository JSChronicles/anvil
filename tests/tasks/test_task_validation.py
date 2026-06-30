import pytest

from anvil.task_loader import ResolvedTask
from anvil.task_validation import TaskValidationError, validate_tasks
from anvil.providers.azure.tasks.count_resource_groups import (
    run as count_resource_groups,
)
from anvil.providers.gcp.tasks.get_project_info import run as get_project_info


def test_validate_tasks_accepts_valid_task():
    def run(*, account_id, account_alias, session, dry_run, metadata, actions=None):
        pass

    task = ResolvedTask(name="valid", run=run, depends_on=[], optional=False)

    validate_tasks([task])


def test_validate_tasks_accepts_var_keyword_task():
    def run(**kwargs):
        pass

    task = ResolvedTask(name="valid", run=run, depends_on=[], optional=False)

    validate_tasks([task])


def test_validate_tasks_accepts_provider_neutral_task():
    def run(
        *,
        provider,
        execution_target_id,
        execution_target_name,
        execution_target_type,
        region,
        session,
        dry_run,
        metadata,
        actions,
    ):
        pass

    task = ResolvedTask(name="valid", run=run, depends_on=[], optional=False)

    validate_tasks([task])


def test_validate_tasks_accepts_real_azure_count_resource_groups_task():
    task = ResolvedTask(
        name="count_resource_groups",
        run=count_resource_groups,
        depends_on=[],
        optional=False,
    )

    validate_tasks([task])


def test_validate_tasks_accepts_real_gcp_get_project_info_task():
    task = ResolvedTask(
        name="get_project_info",
        run=get_project_info,
        depends_on=[],
        optional=False,
    )

    validate_tasks([task])


def test_validate_tasks_rejects_task_missing_actions():
    def run(*, account_id, account_alias, session, dry_run, metadata):
        pass

    task = ResolvedTask(name="missing-actions", run=run, depends_on=[], optional=False)

    with pytest.raises(TaskValidationError):
        validate_tasks([task])


def test_validate_tasks_rejects_bad_signature():
    def run(account_id):  # missing required kwargs
        pass

    task = ResolvedTask(name="bad", run=run, depends_on=[], optional=False)

    with pytest.raises(TaskValidationError):
        validate_tasks([task])


def test_validate_tasks_rejects_duplicate_names():
    def run(*, account_id, account_alias, session, dry_run, metadata, actions=None):
        pass

    tasks = [
        ResolvedTask("dup", run, depends_on=[], optional=False),
        ResolvedTask("dup", run, depends_on=[], optional=False),
    ]

    with pytest.raises(TaskValidationError):
        validate_tasks(tasks)
