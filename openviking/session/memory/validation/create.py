# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Create-only intent and collision checks shared by extraction and persistence."""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from openviking.session.memory.dataclass import ResolvedOperations
from openviking_cli.exceptions import ConflictError


class MemoryCreateConflictError(ConflictError):
    """A rejected SDK create, not a successful empty extraction."""

    def __init__(self, conflicts: list[dict[str, str]]) -> None:
        self.conflicts = conflicts
        super().__init__(
            "Memory create failed: " + json.dumps(conflicts, ensure_ascii=False) + ". "
            "Conflicting creates were not applied; existing targets are unchanged. For already_exists, read the "
            "existing memory through eval with an explicit assignment, then edit/update the "
            "returned object while preserving unrelated content; do not call create again. "
            "For duplicate_target, combine changes into one operation for that target. "
            "Re-emit the complete corrected program, including unaffected operations.",
            resource=conflicts[0]["uri"] if conflicts else None,
        )


def memory_create_target_key(uri: str, viking_fs: Any, ctx: Any) -> str:
    """Use the same storage identity for collision checks and repair-progress tracking."""
    return viking_fs._uri_to_path(uri, ctx=ctx).rstrip("/").casefold()


async def validate_create_operations(
    operations: ResolvedOperations, viking_fs: Any, ctx: Any
) -> None:
    """Reject occupied or multiply-targeted creates before any candidate writes.

    Only the persistence caller's pathlock makes this check race-free. Extraction
    uses the same check optimistically to give the model actionable feedback.
    Namespace/read failures propagate; they must not be treated as nonexistence.
    """
    creates = [op for op in operations.upsert_operations if op.create_only and op.uris]
    if not creates:
        return
    if viking_fs is None:
        raise RuntimeError("VikingFS is required to validate memory creation")

    counts = Counter(
        memory_create_target_key(uri, viking_fs, ctx)
        for op in operations.upsert_operations
        for uri in op.uris
    )
    counts.update(
        memory_create_target_key(file.uri, viking_fs, ctx)
        for file in operations.delete_file_contents
        if file.uri
    )
    conflicts = []
    checked = set()
    for op in creates:
        for uri in op.uris:
            identity = memory_create_target_key(uri, viking_fs, ctx)
            if identity in checked:
                continue
            checked.add(identity)
            if counts[identity] > 1:
                conflicts.append({"uri": uri, "code": "duplicate_target"})
            if await viking_fs.exists(uri, ctx=ctx):
                conflicts.append({"uri": uri, "code": "already_exists"})
    if conflicts:
        raise MemoryCreateConflictError(conflicts)
