# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

import asyncio
import inspect
import json
from dataclasses import fields
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openviking.message import Message, TextPart, ToolPart
from openviking.service.task_tracker import TaskStatus, TaskTracker, set_task_tracker
from openviking.session.session import Session
from openviking.storage.queuefs.session_commit_msg import SessionCommitMsg


class _TaskStore:
    def __init__(self):
        self.tasks = {}

    async def create(self, task):
        self.tasks[task.task_id] = task

    async def update(self, task):
        self.tasks[task.task_id] = task

    async def get(self, task_id, *, account_id=None, user_id=None):
        return None

    async def list(self, account_id, *, user_id=None):
        return []

    async def delete(self, task_id, *, account_id, user_id=None):
        self.tasks.pop(task_id, None)


class _MemoryVikingFS:
    def __init__(self, files):
        self.files = files
        self._async_agfs = AsyncMock()

    def _uri_to_path(self, uri, ctx=None):
        return "/local/session-1"

    async def read_file(self, uri, ctx=None):
        if uri not in self.files:
            raise FileNotFoundError(uri)
        return self.files[uri]

    async def write_file(self, uri, content, ctx=None, lease_ref=None):
        self.files[uri] = content

    async def append_file(self, uri, content, ctx=None):
        self.files[uri] += content

    async def exists(self, uri, ctx=None):
        return uri in self.files or any(path.startswith(f"{uri}/") for path in self.files)

    async def ls(self, uri, ctx=None):
        prefix = f"{uri}/"
        names = {
            path[len(prefix) :].split("/")[0] for path in self.files if path.startswith(prefix)
        }
        return [{"name": name} for name in sorted(names)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "keep_count, keep_turns, archive_count",
    [(10, None, 5), (10, 1, 2), (10, 0, 15), (0, None, 15)],
)
async def test_commit_retention_boundary_and_pending_tokens_after_reload(
    monkeypatch, keep_count, keep_turns, archive_count
):
    session_uri = "viking://user/default/sessions/session-1"
    messages = [
        Message(id="old-user", role="user", parts=[TextPart("old question")]),
        Message(id="old-assistant", role="assistant", parts=[TextPart("old answer")]),
        Message(id="latest-user", role="user", parts=[TextPart("latest question")]),
        *[
            Message(
                id=f"step-{i}",
                role="assistant",
                parts=[
                    ToolPart(tool_id=f"tool-{i}", tool_name="read", tool_output=f"result {i}"),
                ],
            )
            for i in range(12)
        ],
    ]
    storage = _MemoryVikingFS(
        {
            f"{session_uri}/messages.jsonl": "\n".join(message.to_jsonl() for message in messages),
        }
    )
    tracker = TaskTracker(_TaskStore())
    monkeypatch.setattr("openviking.session.session._enabled_memory_types", lambda: set())
    monkeypatch.setattr("openviking.service.task_tracker.get_task_tracker", lambda: tracker)
    monkeypatch.setattr(
        "openviking.storage.queuefs.get_queue_manager",
        lambda: SimpleNamespace(enqueue=AsyncMock()),
    )
    session = Session(viking_fs=storage, session_id="session-1", session_uri=session_uri)
    turn_options = (
        {"retention_mode": "turn_budget", "keep_recent_turn_count": keep_turns}
        if keep_turns is not None
        else {}
    )
    result = await session.commit_async(keep_recent_count=keep_count, **turn_options)
    archived = await session._archives.read_messages(result["archive_uri"])
    retained = messages[archive_count:]
    assert [message.id for message in archived] == [m.id for m in messages[:archive_count]]
    assert [message.id for message in session.messages] == [m.id for m in retained]
    assert session.meta.pending_tokens == 0

    reloaded = Session(viking_fs=storage, session_id="session-1", session_uri=session_uri)
    await reloaded.load()
    assert reloaded.meta.pending_tokens == 0
    appended = await reloaded.add_message_async("user", [TextPart("next question")])
    if keep_turns == 0 or keep_count == 0:
        expected = appended.estimated_tokens
    elif keep_turns == 1:
        expected = sum(message.estimated_tokens for message in retained)
    else:
        expected = retained[0].estimated_tokens
    assert reloaded.meta.pending_tokens == expected
    fresh = Session(viking_fs=storage, session_id="session-1", session_uri=session_uri)
    await fresh.load()
    assert fresh.meta.pending_tokens == expected


def test_phase2_queue_snapshot_parameters_are_appended():
    signature = inspect.signature(Session._run_memory_extraction)

    assert list(signature.parameters)[-2:] == ["auto_commit_policy", "extraction_context"]
    assert [item.name for item in fields(SessionCommitMsg)[-2:]] == [
        "auto_commit_policy",
        "extraction_context",
    ]


@pytest.mark.asyncio
async def test_commit_queue_preserves_two_phase_agent_memory_types(monkeypatch):
    session_uri = "viking://user/default/sessions/session-1"
    message = Message(id="user", role="user", parts=[TextPart("remember this")])
    storage = _MemoryVikingFS({f"{session_uri}/messages.jsonl": f"{message.to_jsonl()}\n"})
    tracker = TaskTracker(_TaskStore())
    queue = SimpleNamespace(enqueue=AsyncMock())
    monkeypatch.setattr(
        "openviking.session.session._enabled_memory_types",
        lambda: {"cases", "trajectories", "experiences"},
    )
    monkeypatch.setattr("openviking.service.task_tracker.get_task_tracker", lambda: tracker)
    monkeypatch.setattr("openviking.storage.queuefs.get_queue_manager", lambda: queue)
    session = Session(viking_fs=storage, session_id="session-1", session_uri=session_uri)

    result = await session.commit_async(
        memory_policy={
            "memory_types": ["cases", "trajectories"],
            "working_memory": {"enabled": False},
            "extract_thinking": True,
        }
    )
    archive_meta = json.loads(storage.files[f"{result['archive_uri']}/.meta.json"])

    assert archive_meta["agent_evolution"] == {"enabled": True, "skip_reason": None}
    queued = SessionCommitMsg.from_dict(archive_meta["phase1"]["queue_message"])
    assert queued.memory_policy["extract_thinking"] is True
    assert archive_meta["phase1"]["queue_message"]["memory_policy"]["memory_types"] == [
        "cases",
        "trajectories",
    ]
    queue.enqueue.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("thinking", [None, False, True])
async def test_phase2_forwards_thinking_to_each_batch(monkeypatch, thinking):
    session_uri = "viking://user/default/sessions/session-1"
    messages = [
        Message(id=f"user-{i}", role="user", parts=[TextPart(f"remember {i}")]) for i in range(2)
    ]
    storage = _MemoryVikingFS(
        {f"{session_uri}/messages.jsonl": "".join(m.to_jsonl() + "\n" for m in messages)}
    )
    tracker = TaskTracker(_TaskStore())
    queue = SimpleNamespace(enqueue=AsyncMock())
    monkeypatch.setattr("openviking.session.session._enabled_memory_types", lambda: {"profile"})
    monkeypatch.setattr("openviking.service.task_tracker.get_task_tracker", lambda: tracker)
    monkeypatch.setattr("openviking.storage.queuefs.get_queue_manager", lambda: queue)
    session = Session(viking_fs=storage, session_id="session-1", session_uri=session_uri)
    await session.load()
    session.meta.auto_commit_policy = {"pending_token_threshold": 0, "message_count_threshold": 1}
    session._agent_evolution_enabled_provider = lambda: False
    extract = AsyncMock(return_value=[])
    session._session_compressor = SimpleNamespace(extract_long_term_memories=extract)
    policy = {
        "memory_types": ["profile"],
        "working_memory": {"enabled": False},
        "peer": {"enabled": False},
    }
    if thinking is not None:
        policy["extract_thinking"] = thinking
    result = await session.commit_async(memory_policy=policy)
    meta = json.loads(storage.files[f"{result['archive_uri']}/.meta.json"])
    queued = SessionCommitMsg.from_dict(meta["phase1"]["queue_message"])
    await session._run_memory_extraction(
        task_id=result["task_id"],
        archive_uri=result["archive_uri"],
        messages=messages,
        first_message_id=messages[0].id,
        last_message_id=messages[-1].id,
        memory_policy=queued.memory_policy,
        agent_evolution_enabled=False,
        auto_commit_policy=queued.auto_commit_policy,
    )
    task = await tracker.get(result["task_id"])
    assert task.status.value == "completed", task.to_dict()
    assert extract.await_count == 2
    expected = False if thinking is None else thinking
    assert all(call.kwargs["extract_thinking"] is expected for call in extract.await_args_list)


@pytest.mark.asyncio
async def test_resume_queued_commit_continues_phase2(monkeypatch):
    session_uri = "viking://user/default/sessions/session-1"
    archive_uri = f"{session_uri}/history/archive_001"
    archived = Message(id="archived", role="user", parts=[TextPart("old")])
    retained = Message(id="retained", role="assistant", parts=[TextPart("new")])
    files = {
        f"{session_uri}/messages.jsonl": f"{retained.to_jsonl()}\n",
        f"{session_uri}/.meta.json": json.dumps(
            {"session_id": "session-1", "message_count": 1, "commit_count": 1}
        ),
        f"{archive_uri}/messages.jsonl": f"{archived.to_jsonl()}\n",
    }
    viking_fs = _MemoryVikingFS(files)
    session = Session(viking_fs=viking_fs, session_id="session-1", session_uri=session_uri)
    tracker = TaskTracker(_TaskStore())
    set_task_tracker(tracker)
    monkeypatch.setattr(session, "_run_memory_extraction", AsyncMock())
    message = SessionCommitMsg(
        task_id="task-1",
        session_id="session-1",
        session_uri=session_uri,
        archive_uri=archive_uri,
        user={"account_id": "default", "user_id": "default"},
        memory_policy={"memory_types": []},
        auto_commit_policy={"pending_token_threshold": 8000},
        extraction_context="trusted routing context",
    )

    try:
        await session.resume_queued_commit(message)
    finally:
        set_task_tracker(None)

    session._run_memory_extraction.assert_awaited_once()
    assert session._run_memory_extraction.await_args.kwargs["task_id"] == "task-1"
    assert session._run_memory_extraction.await_args.kwargs["agent_evolution_enabled"] is True
    assert session._run_memory_extraction.await_args.kwargs["auto_commit_policy"] == {
        "pending_token_threshold": 8000
    }
    assert (
        session._run_memory_extraction.await_args.kwargs["extraction_context"]
        == "trusted routing context"
    )
    assert [item.id for item in session._run_memory_extraction.await_args.kwargs["messages"]] == [
        "archived"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("archive_content", [None, "{invalid json", "{}"])
async def test_resume_queued_commit_fails_terminally_for_unreadable_archive(
    monkeypatch, archive_content
):
    session_uri = "viking://user/default/sessions/session-1"
    archive_uri = f"{session_uri}/history/archive_001"
    files = {}
    if archive_content is not None:
        files[f"{archive_uri}/messages.jsonl"] = archive_content
    viking_fs = _MemoryVikingFS(files)
    session = Session(viking_fs=viking_fs, session_id="session-1", session_uri=session_uri)
    tracker = TaskTracker(_TaskStore())
    set_task_tracker(tracker)
    monkeypatch.setattr(session, "_run_memory_extraction", AsyncMock())
    message = SessionCommitMsg(
        task_id="task-1",
        session_id="session-1",
        session_uri=session_uri,
        archive_uri=archive_uri,
        user={"account_id": "default", "user_id": "default"},
    )

    try:
        await session.resume_queued_commit(message)
        task = await tracker.get("task-1")
    finally:
        set_task_tracker(None)

    assert task.status == TaskStatus.FAILED
    failed = json.loads(files[f"{archive_uri}/.failed.json"])
    assert failed["stage"] == "archive_read"
    session._run_memory_extraction.assert_not_awaited()


@pytest.mark.asyncio
async def test_session_context_skips_pending_archive_with_missing_messages(monkeypatch):
    session_uri = "viking://user/default/sessions/session-1"
    archive_uri = f"{session_uri}/history/archive_001"
    session = Session(
        viking_fs=_MemoryVikingFS({}),
        session_id="session-1",
        session_uri=session_uri,
    )
    monkeypatch.setattr(
        session._archives,
        "list_refs",
        AsyncMock(
            return_value=[{"archive_id": "archive_001", "archive_uri": archive_uri, "index": 1}]
        ),
    )

    context = await session.get_context_for_search(query="test")

    assert context == {"latest_archive_overview": "", "current_messages": []}


@pytest.mark.asyncio
async def test_resume_queued_commit_uses_agent_evolution_archive_snapshot(monkeypatch):
    session_uri = "viking://user/default/sessions/session-1"
    archive_uri = f"{session_uri}/history/archive_001"
    archived = Message(id="archived", role="user", parts=[TextPart("old")])
    files = {
        f"{session_uri}/messages.jsonl": "",
        f"{session_uri}/.meta.json": json.dumps(
            {"session_id": "session-1", "message_count": 0, "commit_count": 1}
        ),
        f"{archive_uri}/messages.jsonl": f"{archived.to_jsonl()}\n",
        f"{archive_uri}/.meta.json": json.dumps(
            {
                "agent_evolution": {
                    "enabled": False,
                    "skip_reason": "agent_evolution_disabled",
                    "user_config_error": None,
                }
            }
        ),
    }
    session = Session(
        viking_fs=_MemoryVikingFS(files),
        session_id="session-1",
        session_uri=session_uri,
    )
    tracker = TaskTracker(_TaskStore())
    set_task_tracker(tracker)
    monkeypatch.setattr(session, "_run_memory_extraction", AsyncMock())
    message = SessionCommitMsg(
        task_id="task-1",
        session_id="session-1",
        session_uri=session_uri,
        archive_uri=archive_uri,
        user={"account_id": "default", "user_id": "default"},
        memory_policy={"memory_types": ["profile"]},
    )

    try:
        await session.resume_queued_commit(message)
    finally:
        set_task_tracker(None)

    kwargs = session._run_memory_extraction.await_args.kwargs
    assert kwargs["agent_evolution_enabled"] is False
    assert kwargs["agent_memory_skip_reason"] == "agent_evolution_disabled"
    assert kwargs["user_config_error"] is None


def test_session_commit_message_ignores_unknown_fields():
    message = SessionCommitMsg.from_dict(
        {
            "task_id": "task-1",
            "session_id": "session-1",
            "session_uri": "viking://user/default/sessions/session-1",
            "archive_uri": "viking://user/default/sessions/session-1/history/archive_001",
            "user": {"account_id": "default", "user_id": "default"},
            "actor_peer_id": "visitor-a",
            "usage_uris": ["viking://resources/legacy"],
        }
    )

    assert message.task_id == "task-1"
    assert message.auto_commit_policy == {}
    assert message.extraction_context == ""
    assert "actor_peer_id" not in message.to_dict()
    assert "usage_uris" not in message.to_dict()


@pytest.fixture
def commit_publish_state(monkeypatch):
    session_uri = "viking://user/default/sessions/publish-order"
    messages = [
        Message(id="archive", role="user", parts=[TextPart("archive me")]),
        Message(id="retain", role="assistant", parts=[TextPart("retain me")]),
    ]
    storage = _MemoryVikingFS(
        {
            f"{session_uri}/messages.jsonl": "\n".join(m.to_jsonl() for m in messages),
            f"{session_uri}/.meta.json": json.dumps(
                {"session_id": "publish-order", "message_count": 2}
            ),
        }
    )
    tracker = TaskTracker(_TaskStore())
    queue = SimpleNamespace(enqueue=AsyncMock())
    monkeypatch.setattr("openviking.session.session._enabled_memory_types", lambda: set())
    monkeypatch.setattr("openviking.service.task_tracker.get_task_tracker", lambda: tracker)
    monkeypatch.setattr("openviking.storage.queuefs.get_queue_manager", lambda: queue)
    session = Session(viking_fs=storage, session_id="publish-order", session_uri=session_uri)
    return SimpleNamespace(session=session, storage=storage, queue=queue, uri=session_uri)


@pytest.mark.asyncio
async def test_enqueue_observes_complete_live_messages_and_metadata(commit_publish_state):
    state = commit_publish_state

    async def enqueue(queue_name, data):
        assert queue_name == "SessionCommit"
        consumer = Session(
            viking_fs=state.storage, session_id="publish-order", session_uri=state.uri
        )
        await consumer.load()
        assert [m.id for m in consumer.messages] == ["retain"]
        assert consumer.meta.message_count == 1
        assert consumer.meta.commit_count == 1
        assert consumer.meta.pending_tokens == 0
        phase1 = await consumer._read_phase1_meta(data["archive_uri"])
        assert phase1["status"] == "preparing"
        archived = await consumer._archives.read_messages(data["archive_uri"])
        assert [m.id for m in archived] == ["archive"]
        state.storage._async_agfs.pathlock_release.assert_not_awaited()

    state.queue.enqueue.side_effect = enqueue
    result = await state.session.commit_async(keep_recent_count=1)
    state.queue.enqueue.assert_awaited_once()
    assert (await state.session._read_phase1_meta(result["archive_uri"]))["status"] == "ready"
    state.storage._async_agfs.pathlock_release.assert_awaited_once()


@pytest.mark.asyncio
async def test_commit_does_not_publish_during_incomplete_metadata_write(
    commit_publish_state, monkeypatch
):
    state = commit_publish_state
    writing, finish = asyncio.Event(), asyncio.Event()
    save_meta = state.session._save_meta

    async def paused_save():
        state.storage.files[f"{state.uri}/.meta.json"] = ""
        writing.set()
        await finish.wait()
        await save_meta()

    monkeypatch.setattr(state.session, "_save_meta", paused_save)
    task = asyncio.create_task(state.session.commit_async(keep_recent_count=1))
    try:
        await asyncio.wait_for(writing.wait(), 1)
        state.queue.enqueue.assert_not_awaited()
        finish.set()
        await asyncio.wait_for(task, 1)
        state.queue.enqueue.assert_awaited_once()
    finally:
        finish.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["_write_to_agfs_async", "_save_meta"])
async def test_live_state_write_failure_does_not_enqueue(commit_publish_state, monkeypatch, method):
    state = commit_publish_state
    monkeypatch.setattr(state.session, method, AsyncMock(side_effect=RuntimeError("write failed")))
    with pytest.raises(RuntimeError, match="write failed"):
        await state.session.commit_async(keep_recent_count=1)
    state.queue.enqueue.assert_not_awaited()
    archive = f"{state.uri}/history/archive_001"
    marker = json.loads(state.storage.files[f"{archive}/.failed.json"])
    assert marker["stage"] == "phase1_persist"
    assert [m.id for m in await state.session._archives.read_messages(archive)] == ["archive"]
    assert (await state.session._read_phase1_meta(archive))["status"] != "ready"
    state.storage._async_agfs.pathlock_release.assert_awaited_once()


@pytest.mark.asyncio
async def test_enqueue_failure_keeps_raw_archive_and_marks_failure(commit_publish_state):
    state = commit_publish_state
    state.queue.enqueue.side_effect = RuntimeError("queue unavailable")
    with pytest.raises(RuntimeError, match="queue unavailable"):
        await state.session.commit_async(keep_recent_count=1)
    archive = f"{state.uri}/history/archive_001"
    marker = json.loads(state.storage.files[f"{archive}/.failed.json"])
    assert marker["stage"] == "queue_enqueue"
    assert [m.id for m in await state.session._archives.read_messages(archive)] == ["archive"]
    live = await state.session._read_live_messages_strict()
    assert [m.id for m in live] == ["retain"]
    assert json.loads(state.storage.files[f"{state.uri}/.meta.json"])["commit_count"] == 1
    state.storage._async_agfs.pathlock_release.assert_awaited_once()
