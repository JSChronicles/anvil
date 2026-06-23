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

  <h3 align="center">README</h3>

  <p align="center">
    <a href="https://github.com/JSChronicles/anvil"><strong>Explore the docs</strong></a>
    <br />
    <a href="https://github.com/JSChronicles/anvil/issues/new?labels=Bug%2CNeeds+Triage&projects=&template=bug.yaml&title=%5BBUG%5D+%3Ctitle%3E">Report Bug</a>
    |
    <a href="https://github.com/JSChronicles/anvil/issues/new?labels=enhancement%2Cfeature+request&projects=&template=feature.yaml&title=%5BFEATURE%5D%3A+">Request Feature</a>
  </p>
</div>

## Introduction

Anvil is a declarative AWS execution engine for running Python tasks across large account and region fleets. Describe the work in YAML, keep task logic in plain Python modules, and let the engine handle authentication, role assumption, dependency ordering, bounded concurrency, and structured results so repeatable AWS work can run faster without turning orchestration into custom scripts.

For more, see the [documentation](https://opsfoundry.dev/).

### Why Anvil?

Anvil is built for teams that need repeatable AWS workflows, such as inventory, validation, enforcement, cleanup, and reporting, to run consistently across organizations, accounts, and regions.

- Declarative orchestration
  - Define execution in reusable YAML instead of one-off scripts.
  - Configure organizations, account lists, regions, tasks, task dependencies, dry runs, fail-fast behavior, and concurrency in one place.
- Multi-account and multi-organization by default
  - Discover active accounts and enabled regions, support include/exclude filtering
- Parallel execution and caching
  - Control concurrency at the target, account, and region levels. See [Caching and reuse](https://opsfoundry.dev/anvil/execution-model/#cache-and-reuse-boundaries).
- Shared discovery and session reuse
  - Validate the organization, discover accounts, and check enabled regions, only once, before execution.
- Task isolation
  - Write tasks as simple Python files with a `run(...)` function.
- Built-in and custom tasks
  - Use stock tasks for common AWS operations.
  - Add project-local tasks for team-specific work.
  - Extend the task set without changing the execution engine.
- Structured output and safer operations
  - Record structured results at task, account, target, and engine levels.

## Usage
> [!TIP]
> It is recommended to use the [foundry-anvil-template](https://github.com/JSChronicles/foundry-anvil-template).
>
> The template exposes project-local tasks and processors without forking Anvil.
>
> If you do not need/want the full Anvil framework and only want a simple starting point for small AWS Organization tasks, see: [`templates/aws_multi_account_template.py`](https://opsfoundry.dev/anvil/examples/#standalone-multi-account-script-template)


1. When using the uv tool, there are several ways to run and install dependencies. Here are only a couple examples:
1. uv sync:
   1. Sync the project's dependencies with the environment: uv sync
   1. Activate the virtual environment: .venv\Scripts\activate
1. uv run:
   1. Run a command in the project environment.: `uv run example.py <args>`
      1. uv run anvil run --config-file ./yaml/orgs.yaml
   1. Note that if you use uv run in a project, i.e. a directory with a pyproject.toml, it will install the current project before running the script.


There are multiple global commands:
```console
anvil graph     # Show the resolved task dependency graph
anvil results   # Query JSONL results and rerun failures
anvil list      # List available tasks and processors
anvil validate  # Validate tasks, processors, and AWS authentication
anvil run       # Execute YAML-defined workflows
```


Run a simple YAML file:

This executes the configured targets and tasks, then writes structured results under `./results`.

```console
anvil run --config-file ./yaml/orgs.yaml
```

```yaml
# orgs.yaml example
schema_version: 1

organizations:
  - name: smoke
    profile: root
    tasks:
      - name: noop
    dry_run: true
```

For delegated-administrator patterns, keep the base session on the
delegated-admin profile. Anvil uses that base session directly for the
delegated-admin account if it appears in Organizations discovery, and assumes
`role_name` in every other selected account, including the management/payer
account.

```yaml
schema_version: 1

organizations:
  - name: security
    profile: delegated-admin-security
    role_name: SecurityAuditRole
    regions:
      - us-east-1
    tasks:
      - name: noop
```

### Results

`anvil results` queries completed run output without rerunning AWS work. Use it
to filter historical JSONL results by target, account, region, task, or status,
emit JSON/JSONL for automation, rerun failed work, or run a processor against a
completed results directory. When a run has failures, Anvil prints ready-to-use
`anvil results` commands that point at the affected run's `results.jsonl` file
so you can inspect or rerun the failed accounts.

See more at [Common result queries](https://opsfoundry.dev/anvil/cli/#results)
and [Rerun failures](https://opsfoundry.dev/anvil/cli/#rerun-failures).

### Validation

Use `anvil validate` before a run to perform one or more checks without running
tasks:

```console
anvil validate --tasks --processors --auth --config-file ./yaml/orgs.yaml
```

`--tasks` and `--processors` validate discovery and callable signatures.
`--auth` validates AWS access for the configured targets.

See more at [Task validation](https://opsfoundry.dev/anvil/task-contract/#task-validation).

### Processors

Processors run after a target finishes and turn Anvil results into reports or
integration artifacts. Use them for formats that should stay outside task logic,
such as HTML, SARIF, Markdown, JSON summaries, tickets, or notification payloads.
Target `post_run` processor output is written under the run's `reports`
directory, so `output: smoke.html` becomes `<run_dir>/reports/smoke.html`.

Use `html_report` when you want a self-contained, human-readable report for a
completed target:

```yaml
schema_version: 1

organizations:
  - name: smoke
    profile: root
    regions:
      - us-east-1
    tasks:
      - name: noop
    post_run:
      - processor: html_report
        output: smoke.html
        run_on_failure: true
```

Use `sarif_report` when `detect_` tasks return `sarif_findings` and you want a
SARIF 2.1.0 report for code-scanning or security tooling:

```yaml
schema_version: 1

organizations:
  - name: lambda-runtime-audit
    profile: root
    regions:
      - us-*
    tasks:
      - name: detect_deprecated_lambda_runtimes
    metadata:
      runtimes:
        - python3.8
        - nodejs16.x
    post_run:
      - processor: sarif_report
        output: lambda-runtimes.sarif
        run_on_failure: true
```

See more at [HTML result reports](https://opsfoundry.dev/anvil/cli/#processors).

------------------------------

Run a more detailed YAML:

This shows multi-region execution, concurrency, account filtering, task dependencies, fail-fast behavior, dry-run mode, and task metadata.

```console
anvil run --config-file ./yaml/advanced.yaml
```

```yaml
# advanced.yaml example
schema_version: 1
max_parallel_targets: 2

organizations:
  - name: place
    profile: place-root
    # Organizations support explicit regions, all, glob selectors, and mixed
    # glob plus explicit selectors.
    regions:
      - us-east-1
      - us-west-2
    role_name: OrganizationAccountAccessRole

    max_workers: 5
    max_parallel_regions: 2
    fail_fast: false
    dry_run: true

    include:
      - "111111111111"
      - "222222222222"

    tasks:
      - name: discover_iam_users

      - name: backup_iam_users
        depends_on:
          - discover_iam_users

      - name: remove_iam_user
        depends_on:
          - discover_iam_users
          - backup_iam_users

    metadata:
      user_name: test
```


## Example Benchmarks

To measure concurrency behavior, the engine was tested across 3 organizations with a combined 260 accounts using the `count_vpc` task. The comparison below shows the same kind of work moving from sequential execution to organization-level parallelism and then to account-level parallelism.

The fastest measured run in this benchmark completed 260 accounts in about 1m 35s for 1 region, compared with a 3h 15m manual sequential estimate at 45 seconds per account. With 2 regions, the parallel account run completed in about 2m 48s.

<p align="left">
  <img src="images/count-vpc-grouped-comparison.png" alt="count_vpc runtime comparison" width="1200" height="600">
</p>



<!-- MARKDOWN LINKS & IMAGES -->
[pytest-badge]:https://github.com/JSChronicles/anvil/actions/workflows/pytest.yaml/badge.svg?branch=main
[pytest-url]:https://github.com/JSChronicles/anvil/actions/workflows/pytest.yaml
[ruff-badge]:https://github.com/JSChronicles/anvil/actions/workflows/ruff.yaml/badge.svg?branch=main
[ruff-url]:https://github.com/JSChronicles/anvil/actions/workflows/ruff.yaml

[prek-badge]:https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/j178/prek/master/docs/assets/badge-v0.json
[prek-url]:https://github.com/j178/prek
