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

`ActionRecorder` is available to tasks during execution and can be used anywhere within your task module.

You may record actions directly inside the required `run()` function, or pass the recorder into helper functions for more complex workflows.

---

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

    actions.record(
        "Validated account configuration",
        details={"account": account_alias},
    )
```

### Example - Using ActionRecorder in Helper Functions
Passing the recorder into helper functions is recommended for larger tasks that split logic across multiple functions.

```python
from anvil.actions import ActionRecorder


def cleanup_user(iam, user_name, dry_run, actions):
    actions.record(f"Cleaning resources for {user_name}")

    if not dry_run:
        iam.delete_user(UserName=user_name)

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
    cleanup_user(iam, metadata["user_name"], dry_run, actions)
```


### Other Examples
- [basic_action_recorder](./basic_action_recorder.py)
- [function_recording](./function_recording.py)
