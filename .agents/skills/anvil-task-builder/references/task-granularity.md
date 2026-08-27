# Task Granularity And Performance

Before creating or splitting inventory tasks, consider whether related data should be gathered together. Task boundaries are useful for reuse and clear failure semantics, but each task may repeat AWS client setup and list/describe calls inside the same account-region.

Prefer separate tasks when:

- The resources have different task scopes or ownership boundaries.
- The tasks have different safety profiles, especially read-only vs mutating.
- They are commonly run independently.
- They need different failure, cleanup, or dependency behavior.
- A dependency relationship is meaningful to the workflow.
- Combining them would make the result shape confusing or too broad.

Consider one combined inventory task when:

- The resources share the same task scope and ownership boundary.
- The tasks are read-only.
- They query the same AWS service in the same account-region.
- They repeat the same list or describe operations.
- Their outputs are normally consumed together.
- Performance matters more than task-level granularity.

For example, VPCs, VPC endpoints, and subnets are all EC2 regional inventory. If the goal is a network inventory report, a single task can create one EC2 client, gather the related data once, and share in-memory results instead of having multiple tasks rediscover overlapping state.

When overlap is present but separate tasks still make sense, prefer extracting small shared helper functions over duplicating AWS pagination logic.

For benchmarking or heavy inventory tasks, consider metadata such as `include_details: false` so users can return counts and timings without writing large result payloads.

## Region Concurrency

When suggesting YAML concurrency, treat `max_parallel_regions` as a targeted tool rather than a default speed knob.

It is most likely to help tasks with meaningful per-region runtime, such as long paginated scans or slow regional service checks. It can also help a multi-task regional workflow when the tasks hit different AWS services, because the work is less concentrated on one service API path.

For lightweight describe/list inventory across many accounts, especially multiple tasks that all call the same service, prefer `max_parallel_regions: 1` first because total pressure grows as:

```text
max_parallel_targets * max_workers * max_parallel_regions
```

Recommend benchmarking the actual task mix before raising region concurrency.

This caution assumes multiple accounts are in scope for the run. If the
workflow's target set is only the payer/management account,
`max_parallel_targets` is fixed at 1 and there is no cross-account
contention to protect against, so there is more headroom for concurrency.
See `references/payer-management-account-tasks.md`.
