from collections.abc import Callable

import pytest

from anvil._components import ComponentCatalog, ComponentOrigin, ComponentSource
from anvil.providers.azure.tasks.count_resource_groups import (
    run as count_resource_groups,
)
from anvil.providers.gcp.tasks.get_project_info import run as get_project_info
from anvil.task_loader import TaskDescriptor
from anvil.task_validation import (
    TaskValidationError,
    task_catalog_ambiguity_errors,
    validate_tasks,
)


def _task(
    name: str,
    run: Callable,
    *,
    source_label: str = "stock",
    provider: str | None = None,
) -> TaskDescriptor:
    return TaskDescriptor(
        name=name,
        source=ComponentSource(
            origin=ComponentOrigin.STOCK,
            package="tests.tasks",
            label=source_label,
            provider=provider,
        ),
        load=lambda: run,
    )


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
        dependency_data,
        actions,
    ):
        """Run a valid provider-neutral task."""

        pass

    validate_tasks([_task("valid", run)])


@pytest.mark.parametrize(
    ("name", "run"),
    [
        ("count_resource_groups", count_resource_groups),
        ("get_project_info", get_project_info),
    ],
)
def test_validate_tasks_accepts_real_provider_tasks(name, run):
    validate_tasks([_task(name, run)])


def test_validate_tasks_rejects_legacy_signature_without_dependency_data():
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
        """Run a task using the removed pre-Phase-2 signature."""

        pass

    with pytest.raises(TaskValidationError, match="dependency_data"):
        validate_tasks([_task("legacy-signature", run)])


def test_validate_tasks_rejects_task_missing_actions():
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
        dependency_data,
    ):
        """Run an invalid task."""

        pass

    with pytest.raises(TaskValidationError, match="actions"):
        validate_tasks([_task("missing-actions", run)])


def test_validate_tasks_rejects_bad_signature():
    def run(account_id):
        """Run an invalid task."""

        pass

    with pytest.raises(TaskValidationError):
        validate_tasks([_task("bad", run)])


def test_validate_tasks_rejects_additional_required_parameter():
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
        dependency_data,
        actions,
        extra,
    ):
        """Run an invalid task with an unsupplied parameter."""

        pass

    with pytest.raises(TaskValidationError, match="extra"):
        validate_tasks([_task("extra", run)])


def test_validate_tasks_requires_keyword_only_contract_parameters():
    def run(
        provider,
        *,
        execution_target_id,
        execution_target_name,
        execution_target_type,
        region,
        session,
        dry_run,
        metadata,
        dependency_data,
        actions,
    ):
        """Run an invalid task with a positional-or-keyword parameter."""

        pass

    with pytest.raises(TaskValidationError, match="keyword-only"):
        validate_tasks([_task("positional", run)])


def test_task_catalog_ambiguity_is_provider_scoped():
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
        dependency_data,
        actions,
    ):
        """Run a duplicate test task."""

        pass

    universal = _task("shared", run, source_label="universal")
    aws = _task("shared", run, source_label="aws", provider="aws")
    azure = _task("shared", run, source_label="azure", provider="azure")
    provider_catalogs = {
        "aws": ComponentCatalog.build([universal, aws]),
        "azure": ComponentCatalog.build([universal, azure]),
    }

    errors = task_catalog_ambiguity_errors(provider_catalogs)

    assert errors == [
        "task 'shared' is ambiguous for provider 'aws'; "
        "found in multiple sources: aws, universal",
        "task 'shared' is ambiguous for provider 'azure'; "
        "found in multiple sources: azure, universal",
    ]


def test_same_task_name_in_disjoint_provider_catalogs_is_valid():
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
        dependency_data,
        actions,
    ):
        """Run a provider-specific task."""

        pass

    aws = _task("audit", run, source_label="aws", provider="aws")
    azure = _task("audit", run, source_label="azure", provider="azure")
    provider_catalogs = {
        "aws": ComponentCatalog.build([aws]),
        "azure": ComponentCatalog.build([azure]),
    }

    assert task_catalog_ambiguity_errors(provider_catalogs) == []
    validate_tasks([aws, azure])


def test_validate_tasks_rejects_missing_detail_docstring():
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
        dependency_data,
        actions,
    ):
        pass

    run.__doc__ = None

    with pytest.raises(TaskValidationError, match="detail documentation"):
        validate_tasks([_task("missing-docstring", run)])
