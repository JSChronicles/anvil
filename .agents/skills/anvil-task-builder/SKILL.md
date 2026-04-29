---
name: anvil-task-builder
description: Builds and maintains Anvil task modules, workflows, schemas, runner behavior, and plugin templates. Use when user asks to "create an Anvil task", "edit this task", "add dry-run behavior", "record actions", "return task results", "update Anvil YAML", "modify schemas", "change account execution", or "update plugin templates".
---

# Anvil Task Builder

Use this skill to create Anvil tasks that satisfy the runtime contract, behave safely in dry-run mode, and validate through Anvil's task discovery path.

## Workflow

1. Decide whether the task is a stock task or plugin task.
2. Create or edit the task module.
3. Implement the Anvil `run()` contract.
4. Follow Anvil task conventions for dry-run behavior, logging, actions, and returned data.
5. Apply normal Python and AWS implementation hygiene.
6. Add or update YAML examples that reference the task when useful.
7. Run `uv run anvil tasks validate`.

## Core Rules

- Stock task modules live under `src/anvil/tasks/<task_name>.py`.
- YAML task names must match task module filenames.
- Plugin task packages must be exposed through the `anvil.tasks` entry-point group.
- Every task module must define a callable keyword-only `run()` function.
- The provided boto3 `session` is already scoped to the target account and region.
- Check `dry_run` before every mutating API call.
- Prefix dry-run log and action messages with `(dry-run)` for planned work.
- Use logger calls for task-specific progress and troubleshooting details.
- Use `actions.record(...)` for concise audit-level planned or completed actions.
- Return task-specific JSON-serializable data only.
- Do not duplicate execution context already included by the engine unless the task needs a transformed value.
- Keep task modules import-safe. Do not make AWS calls at import time.
- Validate required `metadata` values before use and raise clear `RuntimeError` messages.
- Prefer AWS paginators for list and describe APIs that can paginate.

## Reference Loading

Load only the reference files needed for the current task:

- For stock task, plugin task, entry-point, and `run()` signature rules, read `references/runtime-contract.md`.
- For dry-run behavior, action recording, logging, and result shape, read `references/dry-run-and-actions.md`.
- For task granularity, inventory task boundaries, performance, and region concurrency, read `references/task-granularity.md`.
- For AWS read-only and mutating task implementation patterns, read `references/aws-task-patterns.md`.
- For YAML examples, dependencies, optional tasks, and validation commands, read `references/yaml-and-validation.md`.

## Review Behavior

When reviewing Anvil tasks, prioritize runtime contract violations, missing dry-run guards, unsafe AWS mutation behavior, invalid metadata handling, missing task discovery wiring, and validation gaps.

If no issues are found, say so directly and mention any commands that were not run.
