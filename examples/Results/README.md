# anvil/Results

<a name="readme-top"></a>

<!-- PROJECT LOGO -->
<br />
<div align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="../../images/anvil-logo-dark.png">
    <source media="(prefers-color-scheme: light)" srcset="../../images/anvil-logo-light.png">
    <img src="../../images/anvil-logo-light.png" alt="Anvil logo" width="420">
  </picture>
  </a>

  <h3 align="center">README</h3>

  <p align="center">
    <a href="https://github.com/JSChronicles/anvil"><strong>Explore the docs</strong></a>
    <br />
    <a href="https://github.com/JSChronicles/anvil/issues/new?labels=Bug%2CNeeds+Triage&projects=&template=bug.yaml&title=%5BBUG%5D+%3Ctitle%3E">Report Bug</a>
    |
    <a href="https://github.com/JSChronicles/anvil/issues/new?labels=enhancement%2Cfeature+request&projects=&template=feature.yaml&title=%5BFEATURE%5D%3A+">Request Feature</a>
  </p>
</div>

## Introduction

Anvil tasks can produce useful result data in two complementary ways:

- Return JSON-serializable data from `run()` for structured task output.
- Use `ActionRecorder` to record concise audit-level actions.

Returned data is the native baseline. It is stored under each task result's
`result` field and is useful for inventory, measurements, findings, IDs,
counts, timing, and other structured task-specific data.

The `actions` parameter is required by the runtime contract. Recording actions
is optional behavior: use it when a task should record what it did, or what it
would do during dry-run mode, in a concise audit-friendly form.

## Returned Results

A task can return any JSON-serializable value from `run()`.

Return data directly from `run()` for small tasks:

```python
import logging

from anvil.actions import ActionRecorder

__LOGGER__ = logging.getLogger(__name__)


def run(
    *,
    provider: str,
    execution_target_id: str,
    execution_target_name: str,
    execution_target_type: str,
    region: str,
    session,
    dry_run: bool,
    metadata: dict[str, object],
    dependency_data: dict[str, object],
    actions: ActionRecorder,
) -> dict[str, object]:
    user_name = str(metadata["user_name"])
    iam = session.client("iam")
    groups = [
        group["GroupName"]
        for group in iam.list_groups_for_user(UserName=user_name)["Groups"]
    ]

    __LOGGER__.info(
        f"Inspected IAM groups for user {user_name} in account "
        f"{execution_target_name} ({execution_target_id}), dry_run={dry_run}"
    )

    return {
        "user_name": user_name,
        "dry_run": dry_run,
        "groups": groups,
        "summary": {"groups": len(groups)},
    }
```

For larger tasks, helper functions can build result data and return it to
`run()`:

```python
import logging

from anvil.actions import ActionRecorder

__LOGGER__ = logging.getLogger(__name__)


def cleanup_user_resources(
    iam_client, user_name: str, dry_run: bool
) -> dict[str, object]:
    group_results: list[dict[str, object]] = []
    access_key_results: list[dict[str, object]] = []

    for group in iam_client.list_groups_for_user(UserName=user_name)["Groups"]:
        group_name = group["GroupName"]
        if dry_run:
            message = f"(dry-run) Would remove user from group: {group_name}"
            status = "planned"
        else:
            iam_client.remove_user_from_group(GroupName=group_name, UserName=user_name)
            message = f"Removed user from group: {group_name}"
            status = "completed"

        __LOGGER__.debug(message)
        group_results.append(
            {"group_name": group_name, "status": status, "message": message}
        )

    for key in iam_client.list_access_keys(UserName=user_name)["AccessKeyMetadata"]:
        key_id = key["AccessKeyId"]
        if dry_run:
            message = f"(dry-run) Would delete access key: {key_id}"
            status = "planned"
        else:
            iam_client.delete_access_key(UserName=user_name, AccessKeyId=key_id)
            message = f"Deleted access key: {key_id}"
            status = "completed"

        __LOGGER__.debug(message)
        access_key_results.append(
            {"access_key_id": key_id, "status": status, "message": message}
        )

    return {
        "user_name": user_name,
        "dry_run": dry_run,
        "groups": group_results,
        "access_keys": access_key_results,
        "summary": {
            "groups": len(group_results),
            "access_keys": len(access_key_results),
        },
    }


def run(
    *,
    provider: str,
    execution_target_id: str,
    execution_target_name: str,
    execution_target_type: str,
    region: str,
    session,
    dry_run: bool,
    metadata: dict[str, object],
    dependency_data: dict[str, object],
    actions: ActionRecorder,
) -> dict[str, object]:
    user_name = metadata.get("user_name")
    if not isinstance(user_name, str):
        raise RuntimeError("example_cleanup requires metadata.user_name to be a string")

    iam = session.client("iam")
    return cleanup_user_resources(iam_client=iam, user_name=user_name, dry_run=dry_run)
```

The returned value appears in the task result:

```json
{
  "task_id": "function_returned_results",
  "task_name": "function_returned_results",
  "region": "us-east-1",
  "status": "success",
  "started_at": "2026-05-01T18:30:12+00:00",
  "ended_at": "2026-05-01T18:30:13+00:00",
  "duration_seconds": 1.0,
  "result": {
    "user_name": "example-user",
    "dry_run": false,
    "groups": [
      {
        "group_name": "Developers",
        "status": "completed",
        "message": "Removed user from group: Developers"
      }
    ],
    "access_keys": [
      {
        "access_key_id": "AKIA...",
        "status": "completed",
        "message": "Deleted access key: AKIA..."
      }
    ],
    "summary": {
      "groups": 1,
      "access_keys": 1
    }
  },
  "error": null
}
```

Returned data is also available in the flattened JSONL query artifact:

```console
anvil results --type task --task function_returned_results --fields target,entity_id,region,task_id,task_name,status,result --json
```

## Recorded Actions

`ActionRecorder` is available to tasks during execution and records concise,
audit-level task actions. Use logging for detailed task progress and
per-resource cleanup details.

Record actions directly inside the required `run()` function for small tasks:

```python
from anvil.actions import ActionRecorder


def run(
    *,
    provider: str,
    execution_target_id: str,
    execution_target_name: str,
    execution_target_type: str,
    region: str,
    session,
    dry_run: bool,
    metadata: dict[str, object],
    dependency_data: dict[str, object],
    actions: ActionRecorder,
) -> None:
    if dry_run:
        actions.record("(dry-run) Would validate account configuration")
    else:
        actions.record("Validated account configuration")
```

For larger tasks, helper functions can receive `ActionRecorder` and record the
specific planned or completed actions they perform.

```python
import logging

from anvil.actions import ActionRecorder

__LOGGER__ = logging.getLogger(__name__)


def cleanup_user(iam, user_name: str, dry_run: bool, actions: ActionRecorder) -> None:
    if dry_run:
        message = f"(dry-run) Would delete IAM user: {user_name}"
        __LOGGER__.debug(message)
        actions.record(message)
        return

    iam.delete_user(UserName=user_name)
    message = f"Deleted IAM user: {user_name}"
    __LOGGER__.debug(message)
    actions.record(message)


def run(
    *,
    provider: str,
    execution_target_id: str,
    execution_target_name: str,
    execution_target_type: str,
    region: str,
    session,
    dry_run: bool,
    metadata: dict[str, object],
    dependency_data: dict[str, object],
    actions: ActionRecorder,
) -> None:
    iam = session.client("iam")
    user_name = str(metadata["user_name"])
    cleanup_user(iam, user_name, dry_run, actions)
```

## Choosing a Result Channel

Use returned results when the task needs to report structured data:

- inventory lists
- counts and measurements
- validation findings
- resource IDs and metadata
- timing or diagnostic values

Use `ActionRecorder` when the task needs a concise audit trail:

- created, updated, or deleted resources
- skipped resources and decisions
- dry-run planned actions
- governance or cleanup outcomes

Production tasks may use both channels when that is useful. These examples keep
each channel separate so the result behavior is easy to understand.

## Examples

- [basic_returned_results](./basic_returned_results.py)
- [function_returned_results](./function_returned_results.py)
- [basic_action_recorder](./basic_action_recorder.py)
- [function_recording](./function_recording.py)
