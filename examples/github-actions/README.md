# GitHub Actions enterprise OIDC example

This directory contains a complete GitHub Actions workflow for running Anvil
across multiple AWS Organizations with separate AWS credential profiles:

- [anvil-multi-oidc-run.yaml](./anvil-multi-oidc-run.yaml)
- [noop-multi-org.yaml](./noop-multi-org.yaml)
- [anvil-single-oidc-run.yaml](./anvil-single-oidc-run.yaml)
- [noop-single-oidc.yaml](./noop-single-oidc.yaml)

The workflow is stored outside `.github/workflows` on purpose. Copy it into
`.github/workflows/` in the repository that should run Anvil.

## What it does

There are two workflow patterns in this directory.

### Single OIDC Role

[anvil-single-oidc-run.yaml](./anvil-single-oidc-run.yaml) is the standard
GitHub OIDC pattern. It configures one AWS role with
`aws-actions/configure-aws-credentials`, exports short-lived AWS credentials into
the job environment, and runs Anvil with a config that does not set `profile`.

Use this when one AWS entry role can reach every organization and account Anvil
needs to process.

### Multiple OIDC Profiles

[anvil-multi-oidc-run.yaml](./anvil-multi-oidc-run.yaml) configures one
named AWS profile per AWS Organization before running Anvil once against a
multi-org config.

Use this when different AWS Organizations require different initial OIDC entry
roles.

Plain GitHub OIDC credentials are normally exported as environment variables,
which gives the Anvil process one active AWS identity. That works when one entry
role can reach every configured AWS Organization. This example uses
`aws-profile` instead because each organization has its own entry role. The
workflow authenticates each profile through GitHub OIDC, and Anvil selects the
right profile through its native per-organization `profile` field. That keeps
credentials explicit while still letting Anvil own parallel organization
execution through `max_parallel_targets`.

## Credential Profiles

The workflow creates these AWS profiles:

```yaml
- name: Configure production AWS profile
  uses: aws-actions/configure-aws-credentials@ec61189d14ec14c8efccab744f656cffd0e33f37 # v6.1.0
  with:
    role-to-assume: arn:aws:iam::111111111111:role/AnvilExecutionRole
    role-session-name: anvil-production-${{ github.run_id }}-${{ github.run_attempt }}
    aws-region: us-east-1
    aws-profile: production
    overwrite-aws-profile: true
```

Add or remove profile setup steps to match the AWS Organizations you want Anvil
to run. Profile names must match the `profile` values in the Anvil YAML.

The workflow sets `AWS_SHARED_CREDENTIALS_FILE` and `AWS_CONFIG_FILE` to files
under `runner.temp` before configuring profiles. That keeps the named profiles
out of the runner user's shared `~/.aws` files. A final `Clean up AWS profile
files` step deletes those temp files with `if: always()`. Keep self-hosted runner
environments isolated as well, because hard cancellation or runner failure can
still prevent cleanup steps from running.

## Required GitHub settings

The workflow needs:

- `permissions.id-token: write` so GitHub can mint OIDC tokens for AWS.
- A protected GitHub environment such as `aws-production` when the workflow can
  run against production AWS Organizations.

The manual workflow input uses the GitHub `environment` input type so operators
can choose the approved environment at dispatch time.

## Required AWS setup

Create a GitHub OIDC identity provider in each AWS account that owns an Anvil
entry role, then allow only the intended repository and branch or environment to
assume it.

Example trust policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
          "token.actions.githubusercontent.com:sub": "repo:YOUR_ORG/YOUR_REPO:environment:aws-production"
        }
      }
    }
  ]
}
```

Each Anvil entry role also needs permissions for organization discovery and any
configured tasks in that AWS Organization. For organization-wide execution, it
usually needs permission to assume the member-account role configured in Anvil
YAML, for example `OrganizationAccountAccessRole`.

## Anvil config shape

For the single-OIDC workflow, leave `profile` unset so boto3 uses the
environment credentials exported by `aws-actions/configure-aws-credentials`:

```yaml
schema_version: 1
max_parallel_targets: 1

organizations:
  - name: single-oidc-org
    regions:
      - us-east-1
    role_name: OrganizationAccountAccessRole
    max_workers: 10
    fail_fast: false
    dry_run: true
    tasks:
      - name: noop
```

The included no-op config has multiple organizations in one file. Each
organization points at one named AWS profile:

```yaml
schema_version: 1
max_parallel_targets: 3

organizations:
  - name: production
    profile: production
    regions:
      - us-east-1
    role_name: OrganizationAccountAccessRole
    max_workers: 10
    fail_fast: false
    dry_run: true
    tasks:
      - name: noop

  - name: security
    profile: security
    regions:
      - us-east-1
    role_name: OrganizationAccountAccessRole
    max_workers: 10
    fail_fast: false
    dry_run: true
    tasks:
      - name: noop
```

The `noop` task exercises auth, AWS Organizations discovery, member account role
assumption, result writing, and artifact upload without changing AWS resources.

Anvil writes run output under `./results`, including one full target JSON file
per target and one `*-target-summary.json` file per config. The workflow uploads
those JSON files even when the run fails.

Scheduled runs default to dry-run mode because `workflow_dispatch` inputs are
not present for scheduled events. Manual runs use the typed `dry_run` boolean
input.
