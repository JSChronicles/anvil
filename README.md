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
anvil list      # List available tasks, processors, and providers
anvil validate  # Validate tasks, processors, providers, and authentication
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

### Provider targets

Each target group may declare `provider`, `mode`, and `provider_options`.
Omitted provider fields keep legacy AWS behavior:

- `organizations` defaults to `provider: aws` and `mode: organization`.
- `accounts` defaults to `provider: aws` and `mode: accounts`.
- Legacy top-level AWS `profile` and `role_name` remain supported.
- `provider_options.profile` and `provider_options.role_name` are AWS
  compatibility aliases. If both the top-level field and alias are present,
  they must match.

Current supported provider modes:

- `include` and `exclude` are mutually exclusive for all providers and modes.
- AWS `organization`: discovers AWS Organizations accounts, supports
  either include or exclude AWS account IDs, and supports region selectors such
  as `all` and glob patterns.
- AWS `accounts`: runs against explicit AWS account IDs in `include`.
- Azure `subscriptions`: runs explicit Azure subscription IDs in `include`, or
  discovers subscriptions when `include` is omitted. `exclude` can remove
  discovered subscriptions. Azure supports universal tasks when an Azure
  runtime session can be built.
- GCP `projects`: runs explicit GCP project IDs in `include`, or discovers
  projects when `include` is omitted. `exclude` can remove discovered projects.
  GCP supports universal tasks when a GCP runtime session can be built.

Azure management group discovery and GCP organization/folder discovery are
intentionally deferred. The schema rejects those modes until provider discovery
support is implemented.

Azure/GCP management group, organization, and folder discovery are still
deferred. Azure/GCP runtime session creation requires the provider's optional
SDKs and credentials; failures are reported as provider-specific runtime errors
without entering AWS auth, session, or account resolution code. Azure/GCP
`provider_options` are passed into the provider runtime session factories.

```yaml
schema_version: 1

accounts:
  - name: azure-explicit-subscriptions
    provider: azure
    mode: subscriptions
    regions:
      - eastus
    include:
      - 11111111-2222-3333-4444-555555555555
    provider_options:
      tenant_id: example-tenant-id
      client_id: example-client-id
      client_secret: example-client-secret
      subscription_id: example-runtime-subscription-id
    tasks:
      - name: noop
```

```yaml
schema_version: 1

accounts:
  - name: gcp-explicit-projects
    provider: gcp
    mode: projects
    regions:
      - us-central1
    include:
      - anvil-dev-project
    provider_options:
      credentials_path: /secure/path/to/credentials.json
      quota_project_id: anvil-billing-project
    tasks:
      - name: noop
```

### Provider task packages

Task compatibility is determined by package location. Existing YAML task names
stay stable where possible; for example, AWS configs can still use
`count_vpc`, which now resolves from `anvil.providers.aws.tasks.count_vpc`.

- `anvil.providers.tasks.<task>` is universal and can run for any provider.
- `anvil.providers.aws.tasks.<task>` is AWS-only.
- `anvil.providers.azure.tasks.<task>` is Azure-only.
- `anvil.providers.gcp.tasks.<task>` is GCP-only.

Provider-owned task plugin entry points use the same compatibility model:

- `anvil.providers.tasks` exposes universal plugin task packages.
- `anvil.providers.aws.tasks` exposes AWS-only plugin task packages.
- `anvil.providers.azure.tasks` exposes Azure-only plugin task packages.
- `anvil.providers.gcp.tasks` exposes GCP-only plugin task packages.

Each task plugin entry point value points at a package containing task modules;
the task module filename is the YAML task name. For example:

```toml
[project.entry-points."anvil.providers.tasks"]
portable = "my_plugin.universal_tasks"

[project.entry-points."anvil.providers.aws.tasks"]
aws-extra = "my_plugin.aws_tasks"
```

Example Azure task configuration:

```yaml
accounts:
  - name: azure-subscriptions
    provider: azure
    mode: subscriptions
    include:
      - 00000000-0000-0000-0000-000000000000
    regions:
      - eastus
    tasks:
      - name: count_resource_groups
```

Example GCP task configuration:

```yaml
accounts:
  - name: gcp-projects
    provider: gcp
    mode: projects
    include:
      - anvil-dev-project
    regions:
      - us-central1
    provider_options:
      credentials_path: /secure/path/to/credentials.json
      quota_project_id: anvil-billing-project
    tasks:
      - name: get_project_info
```

Legacy task plugin entry points under `anvil.tasks` remain unsupported and are
ignored. Plugin authors migrating from `anvil.tasks` should move task modules
into universal or provider-specific task packages and register the matching
`anvil.providers...tasks` entry point group. Direct Python imports should use
`anvil.providers.tasks.<task>` or
`anvil.providers.<provider>.tasks.<task>` for first-party tasks, and the
plugin package path for plugin tasks. Processor plugin entry points are
separate and unchanged.

Duplicate task names across all packages and plugins applicable to the selected
provider are rejected as ambiguous.

The task loader builds a cached descriptor index shaped as
`provider_name -> task_name -> list[TaskDescriptor]`, so resolving multiple
configured tasks does not rebuild discovery descriptors once per task.

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
`--providers` validates the provider contract. `--auth` validates cloud access
for the configured targets.

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
