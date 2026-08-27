# GitHub Task Patterns

## Target Shapes

GitHub tasks should use the provider-neutral `run()` signature. Validate the
provider and target type before making GitHub API calls:

- `organizations` mode discovers repositories under configured owner logins for
  granular tasks. A standalone `search_code` task instead executes once per
  organization to use GitHub's efficient `org:` search qualifier.
- `repositories` mode runs against explicit `owner/repo` targets.
- GitHub uses the provider-neutral `global` region.

Repository-only tasks should validate and split `execution_target_id` with
`require_repository_target(...)` from
`anvil.providers.github.tasks._rest`.

## REST Helpers

Use metadata validators from `anvil.providers.tasks._task_helpers` and REST
helpers from `anvil.providers.github.tasks._rest` instead of reaching into
PyGithub directly when a REST endpoint is needed:

- `require_github_provider(...)` validates provider compatibility.
- `require_repository_target(...)` validates repository targets and returns
  `(owner, repo)`.
- `metadata_bool(...)`, `metadata_int(...)`, and `metadata_string(...)` validate
  scalar YAML metadata consistently across providers.
- `metadata_params(...)` validates GitHub REST query parameters.
- `rest_get(...)` performs a single REST GET through the runtime session client.
- `list_rest_items(...)` handles paginated REST list endpoints with
  `max_results`.
- `runtime_error_from_provider_error(...)` maps common PyGithub exceptions to
  task-facing `RuntimeError` messages.

## Task Behavior

- Keep GitHub tasks read-only unless the task name and docs clearly describe a
  mutation.
- Validate metadata before GitHub API calls.
- Configure `search_code` in its own target so provider planning can choose the
  most efficient organization or repository search scope.
- Bound list-style tasks with `max_results` metadata and a conservative default.
- Return JSON-serializable API data only.
- Record one concise action summary with `actions.record(...)`.
- Use `region` in log and action messages.

## Examples

Use existing first-party tasks as implementation references:

- `src/anvil/providers/github/tasks/audit_branch_protection.py` for a
  repository-only REST task with optional metadata.
- `src/anvil/providers/github/tasks/list_dependabot_alerts.py` for an
  organization-or-repository paginated REST task.
- `src/anvil/providers/github/tasks/search_code.py` for a task that uses the
  GitHub session client's higher-level search helper.
