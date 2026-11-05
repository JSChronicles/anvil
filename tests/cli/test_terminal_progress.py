from __future__ import annotations

import logging
from io import StringIO

import pytest
from rich.console import Console
from rich.logging import RichHandler

from anvil.progress import ProgressEvent, ProgressPhase, ProgressTarget
from anvil.terminal import ProgressMode, TerminalProgress, interactive_progress_enabled


@pytest.mark.parametrize(
    ("mode", "stream_is_terminal", "environment", "expected"),
    [
        (ProgressMode.AUTO, True, {}, True),
        (ProgressMode.AUTO, False, {}, False),
        (ProgressMode.AUTO, True, {"CI": "true"}, False),
        (ProgressMode.AUTO, True, {"TERM": "dumb"}, False),
        (ProgressMode.BAR, False, {"CI": "true"}, True),
        (ProgressMode.OFF, True, {}, False),
    ],
)
def test_interactive_progress_selection(
    mode, stream_is_terminal, environment, expected
):
    assert (
        interactive_progress_enabled(
            mode=mode, stream_is_terminal=stream_is_terminal, environment=environment
        )
        is expected
    )


def _target() -> ProgressTarget:
    return ProgressTarget(
        index=0,
        name="production",
        provider="aws",
        provider_display_name="AWS",
        mode="organization",
    )


def test_terminal_progress_renders_headers_bar_and_completion():
    output = StringIO()
    console = Console(file=output, color_system=None, width=100)
    progress = TerminalProgress(console=console)

    progress.handle(
        ProgressEvent(
            target=_target(), phase=ProgressPhase.EXECUTING, completed=61, total=100
        )
    )
    console.print(progress._render())
    progress.handle(
        ProgressEvent(
            target=_target(),
            phase=ProgressPhase.COMPLETED,
            completed=100,
            total=100,
            duration_seconds=8.2,
        )
    )
    console.print(progress._render())

    rendered = output.getvalue()
    assert rendered.splitlines()[0].strip() == ""
    assert "PROVIDER" in rendered
    assert "TARGET" in rendered
    assert "MODE" in rendered
    assert "PROGRESS" in rendered
    assert "AWS" in rendered
    assert "production" in rendered
    assert "organization" in rendered
    assert "61/100" in rendered
    assert "✓ complete · 8.2s" in rendered


def test_terminal_progress_coordinates_logging_with_live_display():
    output = StringIO()
    console = Console(file=output, color_system=None, force_terminal=True, width=100)
    progress = TerminalProgress(console=console)
    logger = logging.getLogger("tests.terminal-progress")
    logger.handlers = [
        RichHandler(console=console, markup=False, show_time=False, show_path=False)
    ]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    try:
        with progress:
            progress.handle(
                ProgressEvent(target=_target(), phase=ProgressPhase.RESOLVING)
            )
            logger.warning("Region unavailable")
    finally:
        logger.handlers.clear()
        logger.propagate = True

    rendered = output.getvalue()
    assert "Region unavailable" in rendered
    assert "resolving" in rendered
    assert "production" in rendered
