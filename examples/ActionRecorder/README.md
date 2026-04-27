# anvil/ActionRecorder

<a name="readme-top"></a>


<!-- PROJECT LOGO -->
<br />
<div align="center">
    <img src="../../images/logo.png" alt="Logo" width="256" height="256">
  </a>

  <h3 align="center">README</h3>

  <p align="center">
    <a href="https://github.com/JSChronicles/anvil"><strong>Explore the docs »</strong></a>
    <br />
    <a href="https://github.com/JSChronicles/anvil/issues/new?labels=Bug%2CNeeds+Triage&projects=&template=bug.yaml&title=%5BBUG%5D+%3Ctitle%3E">Report Bug</a>
    ·
    <a href="https://github.com/JSChronicles/anvil/issues/new?labels=enhancement%2Cfeature+request&projects=&template=feature.yaml&title=%5BFEATURE%5D%3A+">Request Feature</a>
  </p>
</div>

## Introduction

`ActionRecorder` provides a structured way for Anvil tasks to record actions, decisions, and outcomes during execution.

Instead of relying only on logging output, tasks can use `ActionRecorder` to produce consistent, machine-readable results that integrate with Anvil's execution summaries and reporting.

Using `ActionRecorder` is optional but strongly recommended for tasks that:

- modify infrastructure
- perform governance checks
- need auditable execution details

## Usage

`ActionRecorder` is available to tasks during execution and records concise,
audit-level task actions. Use logging for detailed task progress and
per-resource cleanup details.

You may record actions directly inside the required `run()` function. For larger
tasks, helper functions should usually log detailed per-resource work and return
control to `run()`, where the task records one high-level planned or completed
action.

---

### Output Comparison

Without `ActionRecorder`, a task can still log what happened:

```text
INFO Validated account configuration
```

The task result only has the normal execution fields:

```json
{
  "task": "validate_account",
  "region": "us-east-1",
  "status": "success",
  "started_at": "2026-04-26T20:15:00+00:00",
  "ended_at": "2026-04-26T20:15:01+00:00",
  "duration_seconds": 1.0,
  "result": null,
  "error": null
}
```

With `ActionRecorder`, logs can stay focused on human-readable progress:

```text
INFO Validated account configuration
```

The task also records a concise action that can be used by summaries, auditing,
or reporting:

```json
{
  "task": "validate_account",
  "region": "us-east-1",
  "status": "success",
  "actions": [
    "Validated account configuration"
  ]
}
```

### Example - Record Actions Directly in `run()`
This approach works well for small or single-purpose tasks.

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
) -> None:

    if dry_run:
        actions.record("(dry-run) Would validate account configuration")
    else:
        actions.record("Validated account configuration")
```

### Example - Log Details in Helper Functions
Passing work into helper functions is recommended for larger tasks that split
logic across multiple functions. Log detailed per-resource work in helpers and
record a concise action from `run()`.

```python
import logging

from anvil.actions import ActionRecorder

__LOGGER__ = logging.getLogger(__name__)


def cleanup_user(iam, user_name, dry_run):
    if dry_run:
        __LOGGER__.debug(f"(dry-run) Would delete IAM user: {user_name}")
    else:
        iam.delete_user(UserName=user_name)
        __LOGGER__.debug(f"Deleted IAM user: {user_name}")

def run(
    *,
    account_id: str,
    account_alias: str,
    session,
    dry_run: bool,
    metadata: dict[str, object],
    actions: ActionRecorder,
) -> None:
    iam = session.client("iam")
    cleanup_user(iam, metadata["user_name"], dry_run)

    if dry_run:
        actions.record(
            f"(dry-run) Would clean IAM resources for {metadata['user_name']}"
        )
    else:
        actions.record(f"Cleaned IAM resources for {metadata['user_name']}")
```


### Other Examples
- [basic_action_recorder](./basic_action_recorder.py)
- [function_recording](./function_recording.py)
