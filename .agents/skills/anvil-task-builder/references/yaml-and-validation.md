# YAML Wiring And Validation

## YAML Wiring

Add or update an example config when a task needs metadata or dependencies:

```yaml
schema_version: 2

targets:
  - name: example
    provider:
      name: aws
      mode: organization
      options: {}
    regions:
      - us-east-1
    dry_run: true
    tasks:
      - name: inventory_users
      - id: remove_example_user
        name: remove_iam_user
        depends_on:
          - inventory_users
        dependency_data:
          users:
            task_id: inventory_users
            path: result.users
    metadata:
      user_name: example-user
```

`name` selects the discovered component. `id` identifies one configured use and
defaults to `name` when omitted. If a component is repeated, give every use an
explicit unique ID.

Use effective IDs in `depends_on`; Anvil does not fall back to component names.
Normal dependents require every dependency to succeed. Use `always_run: true`
with at least one dependency for cleanup that should run after unsuccessful
work.

`dependency_data` selects runtime values from tasks listed directly in
`depends_on`. Omit `path` for the complete producer `TaskResult`, use
`path: result` for its returned value, or select nested mapping values such as
`path: result.users`. Existing null values are valid; missing paths produce a
consumer task error before its `run()` function is called.

Task scope is module-declared with `TASK_SCOPE`; do not put scope in YAML.

For a complete configured-target fan-out/fan-in and recovery example, see
`examples/17-aws-config-cleanup-workflow.yaml`.

## Region Selection

Use explicit region or location names when the user wants exact coverage:

```yaml
regions:
  - us-east-1
  - us-west-2
```

GitHub targets use the provider-neutral global location:

```yaml
regions:
  - global
```

For AWS `organization`, Azure `tenant`/`subscriptions`, and GCP `projects`
targets, prefer selectors when the user wants broad multi-region coverage. Use
`all` when they want every available provider location discovered for each
execution target. `all` must be lowercase and must be the only region value:

```yaml
regions:
  - all
```

Use region/location globs when the user wants multiple similar provider
locations without listing each one. Globs can be used alone, combined with other
globs, or mixed with explicit regions:

```yaml
regions:
  - us-*
```

```yaml
regions:
  - us-*-1
```

```yaml
regions:
  - us-*
  - eu-*
```

```yaml
regions:
  - us-*
  - ca-central-1
```

Region selectors are resolved against provider-discovered locations. AWS
executes only `ENABLED` or `ENABLED_BY_DEFAULT` regions. Azure executes
locations returned for the subscription. GCP executes Compute regions with
status `UP`. Anvil warns for matched unavailable locations, rejects glob
selectors that match no known location, and fails when no available location
remains.

## Validation

After creating or editing a task, run:

```powershell
uv run anvil validate --tasks
```

This validates task discovery and the required `run()` signature without executing provider API logic.

If `uv run` cannot build or install the project in the current environment, and dependencies are already available, use this fallback:

```powershell
$env:PYTHONPATH='src'; python -m anvil.cli validate --tasks
```

For YAML examples, also validate the config schema or run the relevant example tests. A lightweight schema validation pattern is:

```powershell
$env:PYTHONPATH='src'; python -c "from pathlib import Path; import yaml; from anvil.validators import validate_config_schema; path=Path('examples/example.yaml'); validate_config_schema(config=yaml.safe_load(path.read_text(encoding='utf-8')) or {})"
```
