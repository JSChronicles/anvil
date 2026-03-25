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


## What Anvil provides

### Declarative task execution with dependency ordering

Anvil separates task definition from task execution. Tasks declare what should run and any dependencies between them, and the engine resolves execution order centrally rather than relying on ad hoc sequencing inside task code.

### Multi-organization orchestration with bounded parallel account execution

Anvil can target multiple organizations and execute work across many accounts in parallel. Account-level concurrency is bounded by worker-pool limits so execution remains controlled and predictable rather than unbounded.

### Parallel authentication checks across organizations

Before execution, Anvil can validate authentication across organizations concurrently. This reduces startup time for larger runs and surfaces authentication issues earlier, before task execution begins.

### Optional fail-fast execution and cancellation signaling

Anvil supports fail-fast behavior for cases where one failure should stop additional work. When enabled, the engine signals cancellation and prevents unnecessary downstream execution rather than continuing as though the run were still healthy.

### Structured result aggregation across engine layers

Execution results are collected and rolled up across tasks, accounts, organizations, and the engine as a whole. This gives operators a more consistent view of outcomes than loose per-task logging alone.

### A defined task runtime contract with engine-managed execution context

Tasks run through a consistent interface and receive engine-managed context rather than building their own execution plumbing. This keeps task implementations focused on business logic while the engine handles shared concerns such as session context, metadata, dry-run state, and action recording.


<p align="center">
  <img src="../images/flow-diagram.png" alt="flow-diagram" width="275" height="950">
</p>
