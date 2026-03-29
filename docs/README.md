# anvil in-depth

<a name="readme-top"></a>


<!-- PROJECT LOGO -->
<br />
<div align="center">
    <img src="../images/logo.png" alt="Logo" width="256" height="256">
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

## Execution model

Anvil executes declarative task workflows across one or more AWS organizations, across many accounts within each organization, and across one or more configured AWS regions.

At a high level:

1. Each organization is defined independently in configuration.
2. Each organization can declare its own profile, role, regions, worker limits, task graph, and fail-fast behavior.
3. For each organization, Anvil authenticates, discovers eligible accounts, applies include or exclude filters, validates configured regions against enabled regions, and then executes tasks for each selected account.
4. Within an account, tasks execute in dependency order for each configured region.
5. Results are captured at task, account, organization, and engine scope.

This makes Anvil suitable for workflows that need consistent execution across multiple AWS organizations while still respecting region-specific service presence, account boundaries, and per-organization execution settings.

### Multi-organization execution

Anvil supports defining multiple organizations in a single run. Each organization is treated as an independent execution context with its own:

- AWS profile
- target regions
- role name
- include or exclude account filters
- worker concurrency
- dry-run behavior
- fail-fast setting
- task definitions
- metadata

This allows a single execution to coordinate work across separate AWS environments without forcing them into a shared credential model or shared runtime configuration.

### Multi-region execution

Within each organization, Anvil can execute tasks across multiple configured AWS regions.

Configured regions are treated as part of the execution scope rather than as a single global default. During organization startup, Anvil validates the configured region list against the regions enabled for that organization and only executes in the effective configured regions that remain after validation.

Task execution then occurs per account and per region, and task results include the region they ran in. This makes region-specific inventory, validation, enforcement, and reporting workflows easier to reason about and easier to audit later from structured output.


## Authentication validation

Anvil includes an authentication check mode that validates AWS access for each configured organization before account-level task execution begins. This helps catch expired credentials, missing profiles, access issues, or invalid SSO sessions early.
Authentication checks run concurrently across organizations through a small bounded worker pool. Anvil currently validates up to **4 organizations at a time**, which reduces startup latency while keeping concurrency controlled.

### What auth check does

For each configured organization, Anvil:

1. Infers the likely authentication source.
2. Creates a boto3 session.
3. Calls AWS STS `GetCallerIdentity`.
4. Records a structured result with status, source, timing, message, and optional remediation guidance.

`auth check` is a lightweight preflight validation step, not a full execution run.

### Supported authentication-source detection

Anvil can currently classify authentication as:

- **SSO**
- **Profile static**
- **Profile assume role**
- **Environment**
- **OIDC**
- **Unknown**

This source classification is informational, but it improves failure reporting and remediation guidance.

### Common checks and error meanings

Auth check normalizes several common authentication problems into clearer messages, including:

- **AWS profile not found**
- **No AWS credentials available**
- **AWS SSO session is invalid or expired**
- **AWS credentials have expired**
- **Access denied when calling AWS**
- **Unexpected error during authentication**

Where possible, Anvil also includes remediation guidance such as re-running SSO login for the affected profile.


## Task validation

Anvil includes a task validation mode that checks discovered tasks for structural correctness without executing them. This helps catch task-definition issues before a run begins.

### What task validation does

Task validation verifies:

1. the task has a valid non-empty name
2. the task exposes a callable `run(...)` entrypoint
3. the `run(...)` signature includes the required runtime parameters
4. the task does not use unsupported positional-only parameters
5. duplicate task names are rejected

Because this validation is structural, it does not perform AWS calls or execute task logic.

### Runtime contract expectations

Tasks are expected to expose a `run(...)` function compatible with the engine-managed execution contract.

Anvil currently requires support for these parameters:

- `account_id`
- `account_alias`
- `session`
- `dry_run`
- `metadata`



## Flow
<p align="center">
  <img src="../images/flow-diagram.png" alt="flow-diagram" width="275" height="950">
</p>
