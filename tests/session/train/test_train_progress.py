"""Train adapters use the shared progress display without changing execution."""

from __future__ import annotations

from io import StringIO
from types import SimpleNamespace

import pytest
from rich.console import Console

from openviking.session.train.components import progress as train_progress
from openviking.utils import progress as progress_ui


@pytest.fixture
def clock(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(train_progress, "time", SimpleNamespace(monotonic=lambda: now[0]))
    return now


def test_plain_text_elapsed_and_failed_counts_freeze(clock, capsys):
    printer = train_progress.ProgressPrinter(total=2, label="train", enabled=True)
    assert not printer._use_rich
    clock[0] = 200.0
    printer.render()
    assert "elapsed: 0s" in capsys.readouterr().out
    printer.start_one()
    clock[0] = 262.0
    printer.fail_one()
    assert "(1/2, 1 failed) elapsed: 1m02s" in capsys.readouterr().out
    printer.finish()
    assert "elapsed: 1m02s\n" in capsys.readouterr().out
    clock[0] = 400.0
    printer.render()
    printer.finish()
    assert capsys.readouterr().out == ""


def test_rich_printer_uses_shared_factory_and_preserves_remote_counts(monkeypatch, clock):
    factory = progress_ui.make_three_state_progress
    created = []

    def make_progress(*args, **kwargs):
        kwargs["console"] = Console(file=StringIO())
        kwargs["get_time"] = lambda: clock[0]
        progress, task_id = factory(*args, **kwargs)
        progress.live.auto_refresh = False
        created.append(progress)
        return progress, task_id

    monkeypatch.setattr(progress_ui, "make_three_state_progress", make_progress)
    monkeypatch.setattr(train_progress.sys.stderr, "isatty", lambda: True)
    printer = train_progress.ProgressPrinter(total=4, label="train", enabled=True)
    printer.render()
    try:
        clock[0] = 162.0
        printer.update_counts(completed=1, failed=1, running=1)
        assert len(created) == 1
        task = created[0].tasks[0]
        assert task.completed == 2
        assert task.fields == {"running": 1, "succeeded": 1, "failed": 1}
        assert progress_ui.ElapsedTimeColumn().render(task).plain == "elapsed: 1m02s"
        with pytest.raises(ValueError):
            printer.update_counts(completed=4, failed=1, running=0)
    finally:
        printer.finish()
    clock[0] = 200.0
    assert progress_ui.ElapsedTimeColumn().render(task).plain == "elapsed: 1m02s"


@pytest.mark.parametrize(("enabled", "total"), [(False, 2), (True, 0)])
def test_disabled_or_empty_progress_is_silent(enabled, total, capsys):
    printer = train_progress.ProgressPrinter(total=total, label="train", enabled=enabled)
    printer.render()
    printer.start_one()
    printer.complete_one()
    printer.fail_one()
    printer.finish()
    assert capsys.readouterr() == ("", "")


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_run_with_progress_finishes_on_success_and_failure(fail, clock, capsys):
    async def execute(item, index):
        clock[0] = 162.0
        if fail:
            raise RuntimeError("rollout failed")
        return item + index

    kwargs = {"coroutine_factory": execute, "label": "train", "enabled": True, "concurrency": 1}
    if fail:
        with pytest.raises(RuntimeError, match="rollout failed"):
            await train_progress.run_with_progress([1], **kwargs)
    else:
        assert await train_progress.run_with_progress([1], **kwargs) == [1]
    output = capsys.readouterr().out
    assert output.endswith("elapsed: 1m02s\n")
    assert ("1 failed" in output) == fail
