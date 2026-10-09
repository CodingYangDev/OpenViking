# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openviking.pyagfs import AsyncAGFSClient
from openviking.service.task_queue_middleware import TaskWorkQueueMiddleware
from openviking.service.task_store import TaskStore
from openviking.service.task_tracker import TaskStatus, TaskTracker
from openviking.service.task_work_index import TaskWorkIndex
from openviking.storage.queuefs.named_queue import NamedQueue
from openviking.storage.queuefs.process_result import ProcessOutcome
from openviking.storage.queuefs.session_commit_msg import SessionCommitMsg
from openviking.storage.queuefs.session_commit_processor import SessionCommitProcessor


@pytest.fixture
def consumer(monkeypatch):
    store = AsyncMock(spec=TaskStore)
    store.get.return_value = None
    tracker = TaskTracker(store)
    index = TaskWorkIndex()
    tracker.attach_work_index(index)
    monkeypatch.setattr(
        "openviking.storage.queuefs.session_commit_processor.get_task_tracker", lambda: tracker
    )
    transport = AsyncMock(spec=AsyncAGFSClient)
    transport.write.return_value = "message-1"
    monkeypatch.setattr(
        "openviking.storage.queuefs.named_queue.AsyncAGFSClient", lambda client: transport
    )
    session = SimpleNamespace(
        exists=AsyncMock(return_value=True),
        load=AsyncMock(),
        resume_queued_commit=AsyncMock(return_value=True),
    )
    handler = SessionCommitProcessor(SimpleNamespace(session=lambda *args, **kwargs: session))
    queue = NamedQueue(
        object(),
        "/queue",
        "SessionCommit",
        dequeue_handler=handler,
        middlewares=[TaskWorkQueueMiddleware(index)],
    )
    message = SessionCommitMsg(
        task_id="task-1",
        session_id="session-1",
        session_uri="viking://user/alice/sessions/session-1",
        archive_uri="viking://user/alice/sessions/session-1/history/archive_001",
        user={"account_id": "acme", "user_id": "alice"},
    )
    return SimpleNamespace(
        store=store,
        tracker=tracker,
        index=index,
        transport=transport,
        session=session,
        handler=handler,
        queue=queue,
        message=message,
        owner={"account_id": "acme", "user_id": "alice"},
    )


async def enqueue(state):
    await state.queue.enqueue(state.message.to_dict())
    return {"id": "message-1", "data": state.transport.write.await_args.args[1].decode()}


@pytest.mark.parametrize("registered", [True, False])
@pytest.mark.parametrize("stage", ["load", "resume_queued_commit"])
async def test_consumer_exception_finalizes_task_before_ack(consumer, registered, stage):
    state = consumer
    error = json.JSONDecodeError("Expecting value", "", 0)
    getattr(state.session, stage).side_effect = error
    if registered:
        await state.tracker.create("session_commit", task_id="task-1", **state.owner)
    message = await enqueue(state)

    result = await state.queue.process_dequeued(message)

    assert result.outcome is ProcessOutcome.FAILED
    assert result.error == f"JSONDecodeError: {error}"
    task = await state.tracker.get("task-1", **state.owner)
    assert task.error == result.error
    assert state.index.has_work("task-1")

    async def ack(path, data):
        assert path == "/queue/SessionCommit/ack"
        assert data == b"message-1"
        # The terminal state must already be durable before the recovery item disappears.
        task = await state.tracker.get("task-1", **state.owner)
        assert task.status is TaskStatus.FAILED
        assert state.store.update.await_args.args[0].status is TaskStatus.FAILED
        assert not state.index.has_work("task-1")
        return ""

    state.transport.write.side_effect = ack
    await state.queue.ack("message-1", message)
    assert not state.index.has_work("task-1")
    # A producer arriving after the consumer must not reset the task to pending.
    task = await state.tracker.create("session_commit", task_id="task-1", **state.owner)
    assert task.status is TaskStatus.FAILED
    assert task.error == result.error


@pytest.mark.parametrize("stage", ["load", "resume_queued_commit"])
async def test_consumer_cancellation_does_not_become_failed_delivery(consumer, stage):
    state = consumer
    getattr(state.session, stage).side_effect = asyncio.CancelledError()
    message = await enqueue(state)
    with pytest.raises(asyncio.CancelledError):
        await state.queue.process_dequeued(message)
    assert state.index.has_work("task-1")
    state.store.create.assert_not_awaited()
    assert all(not call.args[0].endswith("/ack") for call in state.transport.write.await_args_list)


async def test_failure_persistence_error_does_not_ack_or_claim_success(consumer, monkeypatch):
    state = consumer
    state.session.load.side_effect = ValueError("bad metadata")
    fail = AsyncMock(side_effect=OSError("task store unavailable"))
    monkeypatch.setattr(state.tracker, "fail", fail)
    message = await enqueue(state)
    with pytest.raises(OSError, match="task store unavailable"):
        await state.queue.process_dequeued(message)
    fail.assert_awaited_once()
    assert state.index.has_work("task-1")
    assert all(not call.args[0].endswith("/ack") for call in state.transport.write.await_args_list)


async def test_concurrent_worker_acks_failure_and_continues_with_next_task(consumer):
    import threading
    from dataclasses import replace

    from openviking.storage.queuefs.queue_manager import QueueManager

    state = consumer
    messages = [await enqueue(state)]
    good = replace(state.message, task_id="task-2", session_id="session-2")
    await state.tracker.create("session_commit", task_id="task-2", **state.owner)
    state.transport.write.return_value = "message-2"
    await state.queue.enqueue(good.to_dict())
    messages.append({"id": "message-2", "data": state.transport.write.await_args.args[1].decode()})
    state.session.load.side_effect = [json.JSONDecodeError("Expecting value", "", 0), None]

    async def complete(msg):
        await state.tracker.complete(msg.task_id, {"session_id": msg.session_id}, **state.owner)
        return True

    async def read(path):
        assert path.endswith("/dequeue")
        return json.dumps(messages.pop(0)).encode() if messages else b"{}"

    stop = threading.Event()
    acknowledged = []

    async def write(path, data):
        assert path.endswith("/ack")
        acknowledged.append(data.decode())
        task_id = "task-1" if data == b"message-1" else "task-2"
        task = await state.tracker.get(task_id, **state.owner)
        assert task.status is (TaskStatus.FAILED if task_id == "task-1" else TaskStatus.COMPLETED)
        if len(acknowledged) == 2:
            stop.set()
        return ""

    state.session.resume_queued_commit.side_effect = complete
    state.transport.read.side_effect = read
    state.transport.write.side_effect = write
    manager = QueueManager.__new__(QueueManager)
    manager._poll_interval = 0.001
    manager._REQUEUE_POLL_INTERVAL = 0.001
    try:
        await asyncio.wait_for(manager._worker_async_concurrent(state.queue, stop, 1), timeout=1)
    finally:
        stop.set()
    assert acknowledged == ["message-1", "message-2"]
    assert not state.index.has_work("task-1")
    assert not state.index.has_work("task-2")


async def test_consumer_failure_is_trace_only_with_task_identity(consumer, monkeypatch):
    import importlib
    from unittest.mock import Mock

    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.trace import StatusCode

    module = importlib.import_module("openviking.telemetry.tracer")
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    console = Mock()
    monkeypatch.setattr(module, "_otel_tracer", provider.get_tracer("test"))
    monkeypatch.setattr(module, "logger", console)
    consumer.session.load.side_effect = json.JSONDecodeError("Expecting value", "", 0)
    try:
        result = await consumer.queue.process_dequeued(await enqueue(consumer))
        assert result.outcome is ProcessOutcome.FAILED
        console.opt.assert_not_called()
        (span,) = exporter.get_finished_spans()
        assert span.name == "queue.session_commit.process"
        assert span.status.status_code == StatusCode.ERROR
        for key, expected in {
            "task_id": "task-1",
            "session_id": "session-1",
            "account_id": "acme",
            "user_id": "alice",
        }.items():
            assert span.attributes[key] == expected
        assert any(
            "JSONDecodeError" in str(event.attributes.get("exception.stacktrace", ""))
            for event in span.events
        )
    finally:
        provider.shutdown()


@pytest.mark.parametrize("processed", [True, False])
async def test_on_dequeue_preserves_success_and_deferred_results(consumer, monkeypatch, processed):
    work = AsyncMock(return_value=processed)
    monkeypatch.setattr(consumer.handler, "_process", work)
    result = await consumer.handler.on_dequeue({"data": consumer.message.to_dict()})
    assert result.outcome is (ProcessOutcome.SUCCESS if processed else ProcessOutcome.REQUEUED)
    consumer.store.create.assert_not_awaited()


async def test_invalid_queue_payload_still_returns_handled_failure(consumer):
    result = await consumer.handler.on_dequeue({"data": "not JSON"})
    assert result.outcome is ProcessOutcome.FAILED
    consumer.session.load.assert_not_awaited()
    consumer.store.create.assert_not_awaited()
