# anvil

<a name="readme-top"></a>

<!-- PROJECT SHIELDS -->
[![pytest][pytest-badge]][pytest-url]
[![ruff][ruff-badge]][ruff-url]
[![prek][prek-badge]][prek-url]



<!-- PROJECT LOGO -->
<br />
<div align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="images/anvil-logo-dark.png">
    <source media="(prefers-color-scheme: light)" srcset="images/anvil-logo-light.png">
    <img src="images/anvil-logo-light.png" alt="Anvil logo" width="236">
  </picture>

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

Anvil is a declarative provider-aware execution engine for running Python tasks across cloud target and region fleets. Describe the work in YAML, keep task logic in plain Python modules, and let the engine handle authentication, target resolution, dependency ordering, bounded concurrency, and structured results. The current runtime preserves the AWS organization/account behavior and optimizations from earlier releases while adding explicit Azure subscription and GCP project target support.

For more, see the [documentation](https://opsfoundry.dev/).

### Why Anvil?

Anvil is built for teams that need repeatable cloud workflows, such as inventory, validation, enforcement, cleanup, and reporting, to run consistently across provider targets and regions.

- Declarative orchestration
  - Define execution in reusable YAML instead of one-off scripts.
  - Configure provider targets, regions, tasks, task dependencies, dry runs, fail-fast behavior, and concurrency in one place.
- Multi-target by default
  - AWS can discover active organization accounts and enabled regions, with include/exclude filtering.
  - Azure subscriptions and GCP projects can run from explicit IDs or provider
    discovery.
- Parallel execution and caching
  - Control concurrency at the target, account, and region levels. See [Caching and reuse](https://opsfoundry.dev/anvil/execution-model/#cache-and-reuse-boundaries).
- Shared discovery and session reuse
  - Validate targets, discover supported provider metadata, and reuse session/runtime state before execution.
- Task isolation
  - Write tasks as simple Python files with a `run(...)` function.
- Built-in tasks
  - Use provider-package tasks for common AWS operations and universal tasks
    where they apply.
  - Provider-owned task plugin entry points can add universal tasks or
    provider-specific AWS, Azure, and GCP tasks.
- Structured output and safer operations
  - Record structured results at task, account/target, target group, and engine levels.

## Usage
> [!TIP]
> It is recommended to use the [foundry-anvil-template](https://github.com/JSChronicles/foundry-anvil-template).
>
> The template exposes project-local processors without forking Anvil.
>
> If you do not need/want the full Anvil framework and only want a simple starting point for small AWS Organization tasks, see: [`templates/aws_multi_account_template.py`](https://opsfoundry.dev/anvil/examples/#standalone-multi-account-script-template)


1. Install Anvil with the provider SDKs you need:
   1. Installed package users can choose provider extras with pip. Base installs
      include AWS support and the default CLI behavior:
      `pip install anvil`
   1. Azure users should install the Azure extra:
      `pip install "anvil[azure]"`
   1. GCP users should install the GCP extra:
      `pip install "anvil[gcp]"`
   1. Source checkout users should sync the matching uv extra instead:
      `uv sync --extra azure` or `uv sync --extra gcp`
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
anvil results   # Query JSONL results and rerun failures
anvil list      # List available tasks, processors, and providers
anvil validate  # Inspect environment health or run focused validation checks
anvil run       # Execute YAML-defined workflows
```


Run a simple YAML file:

This executes the configured targets and tasks, then writes structured results under `./results`.

```console
anvil run --config-file ./yaml/orgs.yaml
```

```yaml
# orgs.yaml example
schema_version: 2

targets:
  - name: smoke
    provider:
      name: aws
      mode: organization
      options:
        profile: root
    tasks:
      - name: noop
    dry_run: true
```

### Provider task packages
> [!NOTE]
> Duplicate task names across all packages and plugins applicable to the selected provider are rejected as ambiguous.
>

Task compatibility is determined by package location.

For schema-v2 invocation IDs, dependency-data selection, `always_run` recovery,
module-declared scopes, and configured-target fan-in/fan-out, see
[Task workflows](docs/task-workflows.md).

- `anvil.providers.tasks.<task>` is universal and can run for any provider.
- `anvil.providers.aws.tasks.<task>` is AWS-only.
- `anvil.providers.azure.tasks.<task>` is Azure-only.
- `anvil.providers.gcp.tasks.<task>` is GCP-only.

### Extension package discovery

Tasks, processors, and providers are discovered from package folders. Adding a
public module or provider folder to an already registered package does not
require another entry-point declaration. Discovery records names and sources
without importing child implementations; normal execution imports only the
selected components. Duplicate names are rejected as ambiguous and report every
conflicting source.

Third-party distributions register their package roots in `pyproject.toml`:

```toml
[project.entry-points."anvil.providers.tasks"]
universal-tasks = "company_anvil.tasks"

[project.entry-points."anvil.providers.aws.tasks"]
aws-tasks = "company_anvil.aws_tasks"

[project.entry-points."anvil.processors"]
processors = "company_anvil.processors"

[project.entry-points."anvil.provider_packages"]
providers = "company_anvil.providers"
```

Each task or processor filename is its component name. Each immediate child of
a provider collection is a provider package and must expose
`create_provider_instance()`.

Example Azure task configuration:

```yaml
schema_version: 2

targets:
  - name: azure-subscriptions
    provider:
      name: azure
      mode: subscriptions
      options: {}
    include:
      - 00000000-0000-0000-0000-000000000000
    regions:
      - eastus
    tasks:
      - name: count_resource_groups
```

Example GCP task configuration:

```yaml
schema_version: 2

targets:
  - name: gcp-projects
    provider:
      name: gcp
      mode: projects
      options:
        credentials_path: /secure/path/to/credentials.json
        quota_project_id: anvil-billing-project
    include:
      - anvil-dev-project
    regions:
      - us-central1
    tasks:
      - name: get_project_info
```

For delegated-administrator patterns, keep the base session on the
delegated-admin profile. Anvil uses that base session directly for the
delegated-admin account if it appears in Organizations discovery, and assumes
`role_name` in every other selected account, including the management/payer
account.

```yaml
schema_version: 2

targets:
  - name: security
    provider:
      name: aws
      mode: organization
      options:
        profile: delegated-admin-security
        role_name: SecurityAuditRole
    regions:
      - us-east-1
    tasks:
      - name: noop
```

### Results

`anvil results` queries completed run output without rerunning cloud work. Use it
to filter historical JSONL results by target, account, region/location, task, or
status, emit JSON/JSONL for automation, rerun failed work, or run a processor
against a completed results directory. When a run has failures, Anvil prints
ready-to-use `anvil results` commands that point at the affected run's
`results.jsonl` file so you can inspect or rerun the failed execution targets.

See more at [Common result queries](https://opsfoundry.dev/anvil/cli/#results)
and [Rerun failures](https://opsfoundry.dev/anvil/cli/#rerun-failures).

### Validation

Use `anvil validate` before a run to inspect the local environment or perform
one or more focused checks without running tasks:

```console
anvil validate
```

With no switches, `anvil validate` prints offline diagnostics for the current
Anvil environment, including Python and Anvil versions, optional provider
dependency availability, provider/task/processor discovery, local auth source
hints, and result path state. It does not call cloud APIs, validate live access,
or run tasks.

Validate a YAML config file offline:

```console
anvil validate --config-file ./yaml/orgs.yaml
```

This parses the config, validates schema and target shape, and checks CLI
override semantics without checking credentials or calling provider APIs.

Run focused validation categories:

```console
anvil validate --tasks --processors --auth --config-file ./yaml/orgs.yaml
```

`--tasks` and `--processors` validate discovery, keyword-only callable
signatures, and operator-facing detail documentation. Validation rejects
additional required parameters that Anvil cannot supply at runtime.
`--providers` validates the provider contract. `--auth` validates cloud access
for the configured targets after loading and validating the config file.

See more at [Task validation](https://opsfoundry.dev/anvil/task-contract/#task-validation).

### Processors

Processors run after a target finishes and turn Anvil results into reports or
integration artifacts. Use them for formats that should stay outside task logic,
such as HTML, SARIF, Markdown, JSON summaries, tickets, or notification payloads.
Processor modules expose a documented keyword-only
`run(*, context, output, metadata)` callable. `context.target_results` is the
canonical result collection; target-level runs additionally set
`context.target_name`, from which `target_result` and `target_result_path` are
derived. Treat context data and processor metadata as invocation snapshots.
Target `post_run` processor output is written under the run's `reports`
directory, so `output: smoke.html` becomes `<run_dir>/reports/smoke.html`.

Use `html_report` when you want a self-contained, human-readable report for a
completed target:

```yaml
schema_version: 2

targets:
  - name: smoke
    provider:
      name: aws
      mode: organization
      options:
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
schema_version: 2

targets:
  - name: lambda-runtime-audit
    provider:
      name: aws
      mode: organization
      options:
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

See more at [HTML result reports](https://opsfoundry.dev/anvil/cli/#processors),
including examples for separating target-level reports or combining a completed
run into one HTML report.

------------------------------

Run a more detailed YAML:

This shows multi-region execution, concurrency, account filtering, task dependencies, fail-fast behavior, dry-run mode, and task metadata.

```console
anvil run --config-file ./yaml/advanced.yaml
```

```yaml
# advanced.yaml example
schema_version: 2
max_parallel_targets: 2

targets:
  - name: place
    provider:
      name: aws
      mode: organization
      options:
        profile: place-root
        role_name: OrganizationAccountAccessRole
    # Organizations support explicit regions, all, glob selectors, and mixed
    # glob plus explicit selectors.
    regions:
      - us-east-1
      - us-west-2

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
