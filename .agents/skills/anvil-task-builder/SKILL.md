---
name: anvil-task-builder
description: Build, edit, and validate Anvil task modules. Use when creating AWS account or region tasks for Anvil, modifying task run() implementations, adding dry-run behavior, recording actions, returning structured task results, or wiring tasks into Anvil YAML and plugin entry points.
---

# Anvil Task Builder

Use this skill to create Anvil tasks that satisfy the runtime contract, behave
safely in dry-run mode, and validate through Anvil's task discovery path.

## Workflow

1. Decide whether the task is a stock task or plugin task.
1. Create or edit the task module.
1. Implement the Anvil `run()` contract.
1. Follow Anvil task conventions for dry-run behavior, logging, actions, and
   returned data.
1. Apply normal Python and AWS implementation hygiene.
1. Add or update YAML examples that reference the task.
1. Run `uv run anvil tasks validate`.

## Anvil Runtime Contract

For stock tasks in this repository, add modules under:

```text
src/anvil/tasks/<task_name>.py
```

The YAML task name must match the module filename:

```yaml
tasks:
  - name: count_vpc
```

For project-local or plugin tasks, expose the task package through the plugin
project's `pyproject.toml`:

```toml
[project.entry-points."anvil.tasks"]
project = "tasks"
```

Anvil discovers modules inside packages registered in the `anvil.tasks`
entry-point group. Directories named `tasks/` are conventional only; they are
not automatically scanned unless registered.

Every task module must define a callable keyword-only `run()` function. Use this
signature unless nearby code has a stronger local convention:

```python
from anvil.actions import ActionRecorder


def run(
    *,
    account_id: str,
    account_alias: str,
    session,
    dry_run: bool,
    metadata: dict[str, object],
    actions: ActionRecorder,
) -> dict:
```

Runtime facts:

- The provided boto3 `session` is already scoped to the target account and
  region.
- `session.region_name` is the current task execution region.
- Operator-provided task inputs come from `metadata`.
- `actions` is an `ActionRecorder` for audit-level actions.
- Returned values are included in Anvil result JSON.
- The engine already includes execution context such as `account_id`,
  `account_alias`, `region`, and `dry_run` in normal results.

## Anvil Task Conventions

- Check `dry_run` before every mutating API call.
- Prefix dry-run log and action messages with `(dry-run)` when describing work
  that would happen but is intentionally not executed.
- Use logger calls for detailed task progress and per-resource cleanup details.
- Use `actions.record(...)` for concise audit-level planned or completed
  actions, usually at the end of `run()` or after a meaningful operation group
  completes.
- Do not log generic account-processing start messages; the engine already logs
  which account is being processed. Task logs should describe task-specific work
  or task-specific outcomes.
- Return task-specific data only. Do not duplicate execution context already
  included by the engine, such as `account_id`, `account_alias`, `region`, or
  `dry_run`, unless the task needs a renamed or transformed value for its own
  result schema.

## Task Granularity And Performance

Before creating or splitting inventory tasks, consider whether related data
should be gathered together. Task boundaries are useful for reuse and clear
failure semantics, but each task may repeat AWS client setup and list/describe
calls inside the same account-region.

Prefer separate tasks when:

- The tasks have different safety profiles, especially read-only vs mutating.
- They are commonly run independently.
- They need different optional/fail-fast behavior.
- A dependency relationship is meaningful to the workflow.
- Combining them would make the result shape confusing or too broad.

Consider one combined inventory task when:

- The tasks are read-only.
- They query the same AWS service in the same account-region.
- They repeat the same list or describe operations.
- Their outputs are normally consumed together.
- Performance matters more than task-level granularity.

For example, VPCs, VPC endpoints, and subnets are all EC2 regional inventory.
If the goal is a network inventory report, a single task can create one EC2
client, gather the related data once, and share in-memory results instead of
having multiple tasks rediscover overlapping state.

When overlap is present but separate tasks still make sense, prefer extracting
small shared helper functions over duplicating AWS pagination logic. For
benchmarking or heavy inventory tasks, consider metadata such as
`include_details: false` so users can return counts and timings without writing
large result payloads.

When suggesting YAML concurrency, treat `max_parallel_regions` as a targeted
tool rather than a default speed knob. It is most likely to help tasks with
meaningful per-region runtime, such as long paginated scans or slow regional
service checks. It can also help a multi-task regional workflow when the tasks
hit different AWS services, because the work is less concentrated on one
service API path. For lightweight describe/list inventory across many accounts,
especially multiple tasks that all call the same service, prefer
`max_parallel_regions: 1` first because total pressure grows as
`max_parallel_targets * max_workers * max_parallel_regions`, and regional API
calls can become slower under that load. Recommend benchmarking the actual task
mix before raising region concurrency.

## Python And AWS Guidance

- Keep task modules import-safe. Do not make AWS calls at import time.
- Use f-strings for task log messages to match the existing task style.
- Return dictionaries, lists, strings, numbers, booleans, or `None`; results
  should be JSON-serializable.
- Validate required `metadata` values before using them, including type checks,
  and raise a clear `RuntimeError` when required metadata is missing or invalid.
- Prefer AWS paginators for list and describe APIs that can paginate.
- Let unexpected AWS errors surface unless the task can add useful context.

When a task requires metadata, validate it before using it. Fail early with a
clear `RuntimeError` that names the task and required metadata key:

```python
user_name = metadata.get("user_name")
if not isinstance(user_name, str):
    raise RuntimeError("remove_iam_user requires metadata.user_name to be a string")
```

Prefer explicit type checks over assuming YAML input shape. Validate lists,
booleans, and nested objects before passing values into AWS APIs.

## Skeleton

```python
"""
Describe what this task does.
"""

import logging

from anvil.actions import ActionRecorder

__LOGGER__ = logging.getLogger(__name__)


def run(
    *,
    account_id: str,
    account_alias: str,
    session,
    dry_run: bool,
    metadata: dict[str, object],
    actions: ActionRecorder,
) -> dict:
    region_name = session.region_name

    actions.record(f"Checked account {account_id} in region {region_name}")
    __LOGGER__.info(f"Completed example check in region {region_name}")

    return {"checked": True}
```

## AWS Task Patterns

For read-only inventory tasks:

- Use the scoped session to build clients.
- Use paginators where available.
- Return counts and identifiers that are useful in downstream JSON output.
- Record a concise action summary.

For mutating tasks:

- Gather current state first.
- Record what would change.
- If `dry_run` is true, return planned changes without calling mutating APIs.
- If `dry_run` is false, execute the smallest necessary AWS calls.
- Return both changed and skipped items where practical.
- Put detailed per-resource messages in helper functions at `debug` level when
  they are useful for troubleshooting.
- Record the high-level planned or completed action once with `actions.record`.

Example dry-run shape:

```python
if dry_run:
    __LOGGER__.info(f"(dry-run) Would delete IAM user {user_name} in account {account_id}")
    actions.record(f"(dry-run) Would delete IAM user {user_name} in account {account_id}")
    return {"planned": True, "deleted": False, "user_name": user_name}

iam.delete_user(UserName=user_name)
__LOGGER__.info(f"Deleted IAM user {user_name} in account {account_id}")
actions.record(f"Deleted IAM user {user_name} in account {account_id}")
return {"planned": False, "deleted": True, "user_name": user_name}
```

For cleanup-style tasks, prefer this pattern:

```python
def cleanup_user_resources(iam_client, user_name: str, dry_run: bool) -> None:
    for key in iam_client.list_access_keys(UserName=user_name).get(
        "AccessKeyMetadata", []
    ):
        key_id = key["AccessKeyId"]
        if dry_run:
            __LOGGER__.debug(f"(dry-run) Would delete access key: {key_id}")
        else:
            iam_client.delete_access_key(UserName=user_name, AccessKeyId=key_id)
            __LOGGER__.debug(f"Deleted access key: {key_id}")


def run(
    *,
    account_id: str,
    account_alias: str,
    session,
    dry_run: bool,
    metadata: dict[str, object],
    actions: ActionRecorder,
) -> None:
    user_name = metadata.get("user_name")
    if not isinstance(user_name, str):
        raise RuntimeError("remove_iam_user requires metadata.user_name to be a string")

    iam_client = session.client("iam")
    cleanup_user_resources(iam_client=iam_client, user_name=user_name, dry_run=dry_run)

    if dry_run:
        actions.record(f"(dry-run) Would remove IAM user resources for {user_name}")
    else:
        actions.record(f"Removed IAM user resources for {user_name}")
```

## YAML Wiring

Add or update an example config when a task needs metadata or dependencies:

```yaml
schema_version: 1

organizations:
  - name: example
    regions:
      - us-east-1
    dry_run: true
    tasks:
      - name: inventory_users
      - name: remove_iam_user
        depends_on:
          - inventory_users
        optional: false
    metadata:
      user_name: example-user
```

Use `depends_on` when task order matters. Use `optional: true` only when failure
should not fail the account or block dependent work.

## Validation

After creating or editing a task, run:

```powershell
uv run anvil tasks validate
```

This validates task discovery and the required `run()` signature without
executing AWS logic.

If `uv run` cannot build or install the project in the current environment, and
dependencies are already available, use this fallback:

```powershell
$env:PYTHONPATH='src'; python -m anvil.cli tasks validate
```

For YAML examples, also validate the config schema or run the relevant example
tests. A lightweight schema validation pattern is:

```powershell
$env:PYTHONPATH='src'; python -c "from pathlib import Path; import yaml; from anvil.validators import validate_config_schema; path=Path('examples/example.yaml'); validate_config_schema(config=yaml.safe_load(path.read_text(encoding='utf-8')) or {})"
```
