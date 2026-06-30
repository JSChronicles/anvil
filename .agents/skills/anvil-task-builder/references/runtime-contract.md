# Anvil Runtime Contract

For universal stock tasks in this repository, add modules under:

```text
src/anvil/providers/tasks/<task_name>.py
```

For provider-specific stock tasks, add modules under:

```text
src/anvil/providers/<provider>/tasks/<task_name>.py
```

The YAML task name must match the module filename:

```yaml
tasks:
  - name: count_vpc
```

For project-local or plugin tasks, expose the task package through the provider-owned entry point group that matches task compatibility:

```toml
[project.entry-points."anvil.providers.tasks"]
project-universal = "tasks.universal"

[project.entry-points."anvil.providers.aws.tasks"]
project-aws = "tasks.aws"

[project.entry-points."anvil.providers.azure.tasks"]
project-azure = "tasks.azure"

[project.entry-points."anvil.providers.gcp.tasks"]
project-gcp = "tasks.gcp"
```

Anvil discovers modules inside packages registered in provider-owned task entry point groups. Directories named `tasks/` are conventional only; they are not automatically scanned unless registered.

Every task module must define a callable keyword-only `run()` function. Use the provider-neutral signature unless nearby code has a stronger local convention:

```python
from anvil.actions import ActionRecorder


def run(
    *,
    provider: str,
    execution_target_id: str,
    execution_target_name: str,
    execution_target_type: str,
    region: str,
    location: str,
    session,
    dry_run: bool,
    metadata: dict[str, object],
    actions: ActionRecorder,
) -> dict:
```

Runtime facts:

- The provided `session` is already scoped to the provider target and region/location.
- `region` is the current task execution region/location. AWS sessions also expose `session.region_name`.
- Operator-provided task inputs come from `metadata`.
- `actions` is an `ActionRecorder` for audit-level actions.
- Returned values are included in Anvil result JSON.
- The engine already includes execution context such as target identity, `region`, and `dry_run` in normal results. Result fields currently retain `account_id` and `account_alias` compatibility names for all providers.

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
    provider: str,
    execution_target_id: str,
    execution_target_name: str,
    execution_target_type: str,
    region: str,
    location: str,
    session,
    dry_run: bool,
    metadata: dict[str, object],
    actions: ActionRecorder,
) -> dict:
    actions.record(
        f"Checked {provider} {execution_target_type} {execution_target_id} "
        f"in location {location or region}"
    )
    __LOGGER__.info(f"Completed example check in location {location or region}")

    return {"checked": True}
```

AWS-only legacy tasks may continue to accept `account_id`, `account_alias`, and
the scoped boto3 `session`; new provider-aware tasks should prefer the
provider-neutral signature above.
