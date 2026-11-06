"""Interactive terminal progress rendering for the Anvil CLI."""

from __future__ import annotations

import os
import sys
import threading
from dataclasses import dataclass
from enum import StrEnum
from types import TracebackType

from rich.columns import Columns
from rich.console import Console, ConsoleRenderable
from rich.live import Live
from rich.padding import Padding
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from anvil.progress import ProgressEvent, ProgressPhase, ProgressTarget


class ProgressMode(StrEnum):
    """Supported CLI progress presentation modes."""

    AUTO = "auto"
    BAR = "bar"
    OFF = "off"


@dataclass(slots=True)
class _TargetProgressState:
    """Latest render state for one configured target."""

    target: ProgressTarget
    phase: ProgressPhase
    completed: int = 0
    total: int | None = None
    duration_seconds: float | None = None

    @classmethod
    def from_event(cls, event: ProgressEvent) -> _TargetProgressState:
        """Build render state from a progress event."""

        return cls(
            target=event.target,
            phase=event.phase,
            completed=event.completed,
            total=event.total,
            duration_seconds=event.duration_seconds,
        )

    def update(self, event: ProgressEvent) -> None:
        """Apply the latest event values to this target state."""

        self.target = event.target
        self.phase = event.phase
        self.completed = event.completed
        self.total = event.total
        self.duration_seconds = event.duration_seconds


def interactive_progress_enabled(
    *,
    mode: ProgressMode,
    stream_is_terminal: bool | None = None,
    environment: dict[str, str] | None = None,
) -> bool:
    """Return whether Anvil should render interactive progress.

    Args:
        mode: Requested progress presentation mode.
        stream_is_terminal: Optional terminal capability override for tests.
        environment: Optional environment override for tests.

    Returns:
        True when the Rich live table should be enabled.
    """

    if mode is ProgressMode.OFF:
        return False
    if mode is ProgressMode.BAR:
        return True

    effective_environment = os.environ if environment is None else environment
    ci_enabled = effective_environment.get("CI", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    terminal_is_dumb = effective_environment.get("TERM", "").strip().lower() == "dumb"
    effective_stream_is_terminal = (
        sys.stderr.isatty() if stream_is_terminal is None else stream_is_terminal
    )
    return effective_stream_is_terminal and not ci_enabled and not terminal_is_dumb


def _duration_text(duration_seconds: float | None) -> str:
    """Format an optional target duration for terminal display."""

    if duration_seconds is None:
        return ""
    return f" · {duration_seconds:.1f}s"


def _progress_bar(*, completed: int, total: int, width: int = 12) -> Text:
    """Render a compact, fixed-width progress bar and counter."""

    bounded_total = max(total, 0)
    bounded_completed = min(max(completed, 0), bounded_total)
    filled_width = (
        0 if bounded_total == 0 else round(width * bounded_completed / bounded_total)
    )
    bar = Text("━" * filled_width, style="bright_blue")
    bar.append("━" * (width - filled_width), style="grey37")
    bar.append(f" {bounded_completed}/{bounded_total}", style="bold")
    return bar


def _status_cell(state: _TargetProgressState) -> ConsoleRenderable:
    """Render the progress cell for one configured target."""

    if state.phase is ProgressPhase.QUEUED:
        return Text("○ queued", style="dim")
    if state.phase is ProgressPhase.PREPARING:
        return Columns(
            [Spinner("dots", style="cyan"), Text("preparing…", style="cyan")],
            padding=(0, 1),
            expand=False,
        )
    if state.phase is ProgressPhase.RESOLVING:
        return Columns(
            [Spinner("dots", style="magenta"), Text("resolving…", style="magenta")],
            padding=(0, 1),
            expand=False,
        )
    if state.phase is ProgressPhase.COMPLETED:
        return Text(
            f"✓ complete{_duration_text(state.duration_seconds)}", style="green"
        )
    if state.phase is ProgressPhase.FAILED:
        return Text(f"✗ failed{_duration_text(state.duration_seconds)}", style="red")
    if state.phase is ProgressPhase.CANCELLED:
        return Text(
            f"⚠ cancelled{_duration_text(state.duration_seconds)}", style="yellow"
        )
    if state.total is None:
        return Text("executing…", style="bright_blue")
    return _progress_bar(completed=state.completed, total=state.total)


class TerminalProgress:
    """Thread-safe Rich live table for configured target progress."""

    def __init__(self, *, console: Console) -> None:
        """Initialize a terminal progress renderer.

        Args:
            console: Shared Rich console also used by the logging handler.
        """

        self.console = console
        self._lock = threading.RLock()
        self._states: dict[int, _TargetProgressState] = {}
        self._live: Live | None = None

    def __enter__(self) -> TerminalProgress:
        """Start the live progress display."""

        with self._lock:
            self._states.clear()
            self._live = Live(
                self._render(),
                console=self.console,
                refresh_per_second=10,
                transient=False,
            )
            self._live.start(refresh=True)
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Stop the live progress display and restore the terminal."""

        del exception_type, exception, traceback
        with self._lock:
            if self._live is not None:
                self._live.stop()
                self._live = None

    def handle(self, event: ProgressEvent) -> None:
        """Apply a runner progress event and refresh the live table."""

        with self._lock:
            state = self._states.get(event.target.index)
            if state is None:
                self._states[event.target.index] = _TargetProgressState.from_event(
                    event
                )
            else:
                state.update(event)
            if self._live is not None:
                self._live.update(self._render(), refresh=True)

    def _render(self) -> ConsoleRenderable:
        """Build the current progress table in configured target order."""

        table = Table(
            box=None,
            pad_edge=False,
            show_edge=False,
            collapse_padding=True,
            header_style="dim cyan",
        )
        table.add_column("PROVIDER", min_width=10, no_wrap=True)
        table.add_column("TARGET", min_width=16, overflow="ellipsis", no_wrap=True)
        table.add_column(
            "MODE", min_width=16, style="bright_blue", overflow="ellipsis", no_wrap=True
        )
        table.add_column("PROGRESS", min_width=24, no_wrap=True)

        for target_index in sorted(self._states):
            state = self._states[target_index]
            table.add_row(
                state.target.provider_display_name,
                state.target.name,
                state.target.mode,
                _status_cell(state),
            )
        return Padding(table, (1, 0, 0, 0))
