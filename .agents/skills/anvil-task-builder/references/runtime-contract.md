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

[project.entry-points."anvil.providers.github.tasks"]
project-github = "tasks.github"
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
    session,
    dry_run: bool,
    metadata: dict[str, object],
    actions: ActionRecorder,
) -> dict:
```

`anvil list --tasks <task_name> --detail` uses the `run()` docstring first and
falls back to the module docstring only when the callable has no docstring. New
tasks should put the operator-facing detail on `run()` in Google style:

- Start with a concise summary of what the task does.
- Document provider and target assumptions.
- Document required and optional `metadata` keys.
- Include `Args:`, `Returns:`, and `Raises:` sections when they apply.
- Keep implementation details out unless they help operators use the task safely.

Runtime facts:

- The provided `session` is already scoped to the provider target and region.
- `region` is the current task execution region. AWS sessions also expose `session.region_name`.
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
    session,
    dry_run: bool,
    metadata: dict[str, object],
    actions: ActionRecorder,
) -> dict:
    """Check the current provider target and return a simple status payload.

    Args:
        provider: Provider name for the current execution target.
        execution_target_id: Provider-specific target ID.
        execution_target_name: Display name for the current target.
        execution_target_type: Provider-specific target type.
        region: Current execution region.
        session: Provider runtime session scoped to the target and region.
        dry_run: Whether Anvil is running in dry-run mode.
        metadata: Task metadata from YAML. This task does not require metadata.
        actions: Action recorder provided by the Anvil engine.

    Returns:
        A JSON-serializable payload indicating that the target was checked.
    """

    actions.record(
        f"Checked {provider} {execution_target_type} {execution_target_id} "
        f"in region {region}"
    )
    __LOGGER__.info(f"Completed example check in region {region}")

    return {"checked": True}
```

AWS-only legacy tasks may continue to accept `account_id`, `account_alias`, and
the scoped boto3 `session`; new provider-aware tasks should prefer the
provider-neutral signature above.
