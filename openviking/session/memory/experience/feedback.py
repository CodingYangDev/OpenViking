# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Trajectory-level effect labels and system-owned Experience feedback counters."""

from __future__ import annotations

import json
from typing import Any

from openviking.session.archive_store import is_storage_not_found
from openviking.session.memory.dataclass import MemoryFile, ResolvedOperation, ResolvedOperations
from openviking.session.memory.utils.memory_file_utils import MemoryFileUtils

_EFFECT_FIELDS = ("positive_ids", "negative_ids", "weak_ids")
_EFFECT_PRIORITY = {"neutral": 0, "weak": 1, "positive": 2, "negative": 3}


def injected_experiences(execution: dict[str, Any]) -> list[dict[str, Any]]:
    """Assign aliases only to advice recorded as inserted, never to proposed DAG actions."""
    reminders: list[dict[str, Any]] = []
    for uri, snapshot in sorted(execution.items()):
        instructions = list(
            dict.fromkeys(
                item["instruction"]
                for item in (snapshot.get("delivered_advice") or [])
                if isinstance(item, dict)
                and isinstance(item.get("instruction"), str)
                and item["instruction"].strip()
            )
        )
        if instructions:
            reminders.append(
                {
                    "id": f"E{len(reminders) + 1}",
                    "experience_uri": uri,
                    "instructions": instructions,
                }
            )
    return reminders


def validate_experience_effects(value: Any, sources: dict[str, str]) -> dict[str, list[str]]:
    """Validate model-written categories against the server-owned delivery bindings."""
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict) or set(value) != set(_EFFECT_FIELDS):
        raise ValueError("experience_effects requires positive_ids, negative_ids and weak_ids")
    seen: set[str] = set()
    result = {}
    for name in _EFFECT_FIELDS:
        ids = value[name]
        if not isinstance(ids, list) or any(not isinstance(alias, str) for alias in ids):
            raise ValueError(f"experience_effects.{name} must be a list of injected IDs")
        if any(alias not in sources for alias in ids):
            raise ValueError(f"experience_effects.{name} contains an ID that was not injected")
        if len(set(ids)) != len(ids) or seen.intersection(ids):
            raise ValueError("An injected ID may appear in at most one experience_effects category")
        seen.update(ids)
        result[name] = sorted(ids)
    return result


def _feedback_stats(value: Any) -> dict[str, Any]:
    if value is None:
        return {
            "schema_version": 1,
            "injected_count": 0,
            **{f"{effect}_count": 0 for effect in _EFFECT_PRIORITY},
            "observed_trajectories": [],
        }
    if isinstance(value, str):
        value = json.loads(value)
    counters = ("injected_count", *(f"{effect}_count" for effect in _EFFECT_PRIORITY))
    if (
        not isinstance(value, dict)
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
        or any(type(value.get(key)) is not int or value[key] < 0 for key in counters)
    ):
        raise ValueError("Invalid system-owned feedback_stats counters")
    observed = value.get("observed_trajectories")
    if (
        not isinstance(observed, list)
        or any(not isinstance(uri, str) or not uri for uri in observed)
        or len(set(observed)) != len(observed)
        or len(observed) != value["injected_count"]
        or sum(value[f"{effect}_count"] for effect in _EFFECT_PRIORITY) != value["injected_count"]
    ):
        raise ValueError("feedback_stats counters and observed_trajectories disagree")
    return {**value, "observed_trajectories": list(observed)}


async def record_experience_feedback_stats(
    trajectory: MemoryFile,
    *,
    registry: Any,
    viking_fs: Any,
    ctx: Any,
) -> dict[str, list[str]]:
    """Count each persisted Trajectory once per Experience through the normal locked writer.

    Counters and their deduplication markers are written together in one Experience file.
    Cached Trajectories can retry this step without rerunning extraction or double counting.
    Records without delivery bindings are not retrospectively attributed to an Experience.
    """
    # Local imports avoid a provider -> feedback -> updater -> provider import cycle.
    from openviking.session.memory.memory_updater import MemoryUpdater
    from openviking.session.memory.streaming_memory_updater import _acquire_stable_operation_lease

    result: dict[str, list[str]] = {"updated_uris": [], "skipped_uris": []}
    sources = trajectory.extra_fields.get("experience_effect_sources", {})
    if not isinstance(sources, dict):
        raise ValueError("Invalid persisted Experience feedback sources")
    if not sources:
        return result
    if (
        not isinstance(sources, dict)
        or any(
            not isinstance(alias, str) or not isinstance(uri, str) for alias, uri in sources.items()
        )
        or any(MemoryUpdater.memory_type_from_uri(uri) != "experiences" for uri in sources.values())
        or not trajectory.uri
        or MemoryUpdater.memory_type_from_uri(trajectory.uri) != "trajectories"
    ):
        raise ValueError("Invalid persisted Experience feedback sources")
    effects = validate_experience_effects(
        trajectory.extra_fields.get("experience_effects"), sources
    )
    by_uri: dict[str, str] = {}
    for alias, uri in sources.items():
        effect = next(
            (name.removesuffix("_ids") for name in _EFFECT_FIELDS if alias in effects[name]),
            "neutral",
        )
        if _EFFECT_PRIORITY[effect] >= _EFFECT_PRIORITY[by_uri.get(uri, "neutral")]:
            by_uri[uri] = effect
    for uri, effect in sorted(by_uri.items()):
        operation = ResolvedOperation(memory_type="experiences", uris=[uri], memory_fields={})
        operations = ResolvedOperations(
            upsert_operations=[operation], delete_file_contents=[], errors=[]
        )
        lease = await _acquire_stable_operation_lease(operations, viking_fs, ctx)
        if lease is None:
            raise RuntimeError("Experience feedback requires a write-set pathlock")
        try:
            try:
                raw = await viking_fs.read_file(uri, ctx=ctx)
            except Exception as exc:
                if not is_storage_not_found(exc):
                    raise
                # A removed Experience must not be recreated by a statistics update.
                result["skipped_uris"].append(uri)
                continue
            previous = MemoryFileUtils.read(raw, uri=uri)
            stats = _feedback_stats(previous.extra_fields.get("feedback_stats"))
            if trajectory.uri in stats["observed_trajectories"]:
                result["skipped_uris"].append(uri)
                continue
            stats["injected_count"] += 1
            stats[f"{effect}_count"] += 1
            stats["observed_trajectories"].append(trajectory.uri)
            operation.memory_fields = {"feedback_stats": stats}
            operation.old_memory_file_content = previous
            # Feedback changes hidden metadata only; do not re-embed unchanged DAG content.
            updater = MemoryUpdater(registry=registry, transaction_handle=lease)
            updater._viking_fs = viking_fs
            updated = await updater.apply_operations(operations, ctx)
            if updated.errors:
                raise RuntimeError(f"Experience feedback update failed: {updated.errors}")
            result["updated_uris"].append(uri)
        finally:
            await viking_fs._async_agfs.pathlock_release(lease)
    return result
