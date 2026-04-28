# Anvil Runtime Contract

For stock tasks in this repository, add modules under:

```text
src/anvil/tasks/<task_name>.py
```

The YAML task name must match the module filename:

```yaml
tasks:
  - name: count_vpc
```

For project-local or plugin tasks, expose the task package through the plugin project's `pyproject.toml`:

```toml
[project.entry-points."anvil.tasks"]
project = "tasks"
```

Anvil discovers modules inside packages registered in the `anvil.tasks` entry-point group. Directories named `tasks/` are conventional only; they are not automatically scanned unless registered.

Every task module must define a callable keyword-only `run()` function. Use this signature unless nearby code has a stronger local convention:

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

- The provided boto3 `session` is already scoped to the target account and region.
- `session.region_name` is the current task execution region.
- Operator-provided task inputs come from `metadata`.
- `actions` is an `ActionRecorder` for audit-level actions.
- Returned values are included in Anvil result JSON.
- The engine already includes execution context such as `account_id`, `account_alias`, `region`, and `dry_run` in normal results.

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