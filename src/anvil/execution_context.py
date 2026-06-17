from __future__ import annotations

import threading
from dataclasses import dataclass, field

from anvil.task_loader import ResolvedTask


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """
    Immutable execution configuration shared across org and account execution.
    """

    regions: list[str]
    role_name: str | None
    dry_run: bool
    tasks: list[ResolvedTask]
    metadata: dict[str, object]

    fail_fast: bool = False
    max_parallel_regions: int = 1
    assume_role_in_management: bool = False
    benchmark_enabled: bool = False
    log_level: str | int | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
