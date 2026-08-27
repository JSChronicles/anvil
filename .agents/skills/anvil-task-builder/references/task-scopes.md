# Task Scope Selection

Use this reference whenever a task is created, changes the provider resources
it touches, changes scope, or is reviewed for duplicate execution.

Task scope defines invocation count and result ownership. It is a correctness
and safety contract, not a concurrency or performance knob. Declare it in the
Python module; task YAML does not select scope.

## Decision Sequence

Ask these questions in order:

1. Can the resource state, API result, or action differ by resolved provider
   location? Use the default `region` scope.
2. Is the resource global to a target but independently owned by every resolved
   execution target? Use `TASK_SCOPE = "target"`.
3. Does the work belong to the original configured target or a shared AWS
   owner/control plane instead of every execution target discovered beneath it?
   Use `TASK_SCOPE = "configured_target"`.
4. Does one module touch resources with different answers? Split it into tasks
   with one ownership boundary each.

Do not choose a broader scope merely to reduce API calls. If repeated work is
the problem but ownership is unchanged, improve pagination, caching, shared
helpers, or task granularity instead.

## Scope Semantics

| Scope | Invocation boundary | Representative resources |
| --- | --- | --- |
| `region` | Once per resolved execution target and concrete location | AWS VPCs, subnets, Lambda functions |
| `target` | Once per resolved execution target | AWS IAM users and policies, Azure resource groups |
| `configured_target` | Once for the original configured YAML target | AWS Organizations and IAM Identity Center control-plane work |

No `TASK_SCOPE` declaration means `region`.

A target-scoped task still receives `region` and a session using the target's
first resolved concrete location. This does not make the provider resource
regional. The task docstring should say whether that location selects an SDK
endpoint, is passed to an API filter, or is ignored by a target-global API.

`configured_target` changes ownership and dependency mapping. It is appropriate
only when one configured target may expand into several execution targets but
the operation must remain attached to the original configuration. It is not a
generic synonym for management account, payer account, or run once.

## Provider Support

Read `ProviderMetadata.supported_task_scopes` for the selected provider rather
than maintaining an allowlist in task code. Stock AWS supports `region`,
`target`, and `configured_target`; the other stock providers currently support
`region` and `target`.

## Safety and Consolidation

For destructive tasks, explicitly verify the expected invocation count for a
configuration with multiple targets and locations. A target-global mutation
left at region scope can be attempted once per configured location.

Consolidate operations only when they share the same ownership boundary as well
as compatible safety, failure, and result semantics. For example, AWS IAM and
IAM Identity Center policies require separate tasks: IAM is account-wide per
resolved target, while Identity Center belongs to a configured-target control
plane.

## Changing an Existing Scope

A scope change can alter:

- invocation count and which target owns each result;
- the `region` and session used by the task;
- dependency fan-out and fan-in shapes;
- retries, actions, and partial-failure behavior;
- whether downstream selectors receive one object or a list.

Review workflows, examples, documentation, and tests that consume the task.
Do not add backward-compatibility behavior unless the user requests it or the
project contract requires it.

## Validation Checklist

- Confirm the provider API's documented ownership boundary.
- Count expected invocations for multiple targets and locations.
- Confirm the provider advertises the declared scope.
- Test destructive tasks cannot repeat against the same global resource.
- Test dependency mapping when connected tasks use different scopes.
- Document scope assumptions and `region` behavior in `run()`.
- Run `uv run anvil validate --tasks` and focused task or runner tests.

Validation can reject unknown or provider-incompatible declarations. It cannot
infer whether the API itself is regional, target-global, or owned by a shared
control plane; review and tests must establish that semantic correctness.
