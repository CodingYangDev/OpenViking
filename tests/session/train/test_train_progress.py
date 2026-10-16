"""Train adapters use the shared progress display without changing execution."""

from __future__ import annotations

import asyncio
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


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("rollout failed"), asyncio.CancelledError()])
async def test_run_with_progress_cancels_and_joins_siblings_before_raising(failure):
    started = asyncio.Event()
    cleanup_started = asyncio.Event()
    finish_cleanup = asyncio.Event()
    workers = []
    called = []

    async def execute(item, index):
        workers.append(asyncio.current_task())
        called.append(item)
        if item == 0:
            await started.wait()
            raise failure
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await finish_cleanup.wait()

    batch = asyncio.create_task(
        train_progress.run_with_progress(
            [0, 1, 2], coroutine_factory=execute, label="train", enabled=False, concurrency=2
        )
    )
    try:
        await asyncio.wait_for(cleanup_started.wait(), timeout=1)
        assert not batch.done(), "The caller must keep its resources open during cleanup"
        finish_cleanup.set()
        with pytest.raises(type(failure)) as caught:
            await batch
        assert caught.value is failure
        assert called == [0, 1], "Queued work must not start after a failure"
        assert all(worker.done() for worker in workers)
    finally:
        finish_cleanup.set()
        batch.cancel()
        for worker in workers:
            worker.cancel()
        await asyncio.gather(batch, *workers, return_exceptions=True)


@pytest.mark.asyncio
async def test_run_with_progress_external_cancel_waits_for_async_cleanup():
    started = asyncio.Event()
    cleanup_started = asyncio.Event()
    finish_cleanup = asyncio.Event()
    workers = []
    cleaned = []

    async def execute(item, index):
        workers.append(asyncio.current_task())
        if len(workers) == 2:
            started.set()
        try:
            await asyncio.Event().wait()
        finally:
            if item == 1:
                cleanup_started.set()
                await finish_cleanup.wait()
            cleaned.append(item)

    batch = asyncio.create_task(
        train_progress.run_with_progress(
            [0, 1], coroutine_factory=execute, label="train", enabled=False, concurrency=2
        )
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        batch.cancel()
        await asyncio.wait_for(cleanup_started.wait(), timeout=1)
        await asyncio.sleep(0)
        assert not batch.done()
        finish_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await batch
        assert sorted(cleaned) == [0, 1]
        assert all(worker.done() for worker in workers)
    finally:
        finish_cleanup.set()
        batch.cancel()
        for worker in workers:
            worker.cancel()
        await asyncio.gather(batch, *workers, return_exceptions=True)


@pytest.mark.asyncio
async def test_run_with_progress_preserves_order_and_concurrency():
    second_finished = asyncio.Event()
    running = 0
    peak_running = 0
    finished = []

    async def execute(item, index):
        nonlocal running, peak_running
        running += 1
        peak_running = max(peak_running, running)
        try:
            if index == 0:
                await second_finished.wait()
            else:
                second_finished.set()
            finished.append(index)
            return item + index
        finally:
            running -= 1

    result = await asyncio.wait_for(
        train_progress.run_with_progress(
            [10, 20, 30], coroutine_factory=execute, label="train", enabled=False, concurrency=2
        ),
        timeout=1,
    )
    assert result == [10, 21, 32]
    assert finished[0] == 1
    assert peak_running == 2
    assert running == 0
