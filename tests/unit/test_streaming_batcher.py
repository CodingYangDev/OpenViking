# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

import asyncio
from types import SimpleNamespace

import pytest

from openviking.session.memory.utils.streaming_batcher import (
    StreamingBatcher,
    StreamingBatcherConfig,
)


@pytest.mark.asyncio
async def test_close_waits_for_in_flight_timer_flush():
    started = asyncio.Event()
    finish = asyncio.Event()

    async def process_batch(items: list[str], _reason: str) -> str:
        started.set()
        await finish.wait()
        return ",".join(items)

    batcher = StreamingBatcher(
        name="test",
        process_batch=process_batch,
        config=StreamingBatcherConfig(
            max_items_per_batch=10,
            max_wait_seconds=0.01,
            timer_check_interval_seconds=0.001,
        ),
    )
    submit = asyncio.create_task(batcher.submit("item"))
    await asyncio.wait_for(started.wait(), timeout=1)

    close = asyncio.create_task(batcher.close())
    await asyncio.sleep(0)
    finish.set()

    assert await asyncio.wait_for(submit, timeout=1) == "item"
    assert await asyncio.wait_for(close, timeout=1) is None


@pytest.mark.asyncio
async def test_close_drains_failing_timer_flush_without_unhandled_task():
    started = asyncio.Event()
    finish = asyncio.Event()
    unhandled: list[dict[str, object]] = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()

    async def process_batch(_items: list[str], _reason: str) -> str:
        started.set()
        await finish.wait()
        raise RuntimeError("boom")

    batcher = StreamingBatcher(
        name="failing-test",
        process_batch=process_batch,
        config=StreamingBatcherConfig(
            max_items_per_batch=10,
            max_wait_seconds=0.01,
            timer_check_interval_seconds=0.001,
        ),
    )
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    try:
        submit = asyncio.create_task(batcher.submit("item"))
        await asyncio.wait_for(started.wait(), timeout=1)

        close = asyncio.create_task(batcher.close())
        await asyncio.sleep(0)
        finish.set()

        with pytest.raises(RuntimeError, match="boom"):
            await asyncio.wait_for(submit, timeout=1)
        assert await asyncio.wait_for(close, timeout=1) is None
        await asyncio.sleep(0)

        assert submit.done()
        assert close.done()
        assert unhandled == []
    finally:
        loop.set_exception_handler(previous_handler)


@pytest.mark.asyncio
@pytest.mark.parametrize("tail_trigger", ["count", "time", "close"])
async def test_queued_flushes_recheck_readiness_before_consuming_fresh_tail(
    monkeypatch, tail_trigger
):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(
        "openviking.session.memory.utils.streaming_batcher.time",
        SimpleNamespace(monotonic=lambda: clock.now),
    )
    entered = [asyncio.Event(), asyncio.Event()]
    release = [asyncio.Event(), asyncio.Event()]
    batches = []

    async def process(items, reason):
        index = len(batches)
        batches.append((list(items), reason))
        if index < 2:
            entered[index].set()
            await release[index].wait()
        return list(items)

    batcher = StreamingBatcher(
        name="stale-flush",
        process_batch=process,
        config=StreamingBatcherConfig(
            max_items_per_batch=8, max_wait_seconds=10, timer_check_interval_seconds=3600
        ),
    )
    tasks = []

    async def enqueue(prefix, size):
        new = [asyncio.create_task(batcher.submit(f"{prefix}{i}")) for i in range(size)]
        tasks.extend(new)
        for _ in range(100):
            if await batcher.get_buffered_item_count() == size:
                return new
            await asyncio.sleep(0)
        raise AssertionError("submissions did not enter the buffer")

    try:
        first = await enqueue("first", 8)
        await asyncio.wait_for(entered[0].wait(), timeout=2)
        burst = await enqueue("burst", 35)
        release[0].set()
        await asyncio.wait_for(entered[1].wait(), timeout=2)
        tail = await enqueue("tail", 2)
        release[1].set()
        await asyncio.wait_for(asyncio.gather(*first, *burst), timeout=2)
        # Queue behind all the old count flushes; neither count nor timer may steal
        # the new tail using an eligibility decision from a previous buffer.
        assert await batcher.flush("count") is None
        assert await batcher.flush("time") is None
        assert [len(items) for items, _ in batches] == [8, 35]
        assert await batcher.get_buffered_item_count() == 2
        assert not any(task.done() for task in tail)
        if tail_trigger == "count":
            tasks.extend(asyncio.create_task(batcher.submit(f"extra{i}")) for i in range(6))
        elif tail_trigger == "time":
            clock.now = 11
            await batcher.flush("time")
        else:
            await batcher.close()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)
        assert len(batches[-1][0]) == (8 if tail_trigger == "count" else 2)
        assert batches[-1][1] == tail_trigger
        assert len({item for items, _ in batches for item in items}) == len(tasks)
    finally:
        for event in release:
            event.set()
        await batcher.close()
        await asyncio.gather(*tasks, return_exceptions=True)
