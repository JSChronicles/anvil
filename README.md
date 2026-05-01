# anvil

<a name="readme-top"></a>

<!-- PROJECT SHIELDS -->
[![pytest][pytest-badge]][pytest-url]
[![ruff][ruff-badge]][ruff-url]
[![prek][prek-badge]][prek-url]



<!-- PROJECT LOGO -->
<br />
<div align="center">
    <img src="images/logo.png" alt="Logo" width="256" height="256">
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

Anvil is a declarative AWS execution engine for running Python tasks across large account and region fleets. Describe the work in YAML, keep task logic in plain Python modules, and let the engine handle authentication, role assumption, dependency ordering, bounded concurrency, and structured results so repeatable AWS work can run faster without turning orchestration into custom scripts.

For a deeper look at the execution flow, see [docs/README.md](docs/README.md).

## Why Anvil?

For teams that need repeatable AWS workflows, such as inventory, validation, enforcement, cleanup, and reporting, to run consistently across organizations, accounts, and multiple regions.

- Declarative orchestration
  - Define execution in YAML instead of one-off scripts.
  - Configure organizations, account lists, regions, tasks, dependencies, dry runs, fail-fast behavior, and concurrency in one place.
- Multi-account and multi-organization by default
  - Discover active AWS Organizations accounts.
  - Support explicit account groups and include/exclude filters.
  - Assume roles into member accounts.
  - Let account owners, admins, governance teams, and security teams run approved tasks at the scope they control.
- Bounded parallel execution
  - Run configured organizations or account groups concurrently with `max_parallel_targets`.
  - Run accounts inside each target concurrently with `max_workers`.
  - Run regions inside each account concurrently with `max_parallel_regions`.
  - Keep concurrency explicit so large runs are faster without accidental API pressure.
- Shared discovery and session reuse
  - Preflight organization identity, account discovery, and enabled-region discovery.
  - Reuse discovery for repeated targets in the same organization.
  - Reuse sessions and clients while keeping credentials scoped to the correct account and region.
- Task isolation
  - Write tasks as plain Python modules.
  - Keep AWS business logic separate from authentication, role assumption, dependency ordering, result aggregation, and concurrency.
- Built-in (Stock) and custom tasks
  - Use built-in tasks for common AWS operations.
  - Add project-local tasks for team-specific work.
  - Expect more governance, security, inventory, cleanup, and reporting built-in tasks over time.
- Structured output and safer operations
  - Record structured results at task, account, target, and engine levels.
  - Use auth checks, dry runs, dependency ordering, optional tasks, fail-fast controls, and cancellation handling for safer repeat runs.


### Repository template

Create your own dedicated task repository using the [foundry-anvil-template](https://github.com/JSChronicles/foundry-anvil-template). The template provides a ready project layout for custom tasks, YAML examples, validation, and CI outside of the main Anvil repository.


### Standalone Multi-Account Script Template

If you do not need/want the full Anvil framework and only want a simple starting point for small AWS Organization tasks, see: [`templates/aws_multi_account_template.py`](./templates/multi_aws_account_task_template.py)

This template provides:
- AWS Organizations account discovery
- active-account filtering
  - `--include` / `--exclude` account selection
- parallel per-account execution
  - multiple regions per account
- assume-role handling for member accounts
- dry-run support
- JSON result output

Replace the innards of the `account_task()` function with your own per-account logic.
Replace the `--example-piece` argparse and `example_piece` in other areas or edit as desired

## Example Benchmarks

To measure concurrency behavior, the engine was tested across 3 organizations with a combined 260 accounts using the `count_vpc` task. The comparison below shows the same kind of work moving from sequential execution to organization-level parallelism and then to account-level parallelism.

The fastest measured run in this benchmark completed 260 accounts in about 1m 35s for 1 region, compared with a 3h 15m manual sequential estimate at 45 seconds per account. With 2 regions, the parallel account run completed in about 2m 48s.

<p align="left">
  <img src="images/count-vpc-grouped-comparison.png" alt="count_vpc runtime comparison" width="1200" height="600">
</p>



## Usage
1. When using the uv tool, there are several ways to run and install dependencies. Here are a few examples:
   1. Manual setup (similar to pip-tools):
      1. Create a Python virtual environment: uv venv or python -m venv .venv
      1. Activate the virtual environment: .\.venv\Scripts\activate.ps1
      1. Install dependencies: uv pip install --requirements pyproject.toml
1. uv sync:
   1. Sync the project's dependencies with the environment: uv sync
   1. Activate the virtual environment: .venv\Scripts\activate
1. uv run:
   1. Run a command in the project environment.: `uv run example.py <args>`
      1. uv run anvil run --config-file ./yaml/orgs.yaml
   1. Note that if you use uv run in a project, i.e. a directory with a pyproject.toml, it will install the current project before running the script.


For a complete GitHub Actions example that runs Anvil with AWS OIDC and uploads
the generated JSON results as workflow artifacts, see
[`examples/github-actions`](./examples/github-actions/README.md).

There are multiple global commands
```console
anvil auth …
anvil graph …
anvil results …
anvil tasks …
anvil run …
```

### Logging verbosity

The `run`, `auth check`, and `graph` commands support `--log-level` to control console output verbosity.

Supported values:
- `DEBUG`
- `INFO`
- `WARNING`
- `ERROR`
- `CRITICAL`

Examples:

```console
anvil run --config-file ./yaml/orgs.yaml --log-level ERROR
anvil auth check --config-file ./yaml/orgs.yaml --log-level WARNING
anvil graph --config-file ./yaml/orgs.yaml --log-level INFO
```

### Authentication

Authentication checks validate AWS credentials and access without executing any tasks.

```console
anvil auth check --help
```

Authenticate credentials from an organization file.
```console
anvil auth check --config-file ./yaml/orgs.yaml

INFO     [auth.py:auth_check:106] Running auth check for org=root profile=root auth_source=AuthSource.SSO
INFO     [auth.py:auth_check:106] Running auth check for org=other-root profile=other-root auth_source=AuthSource.SSO
INFO     [auth.py:auth_check:106] Running auth check for org=random-root profile=random-root auth_source=AuthSource.UNKNOWN
WARNING  [credentials.py:_protected_refresh:603] Refreshing temporary credentials failed during mandatory refresh period.
botocore.exceptions.UnauthorizedSSOTokenError: The SSO session associated with this profile has expired or is otherwise invalid. To refresh this SSO session run aws sso login with the corresponding profile.
{
  "generated_at": "2026-03-31T15:30:15.075014+00:00",
  "auth": [
    {
      "org_name": "root",
      "status": "error",
      "source": "sso",
      "started_at": "2026-03-31T15:30:14.836545+00:00",
      "ended_at": "2026-03-31T15:30:15.074440+00:00",
      "duration_seconds": 0.23789780004881322,
      "message": "AWS SSO session is invalid or expired.",
      "remediation": "aws sso login --profile root"
    },
    {
      "org_name": "other-root",
      "status": "error",
      "source": "sso",
      "started_at": "2026-03-31T15:30:14.841167+00:00",
      "ended_at": "2026-03-31T15:30:15.072661+00:00",
      "duration_seconds": 0.23149509984068573,
      "message": "AWS SSO session is invalid or expired.",
      "remediation": "aws sso login --profile other-root"
    },
    {
      "org_name": "random-root",
      "status": "error",
      "source": "unknown",
      "started_at": "2026-03-31T15:30:14.849622+00:00",
      "ended_at": "2026-03-31T15:30:14.904089+00:00",
      "duration_seconds": 0.054468399845063686,
      "message": "AWS profile not found.",
      "remediation": "Fix your AWS profile configuration."
    }
  ]
}


INFO [auth.py:auth_check:106] Running auth check for org=root profile=root auth_source=AuthSource.SSO
{
  "generated_at": "2026-03-31T15:34:56.998631+00:00",
  "auth": [
    {
      "org_name": "root",
      "status": "success",
      "source": "sso",
      "started_at": "2026-03-31T15:34:54.844004+00:00",
      "ended_at": "2026-03-31T15:34:56.971776+00:00",
      "duration_seconds": 2.1277707000263035,
      "message": "Authenticated successfully.",
      "remediation": null
    },
    {
      "org_name": "other-root",
      "status": "success",
      "source": "sso",
      "started_at": "2026-03-31T15:34:54.848072+00:00",
      "ended_at": "2026-03-31T15:34:56.998306+00:00",
      "duration_seconds": 2.1502324000466615,
      "message": "Authenticated successfully.",
      "remediation": null
    }
  ]
}
```

Suppress all output and rely on the exit code only (useful for CI)
```console
anvil auth check --config-file orgs.yaml --quiet
INFO     [auth.py:auth_check:106] Running auth check for org=root profile=root auth_source=AuthSource.SSO
```

### Graph
Display the resolved task dependency graph for an organization configuration.

```console
anvil graph --help
```

Generate a dependency graph from an organization file.
```console
anvil graph --config-file .\examples\07-optional-task-semantics.yaml

Execution Graph (optional-semantics-org)
----------------------------------------
inventory
└──     reporting
    └──         cleanup
```

Output graph results as JSON
```console
anvil graph --config-file .\examples\07-optional-task-semantics.yaml --json

{
  "organization": "optional-semantics-org",
  "tasks": [
    {
      "name": "inventory",
      "depends_on": []
    },
    {
      "name": "reporting",
      "depends_on": [
        "inventory"
      ]
    },
    {
      "name": "cleanup",
      "depends_on": [
        "reporting"
      ]
    }
  ]
}
```



### Task Management
List all available stock and user-defined tasks
```console
anvil tasks list

Available tasks:
plugin: my-test-project:
  - hello
  - test

stock:
  - compare_asg_to_cluster_instances
  - get_aws_inline_policies
  - get_organization_structure
  - noop
  - noop_fail
  - remove_iam_user
  - remove_missing_group_assignments
  ...
```

Validate all available stock and user-defined tasks:
```console
anvil tasks validate
[ERROR] task validation failed:
  - task 'cleanup' is missing required run() parameters: ['account_alias']
  - task 'inventory' is missing required run() parameters: ['metadata']
```

```console
anvil tasks validate
[OK] all tasks are valid
```

### Execution
```console
anvil run --help
```

Execute all configured organizations and accounts from one or more YAML files, write per-target full results, write a flattened query file, and produce one summary file per YAML in a run-scoped result directory:

```text
results/
  <config-stem>/
    <run-id>/
      summary.json
      results.jsonl
      organizations/
        <organization>.json
```

Account-group configs use `account-groups/` for per-target JSON files instead of `organizations/`.
```console
anvil run --config-file ./yaml/noop.yaml
INFO     [auth.py:auth_check:106] Running auth check for org=root profile=root auth_source=AuthSource.SSO
INFO     [organization.py:execute:39] Starting organization processing (org=root, region=us-east-1)
INFO     [account.py:execute:48] Processing account root (123456789000)
INFO     [account.py:execute:48] Processing account account1 (111111111111)
INFO     [account.py:execute:48] Processing account account2 (222222222222)
INFO     [noop.py:run:33] No-op task executed for account root (123456789000), dry_run=False
INFO     [account.py:execute:48] Processing account Log Archive (333333333333)
INFO     [account.py:execute:48] Processing account Audit (444444444444)
INFO     [noop.py:run:33] No-op task executed for account account1 (111111111111), dry_run=False
INFO     [noop.py:run:33] No-op task executed for account Audit (444444444444), dry_run=False
INFO     [noop.py:run:33] No-op task executed for account Log Archive (333333333333), dry_run=False
INFO     [noop.py:run:33] No-op task executed for account account2 (222222222222), dry_run=False
......
INFO     [cli.py:_write_run_results:132] Wrote run results to xxxx\xxxx\results\noop\2026-05-01T183012Z: summary=xxxx\xxxx\results\noop\2026-05-01T183012Z\summary.json, target_files=1, jsonl_records=50

#Summary below
{
  "state": "completed_success",
  "generated_at": "2026-03-17T18:48:47.392583+00:00",
  "auth": [
    {
      "org_name": "root",
      "status": "success",
      "source": "sso",
      "started_at": "2026-03-17T18:48:36.615369+00:00",
      "ended_at": "2026-03-17T18:48:38.338430+00:00",
      "duration_seconds": 1.7230594999855384,
      "message": "Authenticated successfully.",
      "remediation": null
    }
  ],
  "organizations": [
    {
      "organization": "root",
      "total_accounts": 50,
      "failed_accounts": 0,
      "interrupted_accounts": 0,
      "failed_tasks": 0,
      "has_failures": false,
      "error": null
    }
  ],
  "total_failed_accounts": 0,
  "total_interrupted_accounts": 0,
  "total_failed_tasks": 0
}
```

Use `--benchmark` only for performance investigations. It adds engine, target,
account, region, and result-write timing details to result JSON, which can
dramatically increase output size on large account, region, or task runs. Leave
it off for normal audit/reporting runs, and enable it when comparing benchmark
runs or looking for bottlenecks.

### Result Queries

Runs still write the existing full JSON result files. They also write JSONL
records that flatten account and task results for quick filtering:
`./results/{config-stem}/{run-id}/results.jsonl`.

Common queries:

```console
anvil results failures
anvil results failures --organization prod
anvil results accounts --status failed
anvil results tasks --task count_vpcs
anvil results regions --region us-east-1
anvil results failures --fields account_id,region,task,error --limit 20
anvil results tasks --status failed --jsonl
```

Advanced queries:

```console
anvil results failures --results-file ./results/orgs/2026-05-01T183012Z/results.jsonl
anvil results failures --results-file ./results/orgs/run-a/results.jsonl ./results/accounts/run-b/results.jsonl
anvil results tasks --organization prod --task count_vpcs --fields account_id,region,status,error
anvil results failures --fields record_type,target,account_id,region,task,error
anvil results tasks --status failed --fields account_id,region,error --jsonl
anvil results failures --fields target_type,target,account_id,task,error --limit 50
```

All result query commands support `--organization`, `--account`, `--region`,
`--task`, `--status`, `--fields`, `--limit`, `--results-file` with one or more
JSONL paths, and `--json` or `--jsonl` for structured filtered output. Without
`--results-file`, Anvil queries every `results.jsonl` file under `./results`.

To run multiple YAML files in one command, pass them after a single `--config-file` flag. They run sequentially in the order provided. Each YAML remains an isolated run with its own summary file, and the overall command exits non-zero if any YAML run fails.
```console
anvil run --config-file ./yaml/orgs.yaml ./yaml/orgs2.yaml ./yaml/orgs3.yaml
```

Within a single YAML, you can bound how many configured targets run in parallel. This is separate from each target's `max_workers` and `max_parallel_regions` settings:
```yaml
schema_version: 1
max_parallel_targets: 4
organizations:
  - name: root
    max_workers: 10
    max_parallel_regions: 2
```

`max_parallel_regions` defaults to `1`, which preserves serial region execution within each account. Values from `2` through `4` allow bounded parallel region execution. Approximate account-region task streams per target are `max_workers * max_parallel_regions`, before considering `max_parallel_targets`.

Use `max_parallel_regions` selectively. It is most useful when each region performs heavier, independent work, such as deep inventory, long paginated scans, slow regional service checks, or multiple regional tasks that hit different AWS services. For broad lightweight inventory across many accounts, account-level parallelism is often enough; increasing region parallelism can multiply AWS API pressure and make each regional call slower, especially when several tasks all call the same service. When tuning, start with `max_parallel_regions: 1`, raise it only for tasks with meaningful per-region runtime, and benchmark the full concurrency shape: `max_parallel_targets * max_workers * max_parallel_regions`.

You can run `--include`, `--exclude`, or `--dry-run` to override the YAML file if you want to just test something or run on certain accounts.
```console
# Include only specific accounts:
anvil run --config-file orgs.yaml --include 111111111111 222222222222

# Exclude specific accounts:
anvil run --config-file orgs.yaml --exclude 333333333333 444444444444

# Exclude specific accounts and perform a dry-run:
anvil run --config-file orgs.yaml --exclude 333333333333 444444444444 --dry-run
```


### How task discovery works

Tasks are resolved in the following order:

Anvil discovers tasks from two sources:

- Stock tasks - tasks shipped with Anvil (anvil.tasks)

- Plugin tasks - tasks registered via the anvil.tasks entry-point group

Directories named `tasks/` are conventional only and are not automatically scanned.


### Implement the Task Contract

Each task module must define a callable `run` function.
This is the minimum interface required for Anvil to discover and execute a task.

```python
def run(
    *,
    account_id: str,
    account_alias: str,
    session,
    dry_run: bool,
    metadata: dict,
    actions=None,
):
    """
    Execute the task for a single AWS account.
    """
```

#### Arguments

- `account_id` - AWS account ID currently being processed.
- `account_alias` - Friendly name of the account.
- `session` - A boto3 Session already scoped to the target account.
- `dry_run` - Indicates whether the task should make changes.
- `metadata` - Organization metadata defined in the configuration file.

The return value is optional. Any returned data may be included in execution results.

---

### Optional Helpers (Advanced Usage)

While only the `run()` function is required, tasks can optionally use Anvil-provided utilities to produce structured results or record actions.

For example, tasks may import helpers such as:

```python
from anvil.actions import ActionRecorder
```

This helper allow tasks to:

- record planned or executed actions
- produce structured output for reporting
- integrate with Anvil’s execution summaries

You can view examples of this here [ActionRecorder](./examples/ActionRecorder/README.md)

Using these utilities is **not required**, but recommended for tasks that modify infrastructure or need richer audit output.

### Reference tasks in YAML
Once configured, custom tasks behave exactly like stock tasks:

```yaml
tasks:
  - name: inventory
  - name: cleanup
    depends_on: [inventory]
```


<!-- MARKDOWN LINKS & IMAGES -->
[pytest-badge]:https://github.com/JSChronicles/anvil/actions/workflows/pytest.yaml/badge.svg?branch=main
[pytest-url]:https://github.com/JSChronicles/anvil/actions/workflows/pytest.yaml
[ruff-badge]:https://github.com/JSChronicles/anvil/actions/workflows/ruff.yaml/badge.svg?branch=main
[ruff-url]:https://github.com/JSChronicles/anvil/actions/workflows/ruff.yaml

[prek-badge]:https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/j178/prek/master/docs/assets/badge-v0.json
[prek-url]:https://github.com/j178/prek
