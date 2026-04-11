import pytest

from anvil.task_loader import TaskDescriptor
from anvil.task_validation import TaskValidationError, validate_tasks


def test_validate_tasks_accepts_valid_task():
    def run(*, account_id, account_alias, session, dry_run, metadata, actions=None):
        pass

    task = TaskDescriptor(name="valid", run=run, source="stock")

    validate_tasks([task])


def test_validate_tasks_accepts_var_keyword_task():
    def run(**kwargs):
        pass

    task = TaskDescriptor(name="valid", run=run, source="stock")

    validate_tasks([task])


def test_validate_tasks_rejects_task_missing_actions():
    def run(*, account_id, account_alias, session, dry_run, metadata):
        pass

    task = TaskDescriptor(name="missing-actions", run=run, source="stock")

    with pytest.raises(TaskValidationError):
        validate_tasks([task])


def test_validate_tasks_rejects_bad_signature():
    def run(account_id):  # missing required kwargs
        pass

    task = TaskDescriptor(name="bad", run=run, source="stock")

    with pytest.raises(TaskValidationError):
        validate_tasks([task])


def test_validate_tasks_rejects_duplicate_names():
    def run(*, account_id, account_alias, session, dry_run, metadata, actions=None):
        pass

    tasks = [TaskDescriptor("dup", run, "stock"), TaskDescriptor("dup", run, "plugin")]

    with pytest.raises(TaskValidationError):
        validate_tasks(tasks)
