# SARIF Detection Tasks

Use this reference when creating Anvil tasks that emit SARIF-compatible findings
for the stock `sarif_report` processor.

## Naming

SARIF-producing Anvil tasks MUST use the `detect_` prefix:

```text
detect_<domain>_<condition>
```

Examples:

```text
detect_deprecated_lambda_runtimes
detect_iam_inline_policies
detect_security_groups_open_to_world
detect_stale_iam_access_keys
```

Do not use `detect_` for plain inventory. Use `list_`, `get_`, `count_`, or
`compare_` when the task does not produce actionable findings.

## Required Result Shape

Return this first-party pattern:

```python
return {
    "checked_count": checked_count,
    "finding_count": len(sarif_findings),
    "sarif_findings": sarif_findings,
}
```

`sarif_findings` must be a list. Return an empty list when no findings are
detected. The `sarif_report` processor consumes only `result.sarif_findings`;
it does not infer findings from arbitrary task output.

## Finding Shape

Each finding must include:

```python
{
    "rule": {
        "id": "aws.service.condition",
        "name": "Short rule name",
        "short_description": "One-sentence description.",
        "full_description": "Longer explanation of the condition.",
        "help_markdown": "Actionable remediation guidance.",
        "level": "warning",
        "security_severity": "6.0",
        "precision": "high",
        "tags": ["security", "aws"],
    },
    "message": "Resource-specific finding message.",
    "locations": [
        {
            "uri": "anvil/aws/123456789012/us-east-1/service/type/name.json",
            "message": "Resource display name",
            "properties": {
                "aws_account_id": "123456789012",
                "aws_region": "us-east-1",
                "aws_arn": "arn:aws:...",
            },
        }
    ],
    "fingerprint": "stable-rule-account-region-resource-condition",
    "properties": {
        "resource_name": "example",
    },
}
```

Allowed `level` values are `none`, `note`, `warning`, and `error`.
`security_severity` should be a string score from `0.1` to `10.0` when the
finding is security-related.

## Rules

- Keep `rule.id` stable and do not include account, region, or resource IDs.
- Use a stable `fingerprint` derived from rule ID, account ID, region, resource
  identity, and the condition being reported.
- Put account, region, ARN, and service-specific identifiers in `properties`.
- Include remediation in `rule.help_markdown`.
- Prefer AWS ARNs or immutable resource IDs over names alone.
- Let the Anvil engine provide execution context; repeat account/region only when
  needed for SARIF location properties or fingerprint stability.
- Do not generate SARIF JSON inside tasks. Return Anvil findings and let
  `sarif_report` own SARIF formatting.

## Workflow

1. Create the task in the right package for its ownership:
   - Universal stock Anvil tasks live under
     `src/anvil/providers/tasks/detect_*.py`.
   - Provider-specific stock Anvil tasks live under
     `src/anvil/providers/<provider>/tasks/detect_*.py`.
   - Third-party, plugin, or project-local tasks live in the plugin task package
     and must be exposed through the matching provider-owned task entry-point
     group: `anvil.providers.tasks`, `anvil.providers.aws.tasks`,
     `anvil.providers.azure.tasks`, or `anvil.providers.gcp.tasks`.
2. Implement the normal Anvil keyword-only `run()` contract.
3. Validate metadata before AWS calls.
4. Use paginators for AWS list APIs.
5. Build `sarif_findings` only for actionable findings.
6. Return `checked_count`, `finding_count`, and `sarif_findings`.
7. Validate task and processor discovery after implementation.

Use `src/anvil/providers/aws/tasks/detect_deprecated_lambda_runtimes.py` as the
reference implementation for a first-party SARIF-capable detection task.
