# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Link resource addition reasons to user memories.

This module keeps resource files immutable: all traceability lives in memory
files' MEMORY_FIELDS metadata.
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, AsyncIterator, Dict, List, Optional

from openviking.concurrency import AsyncSemaphore
from openviking.core.namespace import (
    NamespaceShapeError,
    canonical_user_root,
    context_type_for_uri,
    uri_parts,
)
from openviking.core.peer_id import normalize_peer_id
from openviking.message.part import TextPart
from openviking.pyagfs.exceptions import AGFSMountPointNotFoundError
from openviking.resource.uri_mutation_coordinator import UriMutationCoordinator
from openviking.server.error_mapping import is_not_found_error
from openviking.server.identity import RequestContext, Role
from openviking.service.task_work_index import detach_task_context
from openviking.session.memory.memory_updater import MemoryUpdater, MemoryUpdateResult
from openviking.session.memory.utils.memory_file_utils import MemoryFileUtils
from openviking.session.memory.utils.resource_refs import (
    content_references_resource,
    resource_ref_matches,
    unlink_resource_references_from_memory,
)
from openviking.storage.abstract_overview import body_for_preview
from openviking.storage.queuefs import QueueManager, get_queue_manager
from openviking.storage.queuefs.process_result import ProcessResult
from openviking.storage.viking_fs import VikingFS, get_viking_fs
from openviking.storage.vikingdb_manager import VikingDBManager
from openviking_cli.exceptions import NotFoundError
from openviking_cli.session.user_id import UserIdentifier
from openviking_cli.utils import get_logger

if TYPE_CHECKING:
    from openviking.service.session_service import SessionService

logger = get_logger(__name__)

_RESOURCE_REASON_SESSION_ID = "__openviking_resource_reason__"
_RESOURCE_REASON_SESSION_PREFIX = "__reason__"
_RESOURCE_REASON_MEMORY_TYPES = ["entities", "events", "preferences"]
_RESOURCE_ABSTRACT_MAX_CHARS = 200
_ABSTRACT_NOT_READY_MARKERS = (
    "[.abstract.md is not ready]",
    "[Directory abstract is not ready]",
)


def _resource_reason_memory_policy(target_peer_id: Optional[str] = None) -> Dict[str, Any]:
    """Limit reason extraction to resource-related memories owned by the user or target peer."""
    peer_targeted = bool(target_peer_id)
    return {
        "self": {"enabled": not peer_targeted},
        "peer": {"enabled": peer_targeted},
        "memory_types": list(_RESOURCE_REASON_MEMORY_TYPES),
    }


def _resource_reason_peer_id(ctx: RequestContext, resource_uri: str) -> Optional[str]:
    actor_peer_id = normalize_peer_id(ctx.actor_peer_id)
    if actor_peer_id:
        return actor_peer_id
    return _peer_id_from_resource_uri(resource_uri, ctx)


def _peer_id_from_resource_uri(resource_uri: str, ctx: RequestContext) -> Optional[str]:
    try:
        parts = uri_parts(resource_uri)
    except (NamespaceShapeError, ValueError):
        return None
    if len(parts) >= 5 and parts[0] == "user" and parts[2] == "peers":
        return normalize_peer_id(parts[3])
    return None


def _memory_roots_for_resource_refs(ctx: RequestContext, resource_uri: str) -> List[str]:
    user_root = canonical_user_root(ctx)
    target_peer_id = _resource_reason_peer_id(ctx, resource_uri)
    if target_peer_id:
        return [f"{user_root}/peers/{target_peer_id}/memories"]
    return [f"{user_root}/memories"]


class ResourceMemoryLinkService:
    """Create and clean memory references for resources added with a reason."""

    def __init__(
        self,
        *,
        vikingdb: Optional[VikingDBManager] = None,
        viking_fs: Optional[VikingFS] = None,
        session_service: Optional["SessionService"] = None,
        uri_mutation_coordinator: Optional[UriMutationCoordinator] = None,
    ):
        self._vikingdb = vikingdb
        self._viking_fs = viking_fs
        self._session_service = session_service
        self._uri_mutation_coordinator = uri_mutation_coordinator or UriMutationCoordinator()
        # Additions run on queue loops; deletions can run on the HTTP loop.
        self._reason_session_lock = AsyncSemaphore()

    def set_dependencies(
        self,
        *,
        vikingdb: Optional[VikingDBManager],
        viking_fs: VikingFS,
        session_service: Optional["SessionService"] = None,
    ) -> None:
        self._vikingdb = vikingdb
        self._viking_fs = viking_fs
        if session_service is not None:
            self._session_service = session_service

    def _get_viking_fs(self) -> VikingFS:
        return self._viking_fs or get_viking_fs()

    async def on_resource_added(
        self,
        *,
        ctx: RequestContext,
        resource_uri: str,
        reason: str,
        source_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Durably enqueue a resource reason; return its task ID without waiting for extraction."""
        reason = (reason or "").strip()
        if not reason:
            return {"status": "skipped", "reason": "empty_reason"}
        if not resource_uri:
            return {"status": "skipped", "reason": "empty_resource_uri"}
        if not self._session_service:
            return {"status": "skipped", "reason": "session_service_unavailable"}

        added_at = datetime.now(timezone.utc).isoformat()
        resource_uri = "viking://" + "/".join(uri_parts(resource_uri))
        session_id = _RESOURCE_REASON_SESSION_ID
        target_peer_id = _resource_reason_peer_id(ctx, resource_uri)
        commit_result: Dict[str, Any] = {}

        async with (
            self._uri_mutation_coordinator.access(ctx.account_id, [resource_uri]),
            self._reason_session_lock,
        ):
            if not await self._get_viking_fs().exists(resource_uri, ctx=ctx):
                return {"status": "skipped", "reason": "resource_not_found"}
            resource_abstract = await self._read_resource_directory_abstract(resource_uri, ctx)
            session = await self._session_service.get(
                session_id,
                ctx,
                auto_create=True,
            )
            message_spec: Dict[str, Any] = {
                "role": "user",
                "parts": [
                    TextPart(
                        text=self._build_resource_addition_message(
                            resource_uri=resource_uri,
                            reason=reason,
                            source_name=source_name,
                            added_at=added_at,
                            resource_abstract=resource_abstract,
                        )
                    )
                ],
                "created_at": added_at,
            }
            if target_peer_id:
                message_spec["peer_id"] = target_peer_id
            await session.add_messages_async([message_spec])
            commit_result = await self._session_service.commit_async(
                session_id,
                ctx,
                keep_recent_count=0,
                memory_policy=_resource_reason_memory_policy(target_peer_id),
            )

        task_id = commit_result.get("task_id")
        return {
            "status": "accepted",
            "session_id": session_id,
            "commit_task_id": task_id,
            "archive_uri": commit_result.get("archive_uri"),
            "commit_task": None,
        }

    @staticmethod
    def _build_resource_addition_message(
        *,
        resource_uri: str,
        reason: str,
        source_name: Optional[str],
        added_at: str,
        resource_abstract: str,
    ) -> str:
        return (
            "## Resource Addition\n"
            f"Resource URI: {resource_uri}\n"
            f"Source name: {source_name or 'N/A'}\n"
            f"Added at: {added_at or 'N/A'}\n"
            f"Resource abstract: {resource_abstract or 'N/A'}\n"
            f"User reason: {reason}"
        )

    async def prepare_resource_delete(
        self,
        *,
        ctx: RequestContext,
        resource_uri: str,
        recursive: bool = False,
    ) -> Dict[str, Any]:
        """Persist cleanup before deletion; enqueue failure prevents the caller from deleting.

        The caller holds the resource mutation lease until deletion finishes.
        Consumers leave memories untouched when the resource still exists.
        """
        if context_type_for_uri(resource_uri) != "resource":
            return {"status": "skipped", "reason": "not_resource"}

        with detach_task_context():
            await get_queue_manager().enqueue(
                QueueManager.SESSION_COMMIT,
                {
                    "resource_memory_cleanup": True,
                    "user": ctx.user.to_dict(),
                    "role": str(ctx.role),
                    "group_ids": list(ctx.group_ids),
                    "actor_peer_id": ctx.actor_peer_id,
                    "bypass_acl": ctx.bypass_acl,
                    "resource_uri": resource_uri,
                    "recursive": recursive,
                },
            )
        return {"status": "pending", "memory_uris": []}

    async def _unlink_memory_reference(
        self,
        *,
        ctx: RequestContext,
        memory_uri: str,
        resource_uri: str,
        recursive: bool = False,
        lease_ref: Optional[Dict[str, Any]] = None,
    ) -> MemoryUpdateResult:
        """Atomically unlink current references; borrow a covering namespace lease when supplied."""
        viking_fs = self._get_viking_fs()
        memory_lease = await viking_fs._async_agfs.pathlock_acquire_exact(
            viking_fs._uri_to_path(memory_uri, ctx=ctx), owner_lease_ref=lease_ref
        )
        try:
            if await viking_fs.exists(resource_uri, ctx=ctx):
                return MemoryUpdateResult()
            try:
                raw = await viking_fs.read_file(memory_uri, ctx=ctx)
                current = MemoryFileUtils.read(raw, uri=memory_uri)
            except (NotFoundError, FileNotFoundError):
                result = MemoryUpdateResult()
                result.add_deleted(memory_uri)
                return result

            changed = unlink_resource_references_from_memory(
                current,
                resource_uri,
                recursive=recursive,
            )
            result = MemoryUpdateResult()
            if not changed:
                return result

            await viking_fs.write_file(
                memory_uri, MemoryFileUtils.write(current), ctx=ctx, lease_ref=memory_lease
            )
            result.add_edited(memory_uri)
            return result
        finally:
            await viking_fs._async_agfs.pathlock_release(memory_lease)

    async def _find_referencing_memories(
        self,
        *,
        ctx: RequestContext,
        resource_uri: str,
        recursive: bool,
    ) -> List[str]:
        """Return unique memory URIs matching the resource in metadata or visible content."""
        candidate_uris = await self._grep_candidate_memory_uris(
            ctx=ctx,
            resource_uri=resource_uri,
        )
        if candidate_uris is None:
            raise RuntimeError("Resource memory reference scan is unavailable")
        viking_fs = self._get_viking_fs()
        matches = []
        for uri in candidate_uris:
            try:
                raw = await viking_fs.read_file(uri, ctx=ctx)
            except (NotFoundError, FileNotFoundError):
                continue
            mf = MemoryFileUtils.read(raw, uri=uri)
            if any(
                resource_ref_matches(ref.get("resource_uri"), resource_uri, recursive=recursive)
                for ref in self._coerce_resource_refs(mf.extra_fields.get("resource_refs"))
            ) or content_references_resource(mf.content, resource_uri, recursive=recursive):
                matches.append(uri)
        return matches

    async def _grep_candidate_memory_uris(
        self,
        *,
        ctx: RequestContext,
        resource_uri: str,
    ) -> Optional[List[str]]:
        """Use backend grep to avoid reading every memory file when supported."""
        viking_fs = self._get_viking_fs()
        if not hasattr(viking_fs, "grep"):
            return None

        search_needle = self._resource_uri_search_needle(resource_uri)
        if not search_needle:
            return []

        candidate_uris: List[str] = []
        for memory_root in _memory_roots_for_resource_refs(ctx, resource_uri):
            try:
                result = await viking_fs.grep(
                    memory_root,
                    pattern=re.escape(search_needle),
                    ctx=ctx,
                    node_limit=None,
                    level_limit=None,
                )
            except (NotFoundError, FileNotFoundError):
                continue

            for match in result.get("matches", []):
                uri = match.get("uri", "")
                if self._is_memory_markdown_file(uri):
                    candidate_uris.append(uri)

        return list(dict.fromkeys(candidate_uris))

    @staticmethod
    def _resource_uri_search_needle(resource_uri: str) -> str:
        return (resource_uri or "").strip().rstrip("/")

    @staticmethod
    def _is_memory_markdown_file(uri: str) -> bool:
        return (
            isinstance(uri, str)
            and uri.endswith(".md")
            and not uri.endswith("/.abstract.md")
            and not uri.endswith("/.overview.md")
        )

    async def _read_resource_directory_abstract(
        self,
        resource_uri: str,
        ctx: RequestContext,
    ) -> str:
        """Best-effort directory abstract lookup for resource-addition readability."""
        viking_fs = self._get_viking_fs()
        for abstract_uri in self._resource_abstract_uri_candidates(resource_uri):
            try:
                abstract = await viking_fs.read_file(abstract_uri, ctx=ctx)
            except Exception:
                continue
            abstract = self._clean_resource_abstract(abstract)
            if abstract:
                return abstract
        return ""

    @classmethod
    def _resource_abstract_uri_candidates(cls, resource_uri: str) -> List[str]:
        normalized = (resource_uri or "").strip().rstrip("/")
        if not normalized:
            return []
        candidates = [f"{normalized}/.abstract.md"]
        parent = cls._parent_uri(normalized)
        if parent:
            candidates.append(f"{parent}/.abstract.md")
        return list(dict.fromkeys(candidates))

    @staticmethod
    def _parent_uri(uri: str) -> str:
        scheme_index = uri.find("://")
        min_slash_index = scheme_index + 3 if scheme_index >= 0 else 0
        slash_index = uri.rfind("/")
        if slash_index <= min_slash_index:
            return ""
        return uri[:slash_index]

    @staticmethod
    def _clean_resource_abstract(abstract: Any) -> str:
        text = " ".join(body_for_preview(str(abstract or "")).split())
        if not text:
            return ""
        if any(text == marker or text.endswith(marker) for marker in _ABSTRACT_NOT_READY_MARKERS):
            return ""
        if len(text) > _RESOURCE_ABSTRACT_MAX_CHARS:
            return text[: _RESOURCE_ABSTRACT_MAX_CHARS - 3].rstrip() + "..."
        return text

    async def _assert_resource_unlinked(
        self,
        memory_uri: str,
        resource_uri: str,
        ctx: RequestContext,
        *,
        recursive: bool = True,
    ) -> None:
        """Verify remaining files have no stale reference; missing files are already unlinked."""
        try:
            raw = await self._get_viking_fs().read_file(memory_uri, ctx=ctx)
        except (NotFoundError, FileNotFoundError):
            return
        mf = MemoryFileUtils.read(raw, uri=memory_uri)
        if content_references_resource(mf.content, resource_uri, recursive=recursive):
            raise RuntimeError(f"memory content still contains deleted resource URI: {memory_uri}")
        for ref in self._coerce_resource_refs(mf.extra_fields.get("resource_refs")):
            if resource_ref_matches(
                ref.get("resource_uri"),
                resource_uri,
                recursive=recursive,
            ):
                raise RuntimeError(f"memory still contains resource ref: {memory_uri}")

    @staticmethod
    def _coerce_resource_refs(value: Any) -> List[Dict[str, Any]]:
        if isinstance(value, list):
            return [dict(item) for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            return [dict(value)]
        return []

    @asynccontextmanager
    async def resource_deletion_lock(
        self, ctx: RequestContext, resource_uri: str
    ) -> AsyncIterator[tuple[Dict[str, Any], bool]]:
        """Hold an existing parent's tree lease across resource deletion or cleanup.

        Yield the lease and whether its scope covers the affected memory roots.
        Memory writes inside the parent scope must borrow this lease.
        Missing ancestors are skipped so acquiring the lease does not recreate
        the deleted resource. Storage and lock errors propagate to the caller.
        """
        viking_fs = self._get_viking_fs()
        agfs = viking_fs._async_agfs
        parent_path = viking_fs._uri_to_path(resource_uri, ctx=ctx).rsplit("/", 1)[0]
        while parent_path:
            try:
                await agfs.stat(parent_path)
                break
            except Exception as exc:
                if isinstance(exc, AGFSMountPointNotFoundError) or not is_not_found_error(exc):
                    raise
                parent_path = parent_path.rsplit("/", 1)[0]
        if not parent_path:
            raise NotFoundError(resource_uri)
        lease = await agfs.pathlock_acquire_tree(parent_path)
        try:
            memory_paths = [
                viking_fs._uri_to_path(uri, ctx=ctx)
                for uri in _memory_roots_for_resource_refs(ctx, resource_uri)
            ]
            covers_memories = any(
                path == parent_path or path.startswith(parent_path + "/") for path in memory_paths
            )
            yield lease, covers_memories
        finally:
            await agfs.pathlock_release(lease)

    @asynccontextmanager
    async def resource_deletion_guard(
        self, ctx: RequestContext, resource_uri: str
    ) -> AsyncIterator[Optional[tuple[Dict[str, Any], bool]]]:
        """Fence cleanup against replacement uploads; yield None while the resource exists.

        The URI lease orders consumers after a prepared deletion in this process.
        The parent tree lease also excludes writes from other workers and processes.
        """
        async with (
            self._uri_mutation_coordinator.mutation(ctx.account_id, [resource_uri]),
            self.resource_deletion_lock(ctx, resource_uri) as guard,
        ):
            yield None if await self._get_viking_fs().exists(resource_uri, ctx=ctx) else guard

    async def resume_cleanup(self, payload: Dict[str, Any]) -> ProcessResult:
        """Resume durable cleanup without waiting for commits in a SessionCommit worker.

        Unsettled dependencies and cleanup errors requeue the current phase.
        memory_uris checkpoints the final scan so index refresh work survives
        an interrupted unlink.
        """
        from openviking.service.task_tracker import TaskStatus, get_task_tracker

        ctx = RequestContext(
            user=UserIdentifier.from_dict(payload["user"]),
            role=Role(payload["role"]),
            group_ids=tuple(payload["group_ids"]),
            actor_peer_id=payload["actor_peer_id"],
            bypass_acl=payload["bypass_acl"],
        )
        args = {
            "ctx": ctx,
            "resource_uri": payload["resource_uri"],
            "recursive": payload["recursive"],
        }
        try:
            if "pending_task_ids" not in payload:
                payload["pending_task_ids"] = await self._pending_reason_task_ids(ctx)
            tracker = get_task_tracker()
            pending_ids = []
            for task_id in payload["pending_task_ids"]:
                task = await tracker.get(
                    task_id, account_id=ctx.account_id, user_id=ctx.user.user_id
                )
                # Terminal records expire; durable or active work still fences a missing record.
                pending = (
                    tracker.has_work(task_id)
                    if task is None
                    else task.status
                    not in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED)
                )
                if pending:
                    pending_ids.append(task_id)
            payload["pending_task_ids"] = pending_ids
            refresh_ready = False
            async with self.resource_deletion_guard(ctx, payload["resource_uri"]) as guard:
                if guard is None:
                    if "memory_uris" not in payload:
                        return ProcessResult.success()
                    # Index retries read the current file and never unlink a replacement resource.
                    refresh_ready = True
                else:
                    if not pending_ids:
                        if "memory_uris" not in payload:
                            payload["memory_uris"] = await self._find_referencing_memories(**args)
                            if not payload["memory_uris"]:
                                return ProcessResult.success()
                        else:
                            lease_ref, covers_memories = guard
                            for memory_uri in payload["memory_uris"]:
                                await self._unlink_memory_reference(
                                    **args,
                                    memory_uri=memory_uri,
                                    lease_ref=lease_ref if covers_memories else None,
                                )
                                if not await self._get_viking_fs().exists(
                                    payload["resource_uri"], ctx=ctx
                                ):
                                    await self._assert_resource_unlinked(
                                        memory_uri,
                                        payload["resource_uri"],
                                        ctx,
                                        recursive=payload["recursive"],
                                    )
                            refresh_ready = True

            if refresh_ready:
                viking_fs = self._get_viking_fs()
                for uri in payload["memory_uris"]:
                    if await viking_fs.exists(uri, ctx=ctx):
                        await MemoryUpdater.refresh_file_embedding(
                            viking_fs=viking_fs,
                            vikingdb=self._vikingdb,
                            uri=uri,
                            memory_type=MemoryUpdater.memory_type_from_uri(uri),
                            ctx=ctx,
                            strict=True,
                        )
                for directory_uri in {uri.rsplit("/", 1)[0] for uri in payload["memory_uris"]}:
                    if await viking_fs.exists(directory_uri, ctx=ctx):
                        await MemoryUpdater.refresh_schema_overview(
                            viking_fs=viking_fs,
                            directory_uri=directory_uri,
                            ctx=ctx,
                            strict=True,
                        )
                return ProcessResult.success()
        except Exception as exc:
            logger.warning(
                "Deferred resource memory cleanup failed for %s: %s", payload["resource_uri"], exc
            )
        with detach_task_context():
            await get_queue_manager().enqueue(QueueManager.SESSION_COMMIT, payload)
        return ProcessResult.requeued()

    async def _pending_reason_task_ids(self, ctx: RequestContext) -> List[str]:
        """Return this user's unsettled resource reason tasks without waiting for extraction."""
        from openviking.service.task_tracker import TaskStatus, get_task_tracker

        tasks = await get_task_tracker().list_tasks(
            task_type="session_commit",
            account_id=ctx.account_id,
            user_id=ctx.user.user_id,
            limit=None,
        )
        return [
            task.task_id
            for task in tasks
            if task.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED)
            and (
                task.resource_id == _RESOURCE_REASON_SESSION_ID
                or (task.resource_id or "").startswith(_RESOURCE_REASON_SESSION_PREFIX)
            )
        ]
