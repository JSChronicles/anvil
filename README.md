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

Anvil is a declarative provider-aware execution engine for running Python tasks across cloud and service target fleets. Describe the work in YAML, keep task logic in plain Python modules, and let the engine handle authentication, target resolution, dependency ordering, bounded concurrency, and structured results. The current runtime supports AWS, Azure, Cloudflare, Datadog, GCP, GitHub, GitLab, and PagerDuty through the same provider-neutral contracts.

For complete Anvil documentation, see the
[Anvil documentation hub](https://opsfoundry.dev/anvil/).

Key references:

- [Configuration](https://opsfoundry.dev/anvil/configuration/)
- [Providers](https://opsfoundry.dev/anvil/providers/) and
  [provider profiles](https://opsfoundry.dev/anvil/provider-profiles/)
- [Built-in tasks and processors](https://opsfoundry.dev/anvil/built-in-components/)
- [Task contract](https://opsfoundry.dev/anvil/task-contract/) and
  [task workflows](https://opsfoundry.dev/anvil/task-workflows/)
- [CLI and results](https://opsfoundry.dev/anvil/cli/)
- [Extension best practices](https://opsfoundry.dev/anvil/extension-best-practices/)
- [Complete examples](https://opsfoundry.dev/anvil/examples/)

### Why Anvil?

Anvil is built for teams that need repeatable cloud workflows, such as inventory, validation, enforcement, cleanup, and reporting, to run consistently across provider targets and regions.

- Declarative orchestration
  - Define execution in reusable YAML instead of one-off scripts.
  - Configure provider targets, regions, tasks, task dependencies, dry runs, fail-fast behavior, and concurrency in one place.
- Multi-target by default
  - AWS can discover active organization accounts and enabled regions, with include/exclude filtering.
  - Azure subscriptions and GCP projects can run from explicit IDs or provider
    discovery.
  - Cloudflare preserves account and zone boundaries, GitLab preserves group and
    project boundaries, and Datadog and PagerDuty execute at organization or
    account scope without forcing cloud-specific hierarchy concepts.
- Parallel execution and caching
  - Control concurrency at the target, account, and region levels. See [Caching and reuse](https://opsfoundry.dev/anvil/execution-model/#cache-and-reuse-boundaries).
- Shared discovery and session reuse
  - Validate targets, discover supported provider metadata, and reuse session/runtime state before execution.
- Task isolation
  - Write tasks as simple Python files with a `run(...)` function.
- Built-in tasks
  - Use provider-package tasks for common AWS operations and universal tasks
    where they apply.
  - Provider-owned task package entry points can add universal tasks or tasks
    for any discovered provider.
- Structured output and safer operations
  - Record structured results at task, account/target, target group, and engine levels.

## Usage
> [!TIP]
> It is recommended to use the [foundry-anvil-template](https://github.com/JSChronicles/foundry-anvil-template).
>
> The template exposes project-local processors without forking Anvil.
>
> If you do not need/want the full Anvil framework and only want a simple starting point for small AWS Organization tasks, see: [`templates/aws_multi_account_template.py`](https://opsfoundry.dev/anvil/examples/#standalone-aws-multi-account-template)


1. Install Anvil with the provider SDKs you need:
   1. Installed package users can choose provider extras with pip. Base installs
      include AWS support and the default CLI behavior:
      `uv pip install anvil`
   1. All other providers require users to install via extras:
      `uv pip install "anvil[xxxx]"`, so like `uv pip install "anvil[azure]"`
   1. Install every optional provider dependency with:
      `uv pip install "anvil[all]"`
   1. Source checkout users should sync the matching uv extra instead:
      `uv sync --extra <provider>`, or use `uv sync --extra all` for every provider
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
anvil list      # List available tasks, processors, and providers
anvil validate  # Inspect environment health or run focused validation checks
anvil run       # Execute YAML-defined workflows
anvil results   # Query JSONL results and rerun failures
```

Example AWS task configuration:

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
```

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

See [more provider configurations](https://opsfoundry.dev/anvil/examples/) for
simple, multi-target, include/exclude, and advanced YAML files for every
provider.


------------------------------


### Provider profiles

Anvil provider profiles are optional. Cloudflare, Datadog, GitHub, GitLab, and
PagerDuty can load reusable settings from `~/.anvil/config.toml`; set
`ANVIL_CONFIG` to use a different file. AWS, Azure, and GCP continue to use
their provider-native credential configuration instead.

When neither a named profile nor inline profile fields are configured, Anvil
first applies `providers.<provider>.default` when that table exists. If it does
not exist, the provider uses its normal environment variables, SDK credential
chain, workload identity, or other native fallback where supported. Profiles
are namespaced by provider and profile name:

```toml
[providers.cloudflare.security]
api_token_env = "CLOUDFLARE_SECURITY_TOKEN"

[providers.github.work]
token_env = "GITHUB_WORK_TOKEN"
api_url = "https://api.github.com"

[providers.gitlab.default]
token_env = "GITLAB_TOKEN"
url = "https://gitlab.example.com"
```

These fields identify environment variables or provider-native credential
locations; they do not contain the credentials themselves. Set the referenced
variables through your shell, CI secret store, or runtime environment.

Select a named profile with `provider.options.profile`. A profile named
`default` is selected automatically when the target does not provide inline
authentication or connection options. The following target fragments omit
unrelated fields with `# ...`.

Cloudflare uses the named `security` profile while keeping its resource
selector inline:

```yaml
provider:
  name: cloudflare
  mode: zones
  options:
    profile: security
    account_id: '11111111111111111111111111111111'
# ...
```

GitHub references the named `work` profile:

```yaml
provider:
  name: github
  mode: repositories
  options:
    profile: work
# ...
```

GitLab uses `providers.gitlab.default` because no profile or inline connection
fields are configured:

```yaml
provider:
  name: gitlab
  mode: projects
  options: {}
# ...
```

A named profile cannot be combined with inline profile fields such as
`token_env`, `api_url`, or `url`. Resource selectors that do not belong to the
profile remain inline, as shown by Cloudflare's `account_id`. Each provider
continues to validate its own supported profile and target options.

See [Provider profiles](https://opsfoundry.dev/anvil/provider-profiles/) for
default-profile behavior, supported fields, and multi-account or multi-endpoint
patterns.

### Provider task packages
> [!NOTE]
> Duplicate task names across all packages and plugins applicable to the selected provider are rejected as ambiguous.
>

Task compatibility is determined by package location.

See the [built-in component catalog](https://opsfoundry.dev/anvil/built-in-components/)
for the tasks shipped by each provider. For invocation IDs, task-to-task result
sharing, `always_run`, partial recovery results, and scope-aware dependencies,
see [Task workflows](https://opsfoundry.dev/anvil/task-workflows/).

- `anvil.providers.tasks.<task>` is universal and can run for any provider.
- `anvil.providers.<provider>.tasks.<task>` runs only for the provider named by
  that package segment.

Use target `dry_run: true` to review planned removals before execution.


### Extension package discovery

Tasks, processors, and providers are discovered from package folders. Adding a
public module or provider folder to an already registered package does not
require another entry-point declaration. Discovery records names and sources
without importing child implementations; normal execution imports only the
selected components. Duplicate names are rejected as ambiguous and report every
conflicting source.

See [Extension best practices](https://opsfoundry.dev/anvil/extension-best-practices/)
for package layouts, current entry-point groups, provider factories, and
implementation guidance. See the
[Task contract](https://opsfoundry.dev/anvil/task-contract/#task-packages-and-discovery)
for task compatibility, lazy discovery, and ambiguity behavior.

The [Provider reference](https://opsfoundry.dev/anvil/providers/) documents all
stock provider modes, authentication options, target selectors, locations, and
validation behavior and includes a focused configuration example for every
provider.

See [Selectors and regions](https://opsfoundry.dev/anvil/selectors-and-regions/)
for exact `include`, `exclude`, `all`, glob, `management`, and `payer` rules.

For [delegated-administrator patterns](https://opsfoundry.dev/guides/aws/programmatic-account-access/#delegated-admin-security-access),
keep the base session on the delegated-admin profile. Anvil uses that base
session directly for the delegated-admin account if it appears in Organizations
discovery, and assumes `role_name` in every other selected account, including
the management/payer account. AWS organization targets accept `management` and
`payer` as case-insensitive aliases for that account in `include` and `exclude`
filters.

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
    include:
      - management
    tasks:
      - name: noop
```

------------------------------


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

See more at [Task validation](https://opsfoundry.dev/anvil/task-contract/#validation).

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

To build a custom processor, see
[Extension best practices](https://opsfoundry.dev/anvil/extension-best-practices/#build-a-processor).


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
