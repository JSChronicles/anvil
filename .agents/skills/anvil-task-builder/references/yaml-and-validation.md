# YAML Wiring And Validation

## YAML Wiring

Add or update an example config when a task needs metadata or dependencies:

```yaml
schema_version: 1

organizations:
  - name: example
    regions:
      - us-east-1
    dry_run: true
    tasks:
      - name: inventory_users
      - name: remove_iam_user
        depends_on:
          - inventory_users
        optional: false
    metadata:
      user_name: example-user
```

Use `depends_on` when task order matters. Use `optional: true` only when failure should not fail the account or block dependent work.

## Validation

After creating or editing a task, run:

```powershell
uv run anvil tasks validate
```

This validates task discovery and the required `run()` signature without executing AWS logic.

If `uv run` cannot build or install the project in the current environment, and dependencies are already available, use this fallback:

```powershell
$env:PYTHONPATH='src'; python -m anvil.cli tasks validate
```

For YAML examples, also validate the config schema or run the relevant example tests. A lightweight schema validation pattern is:

```powershell
$env:PYTHONPATH='src'; python -c "from pathlib import Path; import yaml; from anvil.validators import validate_config_schema; path=Path('examples/example.yaml'); validate_config_schema(config=yaml.safe_load(path.read_text(encoding='utf-8')) or {})"
```