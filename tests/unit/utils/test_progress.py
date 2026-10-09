"""Shared progress rendering and lifecycle tests."""

from __future__ import annotations

from io import StringIO

import pytest
from rich.console import Console
from rich.progress import ProgressColumn, TextColumn

from openviking.utils.progress import (
    ElapsedTimeColumn,
    ProgressSummaryColumn,
    ThreeStateBarColumn,
    format_duration,
    make_three_state_progress,
)


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(-1, "0s"), (0, "0s"), (59, "59s"), (60, "1m00s"), (62, "1m02s"), (3723, "1h02m03s")],
)
def test_format_duration(seconds, expected):
    assert format_duration(seconds) == expected


def test_factory_uses_shared_columns_and_initial_counts():
    progress, task_id = make_three_state_progress(total=4)
    assert [type(column) for column in progress.columns] == [
        ThreeStateBarColumn,
        TextColumn,
        ProgressSummaryColumn,
        ElapsedTimeColumn,
    ]
    task = progress.tasks[0]
    assert task.id == task_id
    assert task.total == 4
    assert task.completed == 0
    assert task.fields == {"running": 0, "succeeded": 0, "failed": 0}
    assert task.start_time is None
    assert ElapsedTimeColumn().render(task).plain == "elapsed: 0s"


def test_three_state_bar_initializes_and_renders_four_states():
    column = ThreeStateBarColumn(bar_width=8)
    assert isinstance(column, ProgressColumn)
    assert hasattr(column, "_renderable_cache")
    progress, task_id = make_three_state_progress(total=4)
    progress.update(task_id, completed=2, succeeded=1, failed=1, running=1)
    assert column.render(progress.tasks[0]).plain == "████▓▓░░"
    assert [span.style for span in column.render(progress.tasks[0]).spans] == [
        "green",
        "red",
        "yellow",
        "dim",
    ]


@pytest.mark.parametrize("failed", [False, True])
def test_elapsed_starts_on_context_entry_and_freezes_on_exit(failed):
    now = [100.0]
    progress, _ = make_three_state_progress(
        total=2,
        console=Console(file=StringIO()),
        get_time=lambda: now[0],
    )
    progress.live.auto_refresh = False
    task = progress.tasks[0]
    column = ElapsedTimeColumn()
    now[0] = 200.0
    try:
        with progress:
            assert column.render(task).plain == "elapsed: 0s"
            now[0] = 262.0
            assert column.render(task).plain == "elapsed: 1m02s"
            if failed:
                raise RuntimeError("interrupted")
    except RuntimeError as exc:
        assert failed and str(exc) == "interrupted"
    now[0] = 400.0
    assert column.render(task).plain == "elapsed: 1m02s"
    progress.stop()
    assert column.render(task).plain == "elapsed: 1m02s"


def test_completed_task_freezes_elapsed_including_failed_items():
    now = [100.0]
    progress, task_id = make_three_state_progress(
        total=2, console=Console(file=StringIO()), get_time=lambda: now[0]
    )
    progress.live.auto_refresh = False
    with progress:
        now[0] = 103.0
        progress.update(task_id, completed=2, succeeded=1, failed=1)
        now[0] = 110.0
        assert ElapsedTimeColumn().render(progress.tasks[0]).plain == "elapsed: 3s"
    assert ProgressSummaryColumn().render(progress.tasks[0]).plain == "(2/2, 1 failed)"


def test_empty_stage_starts_and_stops_without_division_errors():
    now = [100.0]
    progress, _ = make_three_state_progress(
        console=Console(file=StringIO()), get_time=lambda: now[0]
    )
    progress.live.auto_refresh = False
    with progress:
        assert list(progress.get_renderables())
        assert ElapsedTimeColumn().render(progress.tasks[0]).plain == "elapsed: 0s"
    now[0] = 200.0
    assert ElapsedTimeColumn().render(progress.tasks[0]).plain == "elapsed: 0s"


def test_duration_formatting_does_not_require_optional_rich(monkeypatch):
    import builtins
    import runpy
    from pathlib import Path

    from openviking.utils import progress as progress_ui

    original_import = builtins.__import__

    def without_rich(name, *args, **kwargs):
        if name == "rich" or name.startswith("rich."):
            raise ModuleNotFoundError("Rich is not installed")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_rich)
    module = runpy.run_path(str(Path(progress_ui.__file__)))
    assert module["format_duration"](62) == "1m02s"
    with pytest.raises(RuntimeError, match="Rich is required"):
        module["make_three_state_progress"]()
