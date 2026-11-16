from __future__ import annotations

import builtins
import sys

from anvil import task_loader
from anvil.task_validation import task_validation_errors


def test_aws_tasks_validate_without_aws_sdk_imports(monkeypatch):
    """Keep structural task validation independent of the optional AWS SDK."""

    task_package_prefix = "anvil.providers.aws.tasks."
    for module_name in list(sys.modules):
        if module_name.startswith(task_package_prefix):
            monkeypatch.delitem(sys.modules, module_name)

    original_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name in {"boto3", "botocore"} or name.startswith(("boto3.", "botocore.")):
            raise AssertionError("offline task validation must not import the AWS SDK")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    task_loader._clear_task_caches()
    descriptors = list(task_loader._provider_specific_task_catalog("aws").descriptors)

    assert descriptors
    assert task_validation_errors(descriptors) == []
