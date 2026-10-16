# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Tests for resource-memory linking service."""

import asyncio
import json
import re
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openviking.server.identity import RequestContext, Role
from openviking.service.fs_service import FSService
from openviking.service.resource_memory_link_service import (
    _RESOURCE_REASON_SESSION_ID,
    ResourceMemoryLinkService,
)
from openviking.service.task_tracker import TaskStatus
from openviking.session.memory.dataclass import MemoryFile
from openviking.session.memory.memory_updater import MemoryUpdater
from openviking.session.memory.utils.memory_file_utils import MemoryFileUtils
from openviking.storage.queuefs.process_result import ProcessResult
from openviking.storage.queuefs.session_commit_processor import SessionCommitProcessor
from openviking_cli.exceptions import NotFoundError
from openviking_cli.session.user_id import UserIdentifier


class _FakeVikingFS:
    def __init__(self, store):
        self.store = store
        self.rm_calls = []
        self.read_calls = []
        self.tree_calls = []
        self._async_agfs = SimpleNamespace(
            stat=AsyncMock(return_value={"isDir": True}),
            pathlock_acquire_tree=AsyncMock(return_value={"lease_ref": "resource-lock"}),
            pathlock_acquire_exact=AsyncMock(return_value={"lease_ref": "memory-lock"}),
            pathlock_release=AsyncMock(),
        )

    def _uri_to_path(self, uri, ctx=None):
        return f"/local/{ctx.account_id}/{uri.removeprefix('viking://')}"

    async def exists(self, uri, ctx=None):
        return uri in self.store or any(key.startswith(uri.rstrip("/") + "/") for key in self.store)

    async def read_file(self, uri, ctx=None):
        self.read_calls.append(uri)
        if uri not in self.store:
            raise NotFoundError(uri)
        return self.store[uri]

    async def write_file(self, uri, content, ctx=None, lease_ref=None):
        self.store[uri] = content

    async def rm(self, uri, recursive=False, ctx=None, lock_handle=None, lease_ref=None):
        self.rm_calls.append((uri, recursive))
        self.store.pop(uri, None)
        return {"estimated_deleted_count": 1}

    async def tree(self, uri, ctx=None, node_limit=None, level_limit=None):
        self.tree_calls.append(
            {
                "uri": uri,
                "node_limit": node_limit,
                "level_limit": level_limit,
            }
        )
        prefix = uri.rstrip("/") + "/"
        return [
            {
                "uri": item_uri,
                "rel_path": item_uri.removeprefix(prefix),
                "isDir": False,
            }
            for item_uri in self.store
            if item_uri.startswith(prefix)
        ]

    async def grep(
        self,
        uri,
        pattern,
        exclude_uri=None,
        case_insensitive=False,
        node_limit=None,
        level_limit=None,
        ctx=None,
    ):
        del exclude_uri, case_insensitive, level_limit, ctx
        prefix = uri.rstrip("/") + "/"
        matches = [
            {
                "uri": item_uri,
                "line": 1,
                "content": content,
            }
            for item_uri, content in self.store.items()
            if item_uri.startswith(prefix) and re.search(pattern, content)
        ]
        if node_limit is not None:
            matches = matches[:node_limit]
        return {
            "matches": matches,
            "count": len(matches),
            "match_count": len(matches),
            "files_scanned": len(self.store),
        }


class _FakeGrepVikingFS(_FakeVikingFS):
    def __init__(self, store, grep_uris):
        super().__init__(store)
        self.grep_uris = grep_uris
        self.grep_calls = []

    async def grep(
        self,
        uri,
        pattern,
        exclude_uri=None,
        case_insensitive=False,
        node_limit=None,
        level_limit=None,
        ctx=None,
    ):
        self.grep_calls.append(
            {
                "uri": uri,
                "pattern": pattern,
                "exclude_uri": exclude_uri,
                "case_insensitive": case_insensitive,
                "node_limit": node_limit,
                "level_limit": level_limit,
            }
        )
        return {
            "matches": [
                {
                    "uri": match_uri,
                    "line": 1,
                    "content": self.store.get(match_uri, ""),
                }
                for match_uri in self.grep_uris
            ],
            "count": len(self.grep_uris),
            "match_count": len(self.grep_uris),
            "files_scanned": len(self.store),
        }

    async def tree(self, uri, ctx=None, node_limit=None, level_limit=None):
        raise AssertionError("grep path should not fall back to tree")


class _ReadFailVikingFS:
    async def read_file(self, uri, ctx=None):
        raise RuntimeError("storage unavailable")

    async def tree(self, uri, ctx=None, node_limit=None, level_limit=None):
        memory_uri = "viking://user/alice/memories/entities/wang.md"
        return [{"uri": memory_uri, "rel_path": "entities/wang.md", "isDir": False}]


class _FakeSession:
    def __init__(self):
        self.messages = []
        self.meta = SimpleNamespace(memory_policy=None)

    async def add_messages_async(self, specs):
        self.messages.extend(specs)
        self.meta.memory_policy = None


class _FakeSessionService:
    def __init__(self):
        self.session = _FakeSession()
        self.created = []
        self.memory_policy = None
        self.task_id = None
        self.got = []
        self.committed = []
        self.deleted = []

    async def create(self, ctx, session_id=None, memory_policy=None):
        self.created.append(
            {
                "ctx": ctx,
                "session_id": session_id,
                "memory_policy": memory_policy,
            }
        )
        return self.session

    async def get(self, session_id, ctx, auto_create=False):
        self.got.append(
            {
                "ctx": ctx,
                "session_id": session_id,
                "auto_create": auto_create,
            }
        )
        return self.session

    async def commit_async(self, session_id, ctx, keep_recent_count=0, *, memory_policy=None):
        self.memory_policy = memory_policy
        archive_index = len(self.committed) + 1
        self.committed.append(
            {
                "ctx": ctx,
                "session_id": session_id,
                "keep_recent_count": keep_recent_count,
            }
        )
        return {
            "task_id": self.task_id,
            "archive_uri": (
                f"viking://user/alice/sessions/{session_id}/history/archive_{archive_index:03d}"
            ),
        }

    async def delete(self, session_id, ctx):
        self.deleted.append({"ctx": ctx, "session_id": session_id})


@pytest.fixture
def request_context():
    return RequestContext(
        user=UserIdentifier("acct", "alice"),
        role=Role.USER,
    )


@pytest.fixture
def cleanup_queue(monkeypatch):
    queued = []
    tracker = SimpleNamespace(
        list_tasks=AsyncMock(return_value=[]), get=AsyncMock(), has_work=lambda _: False
    )
    monkeypatch.setattr("openviking.service.task_tracker.get_task_tracker", lambda: tracker)
    monkeypatch.setattr(
        "openviking.service.resource_memory_link_service.get_queue_manager",
        lambda: SimpleNamespace(
            enqueue=AsyncMock(side_effect=lambda _queue, payload: queued.append(dict(payload)))
        ),
    )
    monkeypatch.setattr(MemoryUpdater, "refresh_file_embedding", AsyncMock())
    monkeypatch.setattr(MemoryUpdater, "refresh_schema_overview", AsyncMock())
    return queued


async def _drain_cleanup(service, queued):
    for _ in range(4):
        if not queued:
            return
        await service.resume_cleanup(queued.pop(0))
    assert not queued


@pytest.mark.asyncio
async def test_on_resource_added_bridges_reason_through_fixed_session(request_context, monkeypatch):
    resource_uri = "viking://resources/images/2026/06/11/yueqian_jpeg"
    session_service = _FakeSessionService()
    session_service.task_id = "reason-1"
    tracker = SimpleNamespace(
        wait=AsyncMock(side_effect=AssertionError("ingestion waits for reason"))
    )
    monkeypatch.setattr("openviking.service.task_tracker.get_task_tracker", lambda: tracker)
    service = ResourceMemoryLinkService(
        viking_fs=_FakeVikingFS(
            {
                resource_uri: "resource",
                "viking://resources/images/2026/06/11/.abstract.md": "动漫角色照片合集",
            }
        ),
        session_service=session_service,
    )

    result = await service.on_resource_added(
        ctx=request_context,
        resource_uri=resource_uri,
        reason="这是越前龙马的照片",
        source_name="yueqian.jpeg",
    )

    session_id = result["session_id"]
    assert result["status"] == "accepted"
    assert result["commit_task_id"] == "reason-1"
    tracker.wait.assert_not_awaited()
    assert session_id == _RESOURCE_REASON_SESSION_ID
    assert session_service.got == [
        {
            "ctx": request_context,
            "session_id": session_id,
            "auto_create": True,
        }
    ]
    assert session_service.created == []
    assert session_service.memory_policy == {
        "self": {"enabled": True},
        "peer": {"enabled": False},
        "memory_types": ["entities", "events", "preferences"],
    }
    assert session_service.committed == [
        {
            "ctx": request_context,
            "session_id": session_id,
            "keep_recent_count": 0,
        }
    ]
    assert session_service.deleted == []
    message_text = session_service.session.messages[0]["parts"][0].text
    assert resource_uri in message_text
    assert "这是越前龙马的照片" in message_text
    assert "yueqian.jpeg" in message_text
    assert "动漫角色照片合集" in message_text


@pytest.mark.asyncio
async def test_on_resource_added_reuses_same_reason_session(request_context):
    """Concurrent queue loops must serialize writes to the shared reason session."""
    entered = asyncio.Event()
    release = asyncio.Event()
    worker_waiting = threading.Event()

    class BlockingSessionService(_FakeSessionService):
        async def get(self, session_id, ctx, auto_create=False):
            session = await super().get(session_id, ctx, auto_create=auto_create)
            if len(self.got) == 1:
                entered.set()
                await release.wait()
            return session

    session_service = BlockingSessionService()
    service = ResourceMemoryLinkService(
        viking_fs=_FakeVikingFS(
            {
                f"viking://resources/images/{name}.jpeg": "resource"
                for name in ("ryoma", "fuji", "tezuka")
            }
        ),
        session_service=session_service,
    )

    async def add_reason(name):
        return await service.on_resource_added(
            ctx=request_context,
            resource_uri=f"viking://resources/images/{name}.jpeg",
            reason=f"这是{name}的照片",
            source_name=f"{name}.jpeg",
        )

    async def add_from_other_queue():
        # Notify only after this coroutine has reached its first wait, so the
        # first queue still holds the session while the other queue enters.
        asyncio.get_running_loop().call_soon(worker_waiting.set)
        return await add_reason("tezuka")

    first = asyncio.create_task(add_reason("ryoma"))
    await entered.wait()
    second = asyncio.create_task(add_reason("fuji"))
    await asyncio.sleep(0)
    worker = asyncio.create_task(asyncio.to_thread(lambda: asyncio.run(add_from_other_queue())))
    try:
        assert await asyncio.to_thread(worker_waiting.wait, 3)
        assert len(session_service.got) == 1
    finally:
        release.set()
        results = await asyncio.wait_for(asyncio.gather(first, second, worker), timeout=3)

    assert all(result["status"] == "accepted" for result in results)
    assert all(result["session_id"] == _RESOURCE_REASON_SESSION_ID for result in results)
    assert [call["session_id"] for call in session_service.committed] == [
        _RESOURCE_REASON_SESSION_ID
    ] * 3
    assert session_service.deleted == []
    messages = [item["parts"][0].text for item in session_service.session.messages]
    assert len(messages) == 3
    for name in ("ryoma", "fuji", "tezuka"):
        assert any(f"这是{name}的照片" in message for message in messages)


@pytest.mark.asyncio
async def test_on_resource_added_routes_reason_to_actor_peer(request_context):
    peer_ctx = RequestContext(
        user=request_context.user,
        role=request_context.role,
        actor_peer_id="web-visitor-alice",
    )
    session_service = _FakeSessionService()
    service = ResourceMemoryLinkService(
        viking_fs=_FakeVikingFS({"viking://resources/images/ryoma.jpeg": "resource"}),
        session_service=session_service,
    )

    result = await service.on_resource_added(
        ctx=peer_ctx,
        resource_uri="viking://resources/images/ryoma.jpeg",
        reason="这是越前龙马的照片",
        source_name="ryoma.jpeg",
    )

    assert result["session_id"] == _RESOURCE_REASON_SESSION_ID
    assert session_service.memory_policy == {
        "self": {"enabled": False},
        "peer": {"enabled": True},
        "memory_types": ["entities", "events", "preferences"],
    }
    assert session_service.session.messages[0]["peer_id"] == "web-visitor-alice"
    assert session_service.committed == [
        {
            "ctx": peer_ctx,
            "session_id": _RESOURCE_REASON_SESSION_ID,
            "keep_recent_count": 0,
        }
    ]


@pytest.mark.asyncio
async def test_on_resource_added_routes_peer_resource_uri_to_peer(request_context):
    resource_uri = "viking://user/alice/peers/web-visitor-alice/resources/images/ryoma.jpeg"
    session_service = _FakeSessionService()
    service = ResourceMemoryLinkService(
        viking_fs=_FakeVikingFS({resource_uri: "resource"}),
        session_service=session_service,
    )

    await service.on_resource_added(
        ctx=request_context,
        resource_uri=resource_uri,
        reason="这是越前龙马的照片",
        source_name="ryoma.jpeg",
    )

    assert session_service.memory_policy == {
        "self": {"enabled": False},
        "peer": {"enabled": True},
        "memory_types": ["entities", "events", "preferences"],
    }
    assert session_service.session.messages[0]["peer_id"] == "web-visitor-alice"


@pytest.mark.asyncio
async def test_read_resource_directory_abstract_uses_parent_abstract(request_context):
    service = ResourceMemoryLinkService(
        viking_fs=_FakeVikingFS({"viking://resources/images/.abstract.md": "动漫角色照片合集"})
    )

    abstract = await service._read_resource_directory_abstract(
        "viking://resources/images/yueqian.jpeg",
        request_context,
    )

    assert abstract == "动漫角色照片合集"


@pytest.mark.asyncio
async def test_read_resource_directory_abstract_ignores_missing_or_not_ready(
    request_context,
):
    service = ResourceMemoryLinkService(viking_fs=_FakeVikingFS({}))

    missing = await service._read_resource_directory_abstract(
        "viking://resources/images/yueqian.jpeg",
        request_context,
    )

    assert missing == ""

    service = ResourceMemoryLinkService(
        viking_fs=_FakeVikingFS(
            {
                "viking://resources/images/.abstract.md": (
                    "# viking://resources/images [Directory abstract is not ready]"
                )
            }
        )
    )

    not_ready = await service._read_resource_directory_abstract(
        "viking://resources/images/yueqian.jpeg",
        request_context,
    )

    assert not_ready == ""


@pytest.mark.asyncio
async def test_find_referencing_memories_uses_memory_refs(request_context):
    memory_uri = "viking://user/alice/memories/entities/wang.md"
    resource_uri = "viking://resources/docs/id_card.pdf"
    raw = (
        "王大锤资料。\n\n"
        "<!-- MEMORY_FIELDS\n"
        "{\n"
        '  "resource_refs": [\n'
        "    {\n"
        f'      "resource_uri": "{resource_uri}",\n'
        '      "reason": "这是王大锤的身份证"\n'
        "    }\n"
        "  ]\n"
        "}\n"
        "-->"
    )
    service = ResourceMemoryLinkService(viking_fs=_FakeVikingFS({memory_uri: raw}))

    matches = await service._find_referencing_memories(
        ctx=request_context,
        resource_uri="viking://resources/docs",
        recursive=True,
    )

    assert matches == [memory_uri]


@pytest.mark.asyncio
async def test_find_referencing_memories_uses_grep_candidates_without_tree_scan(request_context):
    memory_uri = "viking://user/alice/memories/entities/wang.md"
    unrelated_uri = "viking://user/alice/memories/entities/unrelated.md"
    overview_uri = "viking://user/alice/memories/entities/.overview.md"
    resource_uri = "viking://resources/docs/id_card.pdf"
    raw = MemoryFileUtils.write(
        MemoryFile(
            uri=memory_uri,
            content="王大锤资料。",
            extra_fields={
                "resource_refs": [
                    {
                        "resource_uri": resource_uri,
                        "reason": "这是王大锤的身份证",
                    }
                ]
            },
        )
    )
    store = {
        memory_uri: raw,
        unrelated_uri: "不会命中的普通记忆",
        overview_uri: f"- [王大锤]({memory_uri})",
    }
    viking_fs = _FakeGrepVikingFS(
        store,
        grep_uris=[memory_uri, memory_uri, overview_uri],
    )
    service = ResourceMemoryLinkService(viking_fs=viking_fs)

    matches = await service._find_referencing_memories(
        ctx=request_context,
        resource_uri=resource_uri,
        recursive=False,
    )

    assert matches == [memory_uri]
    assert viking_fs.grep_calls == [
        {
            "uri": "viking://user/alice/memories",
            "pattern": re.escape(resource_uri),
            "exclude_uri": None,
            "case_insensitive": False,
            "node_limit": None,
            "level_limit": None,
        }
    ]
    assert viking_fs.read_calls == [memory_uri]
    assert viking_fs.tree_calls == []


@pytest.mark.asyncio
async def test_find_referencing_memories_scans_actor_peer_memory(request_context):
    peer_ctx = RequestContext(
        user=request_context.user,
        role=request_context.role,
        actor_peer_id="web-visitor-alice",
    )
    memory_uri = "viking://user/alice/peers/web-visitor-alice/memories/entities/wang.md"
    resource_uri = "viking://resources/docs/id_card.pdf"
    raw = (
        "王大锤资料。\n\n"
        "<!-- MEMORY_FIELDS\n"
        "{\n"
        '  "resource_refs": [\n'
        "    {\n"
        f'      "resource_uri": "{resource_uri}",\n'
        '      "reason": "这是王大锤的身份证"\n'
        "    }\n"
        "  ]\n"
        "}\n"
        "-->"
    )
    service = ResourceMemoryLinkService(viking_fs=_FakeVikingFS({memory_uri: raw}))

    matches = await service._find_referencing_memories(
        ctx=peer_ctx,
        resource_uri=resource_uri,
        recursive=True,
    )

    assert matches == [memory_uri]


@pytest.mark.asyncio
async def test_unlink_memory_reference_keeps_visible_text_and_no_schema_metadata(request_context):
    memory_uri = "viking://user/ryoma/memories/entities/动漫角色/不二周助-write-test3.md"
    resource_uri = "viking://resources/images/2026/06/10/不二周助_jpeg"
    original_raw = MemoryFileUtils.write(
        MemoryFile(
            uri=memory_uri,
            content=f"今天是清明节。[用户保存了一张不二周助的照片]({resource_uri})",
            extra_fields={
                "resource_refs": [
                    {
                        "resource_uri": resource_uri,
                        "source": "content.write",
                    }
                ]
            },
        )
    )
    store = {memory_uri: original_raw}
    service = ResourceMemoryLinkService(viking_fs=_FakeVikingFS(store))

    result = await service._unlink_memory_reference(
        ctx=request_context,
        memory_uri=memory_uri,
        resource_uri=resource_uri,
    )

    assert result.edited_uris == [memory_uri]
    mf = MemoryFileUtils.read(store[memory_uri], uri=memory_uri)
    assert mf.content == "今天是清明节。用户保存了一张不二周助的照片"
    assert mf.extra_fields == {"version": 1}
    assert mf.memory_type is None


@pytest.mark.asyncio
async def test_unlink_memory_reference_does_not_delete_event_memory(
    request_context,
):
    memory_uri = "viking://user/ryoma/memories/events/2026/06/11/越前龙马.md"
    resource_uri = "viking://resources/images/2026/06/11/yueqian_jpeg"
    original_raw = MemoryFileUtils.write(
        MemoryFile(
            uri=memory_uri,
            content=f"[用户保存了一张越前龙马的照片]({resource_uri})",
            extra_fields={
                "category": "动漫角色",
                "name": "越前龙马",
                "user_id": "ryoma",
                "memory_type": "entities",
            },
        )
    )
    store = {memory_uri: original_raw}
    service = ResourceMemoryLinkService(viking_fs=_FakeVikingFS(store))

    result = await service._unlink_memory_reference(
        ctx=request_context,
        memory_uri=memory_uri,
        resource_uri=resource_uri,
    )

    assert memory_uri in store
    assert service._get_viking_fs().rm_calls == []
    assert result.edited_uris == [memory_uri]
    assert result.deleted_uris == []
    mf = MemoryFileUtils.read(store[memory_uri], uri=memory_uri)
    assert mf.content == "用户保存了一张越前龙马的照片"


@pytest.mark.asyncio
async def test_prepared_resource_delete_cleans_visible_uri_without_resource_refs(
    request_context,
    cleanup_queue,
):
    memory_uri = "viking://user/alice/memories/events/2026/06/11/yueqian.md"
    resource_uri = "viking://resources/images/2026/06/12/yueqian_jpeg"
    raw = MemoryFileUtils.write(
        MemoryFile(
            uri=memory_uri,
            content=(
                f"今天是清明节。\n用户昨晚查看了[越前龙马照片]({resource_uri})，之后可参考该资源。"
            ),
            extra_fields={"memory_type": "events"},
        )
    )
    store = {memory_uri: raw}
    service = ResourceMemoryLinkService(viking_fs=_FakeVikingFS(store))

    result = await service.prepare_resource_delete(
        ctx=request_context,
        resource_uri=resource_uri,
    )

    assert result["status"] == "pending"
    await _drain_cleanup(service, cleanup_queue)
    mf = MemoryFileUtils.read(store[memory_uri], uri=memory_uri)
    assert mf.content == "今天是清明节。\n用户昨晚查看了越前龙马照片，之后可参考该资源。"
    assert "resource_refs" not in mf.extra_fields


@pytest.mark.asyncio
async def test_prepared_resource_delete_exact_keeps_child_resource_refs(
    request_context,
    cleanup_queue,
):
    memory_uri = "viking://user/alice/memories/entities/photos.md"
    resource_uri = "viking://resources/images/album"
    child_uri = f"{resource_uri}/child.jpeg"
    raw = MemoryFileUtils.write(
        MemoryFile(
            uri=memory_uri,
            content=(
                f"用户保存了[相册资源]({resource_uri})。\n用户保存了[相册里的子图]({child_uri})。"
            ),
            extra_fields={
                "resource_refs": [
                    {"resource_uri": resource_uri, "source": "content.write"},
                    {"resource_uri": child_uri, "source": "content.write"},
                ],
            },
        )
    )
    store = {memory_uri: raw}
    service = ResourceMemoryLinkService(viking_fs=_FakeVikingFS(store))

    result = await service.prepare_resource_delete(
        ctx=request_context,
        resource_uri=resource_uri,
        recursive=False,
    )

    assert result["status"] == "pending"
    await _drain_cleanup(service, cleanup_queue)
    mf = MemoryFileUtils.read(store[memory_uri], uri=memory_uri)
    assert f"[相册资源]({resource_uri})" not in mf.content
    assert "用户保存了相册资源。" in mf.content
    assert f"[相册里的子图]({child_uri})" in mf.content
    refs = mf.extra_fields["resource_refs"]
    assert refs == [{"resource_uri": child_uri, "source": "content.write"}]


@pytest.mark.asyncio
async def test_assert_resource_unlinked_propagates_non_not_found_errors(request_context):
    service = ResourceMemoryLinkService(viking_fs=_ReadFailVikingFS())

    with pytest.raises(RuntimeError, match="storage unavailable"):
        await service._assert_resource_unlinked(
            "viking://user/alice/memories/entities/wang.md",
            "viking://resources/id_card.pdf",
            request_context,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["enqueue", "delete", "task_store", "pending", "replacement"])
async def test_resource_cleanup_survives_delete_failures_and_preserves_replacements(
    request_context, cleanup_queue, monkeypatch, failure
):
    resource_uri = "viking://resources/report.pdf"
    memory_uri = "viking://user/alice/memories/entities/report.md"
    raw = MemoryFileUtils.write(
        MemoryFile(
            uri=memory_uri,
            content=f"A report [saved]({resource_uri})",
            extra_fields={"resource_refs": [{"resource_uri": resource_uri}]},
        )
    )
    fs = _FakeVikingFS({resource_uri: "resource", memory_uri: raw})
    links = ResourceMemoryLinkService(viking_fs=fs)
    files = FSService(
        viking_fs=fs,
        resource_memory_link_service=links,
        uri_mutation_coordinator=links._uri_mutation_coordinator,
    )
    files._semantic_refresh_parent_uri = lambda *_: None
    files._sync_watch_after_rm = AsyncMock()
    original_rm = fs.rm

    async def checked_rm(*args, **kwargs):
        assert cleanup_queue and resource_uri in fs.store
        if failure == "delete":
            raise RuntimeError("delete failed")
        return await original_rm(*args, **kwargs)

    fs.rm = AsyncMock(side_effect=checked_rm)
    if failure == "enqueue":
        monkeypatch.setattr(
            "openviking.service.resource_memory_link_service.get_queue_manager",
            lambda: SimpleNamespace(enqueue=AsyncMock(side_effect=RuntimeError("queue failed"))),
        )
    if failure in {"enqueue", "delete"}:
        with pytest.raises(RuntimeError, match="failed"):
            await files._rm(resource_uri, request_context)
        assert resource_uri in fs.store and fs.store[memory_uri] == raw
        if failure == "enqueue":
            fs.rm.assert_not_awaited()
        else:
            await _drain_cleanup(ResourceMemoryLinkService(viking_fs=fs), cleanup_queue)
        return

    result = await files._rm(resource_uri, request_context)
    assert result["memory_cleanup"]["status"] == "pending"
    assert resource_uri not in fs.store
    tracker = SimpleNamespace(
        list_tasks=AsyncMock(return_value=[]),
        get=AsyncMock(return_value=SimpleNamespace(status=TaskStatus.RUNNING)),
        has_work=lambda _: False,
    )
    monkeypatch.setattr("openviking.service.task_tracker.get_task_tracker", lambda: tracker)
    if failure == "task_store":
        tracker.list_tasks.side_effect = RuntimeError("task store unavailable")
    else:
        tracker.list_tasks.return_value = [
            SimpleNamespace(
                task_id="old-reason",
                resource_id=_RESOURCE_REASON_SESSION_ID,
                status=TaskStatus.RUNNING,
            )
        ]
    await links.resume_cleanup(cleanup_queue.pop(0))
    assert cleanup_queue and fs.store[memory_uri] == raw
    tracker.list_tasks.side_effect = None
    if failure == "replacement":
        fs.store[resource_uri] = "replacement"
        fs.store[memory_uri] = raw.replace("A report", "Replacement report")
    if failure == "pending":
        tracker.get.return_value.status = TaskStatus.COMPLETED
    # Replay through a fresh service to exercise persisted ownership, not local callbacks.
    await _drain_cleanup(ResourceMemoryLinkService(viking_fs=fs), cleanup_queue)
    assert (resource_uri in fs.store) == (failure == "replacement")
    assert ("resource_refs" in MemoryFileUtils.read(fs.store[memory_uri]).extra_fields) == (
        failure == "replacement"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapped", [False, True])
async def test_commit_processor_dispatches_resource_cleanup(wrapped):
    payload = {"resource_memory_cleanup": True}
    links = SimpleNamespace(resume_cleanup=AsyncMock(return_value=ProcessResult.success()))
    processor = SessionCommitProcessor(SimpleNamespace(), links)

    result = await processor.on_dequeue({"data": json.dumps(payload)} if wrapped else payload)

    assert result.outcome == "success"
    links.resume_cleanup.assert_awaited_once_with(payload)


@pytest.mark.asyncio
async def test_cleanup_finishes_when_checkpointed_memory_is_deleted(request_context, cleanup_queue):
    resource_uri = "viking://resources/report"
    memory_uri = "viking://user/alice/memories/entities/report.md"
    fs = _FakeVikingFS({memory_uri: f"A report [saved]({resource_uri})"})
    links = ResourceMemoryLinkService(viking_fs=fs)
    await links.prepare_resource_delete(ctx=request_context, resource_uri=resource_uri)
    await links.resume_cleanup(cleanup_queue.pop(0))
    assert cleanup_queue[0]["memory_uris"] == [memory_uri]
    del fs.store[memory_uri]

    result = await links.resume_cleanup(cleanup_queue.pop(0))

    assert result.outcome == "success"
    assert not cleanup_queue and memory_uri not in fs.store


@pytest.mark.asyncio
async def test_namespace_cleanup_borrows_parent_lock_without_recreating_resources(
    tmp_path, request_context
):
    from openviking.storage.viking_fs import VikingFS
    from openviking.utils.agfs_utils import RagfsBindingConfig, create_agfs_client
    from openviking_cli.utils.config.agfs_config import AGFSConfig

    fs = VikingFS(
        create_agfs_client(
            RagfsBindingConfig(
                agfs=AGFSConfig(path=str(tmp_path), backend="memory"),
            )
        )
    )
    memory_uri = "viking://user/alice/memories/entities/report.md"
    resource_uri = "viking://resources/report"
    raw = MemoryFileUtils.write(
        MemoryFile(
            uri=memory_uri,
            content=f"A report [saved]({resource_uri})",
            extra_fields={"resource_refs": [{"resource_uri": resource_uri}]},
        )
    )
    await fs.write_file(memory_uri, raw, ctx=request_context)
    links = ResourceMemoryLinkService(viking_fs=fs)
    async with links.resource_deletion_guard(request_context, "viking://resources") as guard:
        assert guard is not None and guard[1]
        await links._unlink_memory_reference(
            ctx=request_context,
            memory_uri=memory_uri,
            resource_uri="viking://resources",
            recursive=True,
            lease_ref=guard[0],
        )
    assert not await fs.exists("viking://resources", ctx=request_context)
    assert (
        "resource_refs"
        not in MemoryFileUtils.read(
            await fs.read_file(memory_uri, ctx=request_context)
        ).extra_fields
    )
