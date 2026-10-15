# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Link resource addition reasons to user memories.

This module keeps resource files immutable: all traceability lives in memory
files' MEMORY_FIELDS metadata.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence

from openviking.concurrency import AsyncSemaphore
from openviking.core.namespace import (
    NamespaceShapeError,
    canonical_user_root,
    context_type_for_uri,
    uri_parts,
)
from openviking.core.peer_id import normalize_peer_id
from openviking.message.part import TextPart
from openviking.resource.uri_mutation_coordinator import UriMutationCoordinator
from openviking.server.identity import RequestContext, Role
from openviking.service.task_work_index import detach_task_context
from openviking.session.memory.dataclass import MemoryFile
from openviking.session.memory.memory_updater import MemoryUpdater, MemoryUpdateResult
from openviking.session.memory.utils.memory_file_utils import MemoryFileUtils
from openviking.session.memory.utils.resource_refs import (
    content_references_resource,
    extract_resource_uris,
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
_RESOURCE_DELETION_MEMORY_TYPES = ["entities", "preferences"]
_RESOURCE_REASON_COMMIT_TIMEOUT_SECONDS = 1800.0
_RESOURCE_ABSTRACT_MAX_CHARS = 200
_ABSTRACT_NOT_READY_MARKERS = (
    "[.abstract.md is not ready]",
    "[Directory abstract is not ready]",
)


def _resource_reason_session_id(resource_uri: str) -> str:
    """Return the stable reason session ID for a normalized resource URI."""
    canonical_uri = "viking://" + "/".join(uri_parts(resource_uri))
    digest = hashlib.sha256(canonical_uri.encode("utf-8")).hexdigest()[:32]
    return f"{_RESOURCE_REASON_SESSION_PREFIX}{digest}"


def _resource_memory_policy(
    *,
    memory_types: Sequence[str],
    target_peer_id: Optional[str] = None,
) -> Dict[str, Any]:
    peer_targeted = bool(target_peer_id)
    return {
        "self": {"enabled": not peer_targeted},
        "peer": {"enabled": peer_targeted},
        "memory_types": list(memory_types),
    }


def _resource_reason_memory_policy(target_peer_id: Optional[str] = None) -> Dict[str, Any]:
    return _resource_memory_policy(
        memory_types=_RESOURCE_REASON_MEMORY_TYPES,
        target_peer_id=target_peer_id,
    )


def _resource_deletion_memory_policy(target_peer_id: Optional[str] = None) -> Dict[str, Any]:
    # Events are append-only, so deletion commits update only mutable memory types.
    return _resource_memory_policy(
        memory_types=_RESOURCE_DELETION_MEMORY_TYPES,
        target_peer_id=target_peer_id,
    )


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


@dataclass
class _MemoryRefMatch:
    memory_uri: str
    memory_file: MemoryFile
    resource_ref: Dict[str, Any]


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
        session_id = _resource_reason_session_id(resource_uri)
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
            session.meta.memory_policy = _resource_reason_memory_policy(target_peer_id)
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
            add_many_async = getattr(session, "add_messages_async", None)
            if callable(add_many_async):
                await add_many_async([message_spec])
            else:
                session.add_messages([message_spec])
            commit_result = await self._session_service.commit_async(
                session_id,
                ctx,
                keep_recent_count=0,
            )

        task_id = commit_result.get("task_id")
        return {
            "status": "accepted",
            "session_id": session_id,
            "commit_task_id": task_id,
            "archive_uri": commit_result.get("archive_uri"),
            "commit_task": None,
        }

    async def on_resource_deleted(
        self,
        *,
        ctx: RequestContext,
        resource_uri: str,
        memory_uris: Sequence[str],
        recursive: bool = False,
        wait: bool = True,
    ) -> Dict[str, Any]:
        """Commit mutable memory updates; wait=False defers completion to the cleanup consumer."""
        if not resource_uri:
            return {"status": "skipped", "reason": "empty_resource_uri"}
        if not self._session_service:
            return {"status": "skipped", "reason": "session_service_unavailable"}

        deleted_at = datetime.now(timezone.utc).isoformat()
        session_id = _resource_reason_session_id(resource_uri)
        target_peer_id = _resource_reason_peer_id(ctx, resource_uri)
        commit_result: Dict[str, Any] = {}
        task_result: Optional[Dict[str, Any]] = None

        async with self._reason_session_lock:
            session = await self._session_service.get(
                session_id,
                ctx,
                auto_create=True,
            )
            session.meta.memory_policy = _resource_deletion_memory_policy(target_peer_id)
            message_spec: Dict[str, Any] = {
                "role": "user",
                "parts": [
                    TextPart(
                        text=self._build_resource_deletion_message(
                            resource_uri=resource_uri,
                            deleted_at=deleted_at,
                            memory_uris=memory_uris,
                            recursive=recursive,
                        )
                    )
                ],
                "created_at": deleted_at,
            }
            if target_peer_id:
                message_spec["peer_id"] = target_peer_id
            add_many_async = getattr(session, "add_messages_async", None)
            if callable(add_many_async):
                await add_many_async([message_spec])
            else:
                session.add_messages([message_spec])
            commit_result = await self._session_service.commit_async(
                session_id,
                ctx,
                keep_recent_count=0,
            )

        task_id = commit_result.get("task_id")
        pending = bool(task_id) and not wait
        if task_id and wait:
            try:
                task_result = await self._wait_for_commit_task(
                    task_id=str(task_id),
                    ctx=ctx,
                )
            except TimeoutError:
                pending = True
        return {
            "status": "pending" if pending else "success",
            "session_id": session_id,
            "commit_task_id": task_id,
            "archive_uri": commit_result.get("archive_uri"),
            "commit_task": task_result,
            **({"pending_task_ids": [str(task_id)]} if pending else {}),
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

    @staticmethod
    def _build_resource_deletion_message(
        *,
        resource_uri: str,
        deleted_at: str,
        memory_uris: Sequence[str],
        recursive: bool,
    ) -> str:
        affected = "\n".join(f"- {uri}" for uri in dict.fromkeys(memory_uris))
        if not affected:
            affected = "- N/A"
        recursive_text = "true" if recursive else "false"
        return (
            "## Resource Deletion\n"
            f"Resource URI: {resource_uri}\n"
            f"Deleted at: {deleted_at or 'N/A'}\n"
            f"Recursive delete: {recursive_text}\n"
            "Affected memory URIs:\n"
            f"{affected}\n\n"
            "Update existing mutable memories that mention or depend on this resource."
        )

    async def _wait_for_commit_task(
        self,
        *,
        task_id: str,
        ctx: RequestContext,
    ) -> Dict[str, Any]:
        from openviking.service.task_tracker import TaskStatus, get_task_tracker

        task = await get_task_tracker().wait(
            task_id,
            account_id=ctx.account_id,
            user_id=ctx.user.user_id,
            timeout=_RESOURCE_REASON_COMMIT_TIMEOUT_SECONDS,
            poll_interval=0.1,
        )
        if task.status == TaskStatus.COMPLETED:
            return task.to_dict()
        if task.status == TaskStatus.FAILED:
            raise RuntimeError(task.error)
        raise RuntimeError(f"session commit task cancelled: {task_id}")

    async def before_resource_delete(
        self,
        *,
        ctx: RequestContext,
        resource_uri: str,
        recursive: bool = False,
        reason_resource_uris: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        """Wait for reason commits before unlinking; persist cleanup and return pending on timeout.

        A missing recursive snapshot includes all of the user's reason sessions.
        """
        if context_type_for_uri(resource_uri) != "resource":
            return {"status": "skipped", "reason": "not_resource"}

        pending_ids = await self._wait_for_pending_reason_commits(
            ctx=ctx,
            resource_uri=resource_uri,
            recursive=recursive,
            reason_resource_uris=reason_resource_uris,
        )
        if pending_ids:
            await self._defer_cleanup(ctx, resource_uri, recursive, pending_task_ids=pending_ids)
            return {"status": "pending", "pending_task_ids": pending_ids, "memory_uris": []}

        matches = await self._find_referencing_memories(
            ctx=ctx,
            resource_uri=resource_uri,
            recursive=recursive,
        )
        if not matches:
            return {"status": "no_references", "memory_uris": []}

        memory_uris = self._memory_uris_from_matches(matches)
        try:
            commit_result = await self.on_resource_deleted(
                ctx=ctx,
                resource_uri=resource_uri,
                memory_uris=memory_uris,
                recursive=recursive,
            )
        except Exception as exc:
            logger.warning("Resource deletion memory commit failed for %s: %s", resource_uri, exc)
            commit_result = {"status": "failed", "error": str(exc)}
        if commit_result.get("status") == "pending":
            await self._defer_cleanup(
                ctx,
                resource_uri,
                recursive,
                pending_task_ids=commit_result["pending_task_ids"],
                commit_submitted=True,
            )
            return {
                "status": "pending",
                "pending_task_ids": commit_result["pending_task_ids"],
                "memory_uris": [],
                "memory_commit": commit_result,
            }

        result = await self._unlink_resource_memories(
            ctx=ctx, resource_uri=resource_uri, recursive=recursive
        )
        return {
            **result,
            "status": "failed" if commit_result.get("status") == "failed" else "success",
            "memory_commit": commit_result,
        }

    async def _unlink_resource_memories(
        self,
        *,
        ctx: RequestContext,
        resource_uri: str,
        recursive: bool,
    ) -> Dict[str, Any]:
        """Unlink a deleted URI after its commits settle; raise if any memory cannot be cleaned."""
        post_commit_matches = await self._find_referencing_memories(
            ctx=ctx,
            resource_uri=resource_uri,
            recursive=recursive,
        )

        cleaned: List[str] = []
        deleted: List[str] = []
        errors: List[str] = []
        grouped = self._group_matches_by_memory(post_commit_matches)
        for memory_uri, memory_matches in grouped.items():
            first = memory_matches[0]
            try:
                cleanup_result = await self._unlink_memory_reference(
                    ctx=ctx,
                    memory_uri=memory_uri,
                    memory_file=first.memory_file,
                    resource_uri=resource_uri,
                    recursive=recursive,
                )
                cleaned.extend(cleanup_result.written_uris + cleanup_result.edited_uris)
                deleted.extend(cleanup_result.deleted_uris)
                if memory_uri in cleanup_result.deleted_uris:
                    continue
                await self._assert_resource_unlinked(
                    memory_uri,
                    resource_uri,
                    ctx,
                    recursive=recursive,
                )
            except NotFoundError:
                deleted.append(memory_uri)
            except Exception as exc:
                errors.append(f"{memory_uri}: {exc}")

        if errors:
            raise RuntimeError(
                "resource memory cleanup failed before deleting resource: " + "; ".join(errors)
            )
        return {
            "memory_uris": list(dict.fromkeys(cleaned)),
            "deleted_memory_uris": list(dict.fromkeys(deleted)),
        }

    async def _defer_cleanup(
        self, ctx: RequestContext, resource_uri: str, recursive: bool, **state: Any
    ) -> None:
        """Persist cleanup ownership and its current phase outside the deleting Task's lifecycle."""
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
                    **state,
                },
            )

    async def resume_cleanup(self, payload: Dict[str, Any]) -> ProcessResult:
        """Resume durable cleanup without waiting for commits in a SessionCommit worker.

        Unsettled dependencies and cleanup errors requeue the current phase.
        commit_submitted marks submission; memory_uris checkpoints the final scan
        so index refresh work survives an interrupted unlink.
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
        state = {
            key: payload[key]
            for key in ("pending_task_ids", "commit_submitted", "memory_uris")
            if key in payload
        }
        for task_id in payload["pending_task_ids"]:
            task = await get_task_tracker().get(
                task_id, account_id=ctx.account_id, user_id=ctx.user.user_id
            )
            if task is None:
                raise KeyError(f"Reason commit task not found: {task_id}")
            if task.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
                await self._defer_cleanup(**args, **state)
                return ProcessResult.requeued()

        if "memory_uris" not in payload:
            matches = await self._find_referencing_memories(**args)
            if not payload.get("commit_submitted"):
                if not matches:
                    return ProcessResult.success()
                # shortcut: QueueFS can replay submission; add an idempotency key if duplicate extraction is costly.
                commit_result = await self.on_resource_deleted(
                    **args, memory_uris=self._memory_uris_from_matches(matches), wait=False
                )
                state.update(
                    pending_task_ids=commit_result.get("pending_task_ids", []),
                    commit_submitted=True,
                )
            else:
                state["memory_uris"] = self._memory_uris_from_matches(matches)
            await self._defer_cleanup(**args, **state)
            return ProcessResult.requeued()

        try:
            await self._unlink_resource_memories(**args)
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
                        viking_fs=viking_fs, directory_uri=directory_uri, ctx=ctx, strict=True
                    )
        except Exception as exc:
            logger.warning(
                "Deferred resource memory cleanup failed for %s: %s", payload["resource_uri"], exc
            )
            await self._defer_cleanup(**args, **state)
            return ProcessResult.requeued()
        return ProcessResult.success()

    async def _wait_for_pending_reason_commits(
        self,
        *,
        ctx: RequestContext,
        resource_uri: str,
        recursive: bool,
        reason_resource_uris: Optional[Sequence[str]],
    ) -> List[str]:
        """Wait without changing tasks; return IDs not observed terminal before timeout.

        All legacy commits participate because their task records have no resource URI.
        """
        from openviking.service.task_tracker import TaskStatus, get_task_tracker

        session_ids = {
            _RESOURCE_REASON_SESSION_ID,
            _resource_reason_session_id(resource_uri),
            *(_resource_reason_session_id(uri) for uri in reason_resource_uris or ()),
        }
        include_all_reasons = recursive and reason_resource_uris is None
        tracker = get_task_tracker()
        deadline = time.monotonic() + _RESOURCE_REASON_COMMIT_TIMEOUT_SECONDS
        tasks = await tracker.list_tasks(
            task_type="session_commit",
            account_id=ctx.account_id,
            user_id=ctx.user.user_id,
            limit=None,
        )
        pending = [
            task
            for task in tasks
            if task.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED)
            and (
                task.resource_id in session_ids
                or (
                    include_all_reasons
                    and (task.resource_id or "").startswith(_RESOURCE_REASON_SESSION_PREFIX)
                )
            )
        ]
        for index, task in enumerate(pending):
            try:
                await tracker.wait(
                    task.task_id,
                    account_id=ctx.account_id,
                    user_id=ctx.user.user_id,
                    timeout=max(0.0, deadline - time.monotonic()),
                    poll_interval=0.1,
                )
            except TimeoutError:
                return [unsettled.task_id for unsettled in pending[index:]]
        return []

    async def _unlink_memory_reference(
        self,
        *,
        ctx: RequestContext,
        memory_uri: str,
        memory_file: MemoryFile,
        resource_uri: str,
        recursive: bool = False,
    ) -> MemoryUpdateResult:
        viking_fs = self._get_viking_fs()
        current = memory_file
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

        await viking_fs.write_file(memory_uri, MemoryFileUtils.write(current), ctx=ctx)
        result.add_edited(memory_uri)
        return result

    async def _find_referencing_memories(
        self,
        *,
        ctx: RequestContext,
        resource_uri: str,
        recursive: bool,
    ) -> List[_MemoryRefMatch]:
        candidate_uris = await self._grep_candidate_memory_uris(
            ctx=ctx,
            resource_uri=resource_uri,
        )
        if candidate_uris is None:
            logger.warning(
                "Skipping resource memory reference scan for %s because grep is unavailable",
                resource_uri,
            )
            return []
        return await self._read_referencing_memory_matches(
            candidate_uris,
            ctx=ctx,
            resource_uri=resource_uri,
            recursive=recursive,
        )

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
            except Exception as exc:
                logger.debug(
                    "Resource memory grep failed for %s, skipping reference scan: %s",
                    memory_root,
                    exc,
                )
                return None

            for match in result.get("matches", []):
                uri = match.get("uri", "")
                if self._is_memory_markdown_file(uri):
                    candidate_uris.append(uri)

        return list(dict.fromkeys(candidate_uris))

    async def _read_referencing_memory_matches(
        self,
        candidate_uris: Sequence[str],
        *,
        ctx: RequestContext,
        resource_uri: str,
        recursive: bool,
    ) -> List[_MemoryRefMatch]:
        viking_fs = self._get_viking_fs()
        matches: List[_MemoryRefMatch] = []
        search_needle = self._resource_uri_search_needle(resource_uri)
        for uri in candidate_uris:
            try:
                raw = await viking_fs.read_file(uri, ctx=ctx)
                raw_text = raw if isinstance(raw, str) else str(raw or "")
            except Exception:
                continue
            if search_needle and search_needle not in raw_text:
                continue

            try:
                mf = MemoryFileUtils.read(raw_text, uri=uri)
            except Exception:
                continue

            matched = False
            for ref in self._coerce_resource_refs(mf.extra_fields.get("resource_refs")):
                if self._resource_ref_matches(ref.get("resource_uri"), resource_uri, recursive):
                    matches.append(_MemoryRefMatch(uri, mf, ref))
                    matched = True
            if matched:
                continue

            if content_references_resource(
                mf.content,
                resource_uri,
                recursive=recursive,
            ):
                matched_uri = next(
                    (
                        item
                        for item in extract_resource_uris(mf.content)
                        if self._resource_ref_matches(item, resource_uri, recursive)
                    ),
                    resource_uri,
                )
                matches.append(
                    _MemoryRefMatch(
                        uri,
                        mf,
                        {
                            "resource_uri": matched_uri,
                            "source": "visible_content",
                        },
                    )
                )
        return matches

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
        try:
            raw = await self._get_viking_fs().read_file(memory_uri, ctx=ctx)
        except (NotFoundError, FileNotFoundError) as exc:
            raise NotFoundError(memory_uri, "memory") from exc
        mf = MemoryFileUtils.read(raw, uri=memory_uri)
        if content_references_resource(mf.content, resource_uri, recursive=recursive):
            raise RuntimeError(f"memory content still contains deleted resource URI: {memory_uri}")
        for ref in self._coerce_resource_refs(mf.extra_fields.get("resource_refs")):
            if self._resource_ref_matches(
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

    @staticmethod
    def _group_matches_by_memory(
        matches: Sequence[_MemoryRefMatch],
    ) -> Dict[str, List[_MemoryRefMatch]]:
        grouped: Dict[str, List[_MemoryRefMatch]] = {}
        for match in matches:
            grouped.setdefault(match.memory_uri, []).append(match)
        return grouped

    @staticmethod
    def _memory_uris_from_matches(matches: Sequence[_MemoryRefMatch]) -> List[str]:
        return list(dict.fromkeys(match.memory_uri for match in matches))

    @staticmethod
    def _resource_ref_matches(
        ref_uri: Any,
        target_uri: str,
        recursive: bool,
    ) -> bool:
        return resource_ref_matches(ref_uri, target_uri, recursive=recursive)
