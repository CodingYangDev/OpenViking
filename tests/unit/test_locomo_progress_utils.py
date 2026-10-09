"""Tests for LoCoMo benchmark progress utilities."""

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest
from rich.progress import ProgressColumn

from benchmark.locomo.vikingbot.progress_utils import (
    ProgressTracker,
)
from openviking.utils.progress import (
    ProgressSummaryColumn,
    ThreeStateBarColumn,
    make_three_state_progress,
)


def test_three_state_bar_column_initializes_progress_column_state():
    column = ThreeStateBarColumn()

    assert isinstance(column, ProgressColumn)
    assert hasattr(column, "_renderable_cache")
    assert hasattr(column, "_update_time")


def test_three_state_progress_renders_without_missing_cache_error():
    progress, task_id = make_three_state_progress(description="Test", transient=True)
    progress.update(task_id, total=4, completed=2, running=1, succeeded=1, failed=1)

    renderables = list(progress.get_renderables())

    assert renderables


def test_progress_summary_hides_ok_and_zero_failed_counts():
    progress, task_id = make_three_state_progress(description="Test", transient=True)
    progress.update(task_id, total=25, completed=23, running=0, succeeded=23, failed=0)
    task = progress.tasks[0]

    summary = ProgressSummaryColumn().render(task).plain

    assert summary == "(23/25)"
    assert "ok" not in summary
    assert "failed" not in summary


def test_progress_summary_shows_failed_only_when_non_zero():
    progress, task_id = make_three_state_progress(description="Test", transient=True)
    progress.update(task_id, total=25, completed=24, running=0, succeeded=23, failed=1)
    task = progress.tasks[0]

    summary = ProgressSummaryColumn().render(task).plain

    assert summary == "(24/25, 1 failed)"


def test_locomo_progress_tracker_records_failed_jobs():
    progress, task_id = make_three_state_progress(description="Test", transient=True)
    tracker = ProgressTracker(progress, task_id, total=2)

    tracker.job_started()
    tracker.job_finished()
    tracker.job_started()
    tracker.job_finished(failed=True)

    task = progress.tasks[0]
    assert tracker.done == 2
    assert tracker.failed == 1
    assert task.completed == 2
    assert task.fields["succeeded"] == 1
    assert task.fields["failed"] == 1


def test_progress_tracker_counts_concurrent_threads():
    progress, task_id = make_three_state_progress(description="Test", transient=True)
    tracker = ProgressTracker(progress, task_id, total=100)

    def run_job(index):
        tracker.job_started()
        tracker.job_finished(failed=index % 4 == 0)

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(run_job, range(100)))

    task = progress.tasks[0]
    assert (tracker.done, tracker.running, tracker.failed) == (100, 0, 25)
    assert task.completed == 100
    assert task.fields["succeeded"] == 75


@pytest.mark.asyncio
async def test_progress_tracker_counts_concurrent_async_tasks():
    progress, task_id = make_three_state_progress(description="Test", transient=True)
    tracker = ProgressTracker(progress, task_id, total=20)

    async def run_job(index):
        tracker.job_started()
        await asyncio.sleep(0)
        tracker.job_finished(failed=index % 4 == 0)

    await asyncio.gather(*(run_job(index) for index in range(20)))

    task = progress.tasks[0]
    assert (tracker.done, tracker.running, tracker.failed) == (20, 0, 5)
    assert task.completed == 20
    assert task.fields["succeeded"] == 15
