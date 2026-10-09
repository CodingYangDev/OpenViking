# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Shared terminal progress rendering for training and benchmark scripts."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

try:  # Rich remains optional for callers that use plain-text output.
    from rich.console import Console
    from rich.progress import Progress, ProgressColumn, Task, TaskID, TextColumn
    from rich.table import Column
    from rich.text import Text
except ImportError:  # pragma: no cover - minimal installations
    Console = None
    Progress = None
    ProgressColumn = object
    Task = Any
    TaskID = Any
    TextColumn = None
    Column = None
    Text = None


class ThreeStateBarColumn(ProgressColumn):
    """Render successful/failed/running/pending work as a rich progress bar."""

    def __init__(self, bar_width: int = 28) -> None:
        table_column = Column(ratio=1, no_wrap=True) if Column is not None else None
        super().__init__(table_column=table_column)
        self.bar_width = bar_width

    def render(self, task: Task) -> Text:
        bar_width = self.bar_width or 40
        total = max(int(task.total or 0), 0)
        processed = max(int(task.completed or 0), 0)
        failed = max(int(task.fields.get("failed", 0) or 0), 0)
        succeeded = max(int(task.fields.get("succeeded", processed - failed) or 0), 0)
        running = max(int(task.fields.get("running", 0) or 0), 0)

        if total <= 0:
            return Text("─" * bar_width, style="bar.back")

        succeeded = min(succeeded, total)
        failed = min(failed, total - succeeded)
        running = min(running, total - succeeded - failed)
        succeeded_width = int(bar_width * succeeded / total)
        failed_width = int(bar_width * (succeeded + failed) / total) - succeeded_width
        running_width = (
            int(bar_width * (succeeded + failed + running) / total) - succeeded_width - failed_width
        )
        pending_width = bar_width - succeeded_width - failed_width - running_width

        bar = Text()
        if succeeded_width > 0:
            bar.append("█" * succeeded_width, style="green")
        if failed_width > 0:
            bar.append("█" * failed_width, style="red")
        if running_width > 0:
            bar.append("▓" * running_width, style="yellow")
        if pending_width > 0:
            bar.append("░" * pending_width, style="dim")
        return bar


class ProgressSummaryColumn(ProgressColumn):
    """Render processed count plus non-zero active/failure counts."""

    def render(self, task: Task) -> Text:
        failed = max(int(task.fields.get("failed", 0) or 0), 0)
        running = max(int(task.fields.get("running", 0) or 0), 0)

        summary = Text("(")
        summary.append(f"{int(task.completed or 0)}/{int(task.total or 0)}")
        if failed > 0:
            summary.append(", ")
            summary.append(f"{failed} failed", style="bold red")
        if running > 0:
            summary.append(", ")
            summary.append(f"{running} running", style="bold yellow")
        summary.append(")")
        return summary


def format_duration(seconds: float) -> str:
    """Format a duration as compact h/m/s text."""
    total_seconds = max(0, int(round(seconds)))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


class ElapsedTimeColumn(ProgressColumn):
    """Show stage wall time, frozen when work finishes or the display stops."""

    def render(self, task: Task) -> Text:
        elapsed = task.finished_time if task.finished else task.elapsed
        return Text(f"elapsed: {format_duration(elapsed or 0)}", style="dim")


class _StageProgress(Progress if Progress is not None else object):
    """Tie task clocks to the display/context lifecycle, including error exits."""

    def start(self) -> None:
        for task in self.tasks:
            self.start_task(task.id)
        super().start()

    def stop(self) -> None:
        for task in self.tasks:
            if task.started and task.stop_time is None:
                self.stop_task(task.id)
        super().stop()


def make_three_state_progress(
    description: str = "Progress",
    console: Console | None = None,
    transient: bool = False,
    *,
    total: int = 0,
    get_time: Callable[[], float] | None = None,
) -> tuple[Progress, TaskID]:
    """Create the shared four-state display and its initially unstarted task.

    Set the total/counts before calling ``start()`` or entering the context.
    Construction does not start the clock; ``stop()`` freezes partial work too.
    """
    if Progress is None:
        raise RuntimeError("Rich is required for terminal progress rendering")
    progress = _StageProgress(
        ThreeStateBarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        ProgressSummaryColumn(),
        ElapsedTimeColumn(),
        console=console or Console(stderr=True, soft_wrap=False),
        transient=transient,
        get_time=get_time,
    )
    task_id = progress.add_task(
        description, start=False, total=total, running=0, succeeded=0, failed=0
    )
    return progress, task_id
