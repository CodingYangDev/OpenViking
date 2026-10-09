# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Shared model-facing policy for safe memory reorganization."""

MEMORY_MERGE_POLICY = """
## Memory Write Decision
- Per target, write only for schema-admissible additions, corrections,
  explicit revocations, or schema-required cleanup; otherwise no-op.
  Mentions or action requests alone do not justify writes.

## Memory Reorganization Safety
- Merge only the same schema-defined identity: the same entity/event,
  or the same owner and behavioral choice dimension.
  Similarity or shared topics are insufficient.
- Before deleting or replacing, preserve each still-valid distinct fact
  exactly once in a surviving schema-valid destination.
  If identity, validity, or destination is uncertain, keep the source.
- Split only into distinct schema-valid identities; never invent identities
  to meet length targets. Keep one identity in one object,
  using sections when needed.
- Compact duplicate wording only; never drop, weaken, or generalize
  valid facts to shorten content.
""".strip()
