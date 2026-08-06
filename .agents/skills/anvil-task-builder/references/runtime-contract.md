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

For project-local or plugin tasks, expose the task package through the provider-owned entry point group that matches task scope:

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

Every task module must define a callable keyword-only `run()` function. Use the provider-neutral signature:

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
- Tasks run once per concrete region by default. A task module may declare
  `TASK_SCOPE = "target"` to run once per execution target or
  `TASK_SCOPE = "configured_target"` to run once for the configured YAML target.
- Providers declare which task scopes they support. AWS supports
  `configured_target` and `region`; Azure, GCP, and GitHub support `region` and
  `target`.
- A target-scoped task receives the first resolved concrete provider location
  as `region`, and its session uses that location. No synthetic target-scope or
  global sentinel is introduced. GitHub's `global` value is a real provider
  location.
- Anvil does not automatically filter provider API responses by region.
- Task documentation should explain whether the task uses `region` to select an
  endpoint/client, passes it to an API request filter, filters returned resources
  itself, or ignores it because the API is target-wide.
- For region-scoped tasks, `region` is the current task execution region. AWS
  sessions also expose `session.region_name`.
- Operator-provided static task inputs come from `metadata`. Target metadata is
  recursively merged with task metadata, with task values taking precedence.
- Runtime dependency inputs come from `dependency_data`. They are selected from
  direct dependency `TaskResult` objects and are never merged into `metadata`.
- Tasks should treat both mappings as read-only. Anvil deep-copies nested
  mappings and lists for every invocation.
- `actions` is an `ActionRecorder` for audit-level actions.
- Returned values are included in Anvil result JSON.
- The engine already includes execution context such as target identity, `region`, and `dry_run` in normal results.

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
    dependency_data: dict[str, object],
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
        dependency_data: Runtime dependency inputs. This task requires none.
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

First-party provider tasks should use the provider-neutral signature above.

## Invocation identity and dependencies

YAML `name` selects the discovered component. YAML `id` identifies one
configured invocation and defaults to `name` when omitted. `depends_on` and
`dependency_data.task_id` always reference effective invocation IDs. Component
names are not a dependency fallback.

The same component may be configured more than once only when every occurrence
has an explicit, unique ID. Results preserve both `task_id` and `task_name`.

Normal tasks run only when every dependency succeeds. An `always_run` task waits
for every dependency to settle and then runs even after errors, interruption, or
skips, provided its dependency chain began. Successful finalization does not
erase an upstream failure.

Use `TaskExecutionError` when a task must report failure while retaining
JSON-serializable recovery data:

```python
from anvil.task_errors import TaskExecutionError


raise TaskExecutionError(
    "Mutation partially failed", partial_result={"attachments": detached_attachments}
)
```
