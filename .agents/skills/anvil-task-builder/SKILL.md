---
name: anvil-task-builder
description: Builds and maintains Anvil task modules, workflows, schemas, runner behavior, SARIF-compatible detect_ tasks, and plugin templates. Use when user asks to "create an Anvil task", "edit this task", "add dry-run behavior", "record actions", "return task results", "create a SARIF task", "create a detect task", "update Anvil YAML", "modify schemas", "change account execution", "update plugin templates", "add concurrency for the payer account", or "build a management-account-only task".
metadata:
  author: JSChronicles
  version: "0.10"
---

# Anvil Task Builder

Use this skill to create Anvil tasks that satisfy the runtime contract, behave safely in dry-run mode, and validate through Anvil's task discovery path.

## Workflow

1. Decide whether the task is a stock task or plugin task.
2. Create or edit the task module.
3. Implement the Anvil `run()` contract.
4. Add useful Google-style docstrings, especially on `run()`, so `anvil list --tasks <task_name> --detail` explains the task.
5. Follow Anvil task conventions for dry-run behavior, logging, actions, and returned data.
6. Apply normal Python and provider-specific implementation hygiene.
7. Add or update YAML examples that reference the task when useful.
8. Run `uv run anvil validate --tasks`.

## Core Rules

- Universal stock task modules live under `src/anvil/providers/tasks/<task_name>.py`.
- Provider-specific stock task modules live under `src/anvil/providers/<provider>/tasks/<task_name>.py`.
- YAML task names must match task module filenames.
- Name list and mutation tasks with a singular resource noun, such as
  `list_user`, `remove_user`, `list_dns_record`, or `remove_team_member`.
- Let one singularly named task operate on one or more selected resources. Use a
  plural metadata key containing an array, such as `users: [USER_ID]`; require
  the array shape even when it contains one item. Do not add separate plural
  task variants such as `remove_users`.
- Qualify the task name when a provider has multiple resource or identity
  boundaries, such as `remove_iam_user`, `remove_idc_user`, or
  `remove_account_member`.
- Require a non-empty selector array for destructive tasks. A list task may
  treat an omitted selector array as “list all” only when that behavior is safe
  and documented.
- Treat renaming a discovered task or changing its metadata contract as a
  breaking change. Update YAML examples, documentation, and tests, and use a
  breaking Conventional Commit marker plus a `BREAKING CHANGE:` footer.
- Extension task packages must use `anvil.providers.tasks` for universal tasks
  or `anvil.providers.<provider>.tasks` for provider-specific tasks. Derive
  `<provider>` from the provider's current `ProviderMetadata.name`; do not copy
  a fixed provider allowlist into plugin guidance.
- Extension processor packages use `anvil.processors`. Provider collection
  packages use `anvil.provider_packages` and expose one immediate child package
  per provider with `create_provider_instance()`. Do not use legacy generic
  plugin or per-component entry points.
- Every task module must define a callable keyword-only `run()` function.
- Every task `run()` function must have a useful Google-style docstring. Include a short summary plus `Args:`, `Returns:`, and `Raises:` sections when applicable; document required `metadata` keys explicitly.
- The provided `session` is already scoped to the provider target and region.
- Check `dry_run` before every mutating API call.
- Prefix dry-run log and action messages with `(dry-run)` for planned work.
- Use logger calls for task-specific progress and troubleshooting details.
- Use `actions.record(...)` for concise audit-level planned or completed actions.
- Return task-specific JSON-serializable data only.
- Do not duplicate execution context already included by the engine unless the task needs a transformed value.
- Keep task modules import-safe. Do not make provider SDK calls at import time.
- Validate required `metadata` values before use and raise clear `RuntimeError` messages.
- Prefer provider SDK pagination helpers for list and describe APIs that can paginate.

## Reference Loading

Load only the reference files needed for the current task:

- For stock task, plugin task, entry-point, and `run()` signature rules, read `references/runtime-contract.md`.
- For dry-run behavior, action recording, logging, and result shape, read `references/dry-run-and-actions.md`.
- For task granularity, inventory task boundaries, performance, and region concurrency, read `references/task-granularity.md`.
- For concurrency guidance specific to workflows that target only the payer/management account, read `references/payer-management-account-tasks.md`.
- For AWS read-only and mutating task implementation patterns, read `references/aws-task-patterns.md`.
- For GitHub REST task helpers, repository target rules, and metadata helpers, read `references/github-task-patterns.md`.
- For SARIF-compatible `detect_` tasks and `sarif_findings` output, read `references/sarif-detection-tasks.md`.
- For YAML examples, invocation IDs, dependencies, dependency data, and validation commands, read `references/yaml-and-validation.md`.

## Review Behavior

When reviewing Anvil tasks, prioritize runtime contract violations, missing or weak `run()` docstrings for `--detail`, missing dry-run guards, unsafe provider mutation behavior, invalid metadata handling, missing task discovery wiring, and validation gaps.

If no issues are found, say so directly and mention any commands that were not run.
