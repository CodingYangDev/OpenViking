# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Live extraction test for SS project/plot/clip/user memories.

This test deliberately separates:

* ``expected``: the human-reviewed candidate ground truth built from the source session.
* ``actual``: files produced by the real SessionCommit -> ExtractLoop -> updater pipeline.

The test is opt-in because it uses the configured live VLM and embedding services.
SS uses an extraction iteration budget of 6 by default; override it with
``SS_MEMORY_EXTRACT_MAX_ITERATIONS``. The output protocol is inherited from ov.conf
unless ``SS_MEMORY_OUTPUT_FORMAT`` is explicitly set. Batches default to 50 messages
and 55,000 estimated message tokens; override them with ``SS_MEMORY_BATCH_MESSAGE_LIMIT``
and ``SS_MEMORY_BATCH_TOKEN_LIMIT``.

Run from the repository root::

    RUN_SS_MEMORY_EXTRACTION_TEST=1 \
    SS_MEMORY_SESSION_FILE=result/ss_memory_ground_truth/sesn-.../session.json \
    SS_MEMORY_EXPECTED_MANIFEST=result/ss_memory_ground_truth/sesn-.../read_manifest.json \
    SS_MEMORY_ACTUAL_DIR=result/ss_memory_ground_truth/sesn-.../actual \
    SS_MEMORY_TYPES=ss_plot \
      .venv/bin/pytest tests/integration/test_ss_four_level_memory_extraction.py -v -s -m integration
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
import yaml

from openviking.message import TextPart
from openviking.server.config import load_server_config
from openviking.server.identity import RequestContext, Role
from openviking.service.core import OpenVikingService
from openviking.session.memory.utils import MemoryFileUtils
from openviking.session.memory_policy import MemoryPolicy
from openviking.telemetry import tracer
from openviking.telemetry.tracer import init_tracer_from_server_config
from openviking_cli.session.user_id import UserIdentifier
from openviking_cli.utils.config.config_loader import load_json_config, resolve_config_path
from openviking_cli.utils.config.consts import (
    DEFAULT_OV_CONF,
    OPENVIKING_CONFIG_ENV,
)
from openviking_cli.utils.config.open_viking_config import OpenVikingConfigSingleton

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_SCHEMAS = Path(__file__).parent / "fixtures" / "ss_memory_schemas"
DEFAULT_SAMPLE_ROOT = (
    PROJECT_ROOT / "result" / "ss_memory_ground_truth" / "sesn-20260918042348-c8t57"
)
DEFAULT_SOURCE = DEFAULT_SAMPLE_ROOT / "session.json"
DEFAULT_EXPECTED = DEFAULT_SAMPLE_ROOT / "read_manifest.json"
MEMORY_TYPES = {"ss_user", "ss_project", "ss_plot", "ss_clip"}
RESOURCE_ID_RE = re.compile(r"\bra_[A-Za-z0-9_]+\b")
TOOL_EVIDENCE_MAX_CHARS = 1_000
USER_TURN_CONTEXT_MAX_CHARS = 2_000
USER_TURN_CONTEXT_BEGIN = "[SS_TURN_CONTEXT BEGIN]"
USER_TURN_CONTEXT_END = "[SS_TURN_CONTEXT END]"
USER_TEXT_BEGIN = "[USER_TEXT]"
_SS_COMMAND_RE = re.compile(r"^\s*ss-cli\s+(?P<resource>[A-Za-z0-9_-]+)\s+(?P<verb>[A-Za-z0-9_-]+)")
_SS_FLAG_RE = re.compile(
    r"(?:^|\s)-{1,2}(?P<key>"
    r"project-id|plot-id|clip-id|take-id|resource-asset-id|resource-item-id|"
    r"resource-id|generation-task-id|status"
    r")\s+(?P<value>\"[^\"]*\"|'[^']*'|\S+)"
)
_TOOL_FIELD_ALIASES = {
    "project_id": "project_id",
    "projectId": "project_id",
    "project-id": "project_id",
    "plot_id": "plot_id",
    "plotId": "plot_id",
    "plot-id": "plot_id",
    "clip_id": "clip_id",
    "clipId": "clip_id",
    "clip-id": "clip_id",
    "take_id": "take_id",
    "takeId": "take_id",
    "take-id": "take_id",
    "selected_take": "selected_take_id",
    "resource_asset_id": "resource_id",
    "resourceAssetId": "resource_id",
    "resource-asset-id": "resource_id",
    "output_resource_asset_id": "output_resource_id",
    "creative_asset_id": "creative_asset_id",
    "creativeAssetId": "creative_asset_id",
    "resource-id": "creative_asset_id",
    "resource_item_id": "resource_item_id",
    "resourceItemId": "resource_item_id",
    "resource-item-id": "resource_item_id",
    "generation_id": "generation_id",
    "generationTaskId": "generation_task_id",
    "generation-task-id": "generation_task_id",
    "source_snapshot_id": "snapshot_id",
    "snapshot_id": "snapshot_id",
    "source_job_id": "job_id",
    "task_id": "task_id",
    "version_id": "version_id",
    "current_version_id": "version_id",
    "clip_revision_id": "clip_revision_id",
    "plot_revision_id": "plot_revision_id",
    "profile_revision_id": "profile_revision_id",
    "status": "status",
    "job_status": "job_status",
    "processing_status": "processing_status",
    "generation_state": "generation_state",
    "lifecycle": "lifecycle",
}
_TOOL_IDENTITY_FIELDS = {
    "project_id",
    "plot_id",
    "clip_id",
    "take_id",
    "selected_take_id",
    "resource_id",
    "output_resource_id",
    "creative_asset_id",
    "resource_item_id",
    "generation_id",
    "generation_task_id",
    "snapshot_id",
    "job_id",
    "task_id",
    "version_id",
    "clip_revision_id",
    "plot_revision_id",
    "profile_revision_id",
}
_TOOL_STATUS_FIELDS = {
    "status",
    "job_status",
    "processing_status",
    "generation_state",
    "lifecycle",
}
_SKIPPED_TOOL_FAMILIES = {
    "project.preference",
    "resource.kind",
    "ss_cli.document",
    "ss_cli.edit_lock",
}
_MUTATING_TOOL_ACTIONS = {
    "add",
    "create",
    "delete",
    "generate",
    "patch",
    "remove",
    "replace",
    "select",
    "submit_image",
    "submit_video",
    "update",
}
logger = logging.getLogger(__name__)


def _flush_tracer_provider() -> None:
    try:
        from opentelemetry import trace as otel_trace

        provider = otel_trace.get_tracer_provider()
        if hasattr(provider, "force_flush"):
            provider.force_flush()
    except Exception as exc:
        logger.warning("Failed to flush test tracer provider: %s", exc)


def _configured_path(env_name: str, default: Path) -> Path:
    return Path(os.environ.get(env_name, str(default))).expanduser().resolve()


def _configured_memory_types() -> set[str]:
    raw = os.environ.get("SS_MEMORY_TYPES", "")
    if not raw.strip():
        return set(MEMORY_TYPES)
    configured = {item.strip() for item in raw.split(",") if item.strip()}
    unknown = configured - MEMORY_TYPES
    if unknown:
        raise ValueError(f"Unknown SS_MEMORY_TYPES: {sorted(unknown)}")
    if not configured:
        raise ValueError("SS_MEMORY_TYPES must enable at least one memory type")
    return configured


def _configured_extract_max_iterations() -> int:
    iterations = int(os.environ.get("SS_MEMORY_EXTRACT_MAX_ITERATIONS", "6"))
    return MemoryPolicy.from_dict({"extract_max_iterations": iterations}).extract_max_iterations


def _configured_extract_thinking() -> bool:
    thinking = json.loads(os.environ.get("SS_MEMORY_EXTRACT_THINKING", "false"))
    return MemoryPolicy.from_dict({"extract_thinking": thinking}).extract_thinking


def _configured_focus_types() -> set[str]:
    raw = os.environ.get("SS_MEMORY_FOCUS_TYPES", "project,plot,clip")
    configured = {item.strip() for item in raw.split(",") if item.strip()}
    if not configured:
        raise ValueError("SS_MEMORY_FOCUS_TYPES must enable at least one focus type")
    return configured


def _load_source(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    turns = payload.get("data", {}).get("turns")
    if not isinstance(turns, list) or not turns:
        raise ValueError(f"SS session file has no data.turns: {path}")
    return payload


def _walk(value: Any):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _reference_index(payload: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    """Resolve resource IDs to stable Plot/Clip IDs from recorded tool results."""
    clip_parents: dict[str, str] = {}
    index: dict[str, list[dict[str, str]]] = {}

    for item in _walk(payload):
        if not isinstance(item, dict):
            continue
        clip_id = str(item.get("clip_id") or "")
        plot_id = str(item.get("plot_id") or "")
        if clip_id and plot_id:
            clip_parents[clip_id] = plot_id

    def record(resource_id: str, context: dict[str, Any]) -> None:
        clip_id = str(context.get("clip_id") or "")
        plot_id = str(context.get("plot_id") or clip_parents.get(clip_id) or "")
        normalized = {
            key: value
            for key, value in {
                "resource_id": resource_id,
                "plot_id": plot_id,
                "clip_id": clip_id,
                "take_id": str(context.get("take_id") or ""),
                "asset_id": str(context.get("creative_asset_id") or ""),
            }.items()
            if value
        }
        if len(normalized) <= 1:
            return
        bucket = index.setdefault(resource_id, [])
        if normalized not in bucket:
            bucket.append(normalized)

    for item in _walk(payload):
        if not isinstance(item, dict):
            continue
        resource_id = str(item.get("output_resource_asset_id") or "")
        if resource_id:
            record(resource_id, item)
        references = item.get("reference_resources")
        if isinstance(references, dict):
            for ref_id, ref_data in references.items():
                if isinstance(ref_data, dict) and isinstance(ref_data.get("owner_context"), dict):
                    record(str(ref_id), ref_data["owner_context"])
    return index


def _iter_recorded_tool_calls(turn: dict[str, Any]):
    """Yield each top-level or subagent tool call once, in recorded order."""
    seen: set[str] = set()

    def visit(value: Any):
        if isinstance(value, dict):
            call_id = value.get("callId")
            is_tool_call = (
                "tool" in value or bool(value.get("name")) or bool(_command_from_call(value))
            )
            if (
                call_id
                and is_tool_call
                and ("input" in value or "result" in value or "tool" in value)
            ):
                normalized_id = str(call_id)
                if normalized_id not in seen:
                    seen.add(normalized_id)
                    yield value
                return
            for child in value.values():
                yield from visit(child)
        elif isinstance(value, list):
            for child in value:
                yield from visit(child)

    yield from visit(turn.get("toolCalls") or [])
    yield from visit(turn.get("subagents") or [])


def _parse_json_result(text: Any) -> dict[str, Any] | None:
    """Parse the structured JSON portion of a recorded textual tool result."""
    if not isinstance(text, str) or not text:
        return None
    stdout = text.split("--- stdout ---", 1)[-1]
    stdout = stdout.split("--- stderr ---", 1)[0]
    start = stdout.find("{")
    if start < 0:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(stdout[start:].lstrip())
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _command_from_call(call: dict[str, Any]) -> str:
    raw_input = call.get("input")
    if isinstance(raw_input, dict):
        return str(raw_input.get("command") or "")
    if not isinstance(raw_input, str):
        return ""
    try:
        parsed = json.loads(raw_input)
    except json.JSONDecodeError:
        return raw_input
    return str(parsed.get("command") or "") if isinstance(parsed, dict) else ""


def _command_metadata(command: str) -> tuple[str, str, dict[str, str]]:
    match = _SS_COMMAND_RE.match(command)
    if not match:
        return "", "", {}
    resource = match.group("resource").replace("-", "_")
    verb = match.group("verb").replace("-", "_")
    family = f"{resource}.{verb}"
    flags = {
        item.group("key"): item.group("value").strip("\"'")
        for item in _SS_FLAG_RE.finditer(command)
    }
    return family, verb, flags


def _safe_status(value: Any) -> str | None:
    if not isinstance(value, (str, int, float, bool)):
        return None
    normalized = str(value)
    if len(normalized) > 40 or not re.fullmatch(r"[A-Za-z0-9_.:-]+", normalized):
        return None
    return normalized


def _compact_tool_record(
    source: Any,
    *,
    list_position: int | None = None,
) -> dict[str, Any]:
    if not isinstance(source, dict):
        return {}

    compact: dict[str, Any] = {}
    for source_key, target_key in _TOOL_FIELD_ALIASES.items():
        if source_key not in source or target_key in compact:
            continue
        value = source[source_key]
        if target_key in _TOOL_STATUS_FIELDS:
            value = _safe_status(value)
        elif not isinstance(value, str) or not value or len(value) > 128:
            value = None
        if value is not None and value != "":
            compact[target_key] = value

    has_identity = bool(_TOOL_IDENTITY_FIELDS & compact.keys())
    if not has_identity:
        return {}
    if list_position is not None and {"plot_id", "clip_id"} & compact.keys():
        compact["list_position"] = list_position
    return compact


def _walk_tool_records(value: Any, *, list_position: int | None = None):
    if isinstance(value, dict):
        compact = _compact_tool_record(value, list_position=list_position)
        if compact:
            yield compact
        for child in value.values():
            yield from _walk_tool_records(child, list_position=list_position)
    elif isinstance(value, list):
        for index, child in enumerate(value, start=1):
            yield from _walk_tool_records(child, list_position=index)


def _tool_result_payloads(call: dict[str, Any]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    tool = call.get("tool")
    if isinstance(tool, dict):
        output = tool.get("output")
        if isinstance(output, dict) and isinstance(output.get("raw_stdout_json"), dict):
            payloads.append(output["raw_stdout_json"])
        result = tool.get("result")
        if isinstance(result, dict):
            stdout_json = result.get("stdout_json")
            if isinstance(stdout_json, dict):
                payloads.append(stdout_json)
    parsed = _parse_json_result(call.get("result"))
    if parsed is not None:
        payloads.append(parsed)
    return payloads


def _tool_family_action_target(
    call: dict[str, Any],
) -> tuple[str, str, dict[str, Any], str]:
    tool = call.get("tool")
    if isinstance(tool, dict):
        family = str(tool.get("family") or "")
        action = str(tool.get("action") or "")
        target = _compact_tool_record(tool.get("target"))
        flags = tool.get("parameters", {}).get("flags")
        flag_record = _compact_tool_record(flags)
        target = {**flag_record, **target}
        return family, action, target, str(tool.get("command") or "")

    command = _command_from_call(call)
    family, action, flags = _command_metadata(command)
    return family, action, _compact_tool_record(flags), command


def _tool_family_is_relevant(family: str) -> bool:
    if not family:
        return False
    if any(
        family == prefix or family.startswith(prefix + ".") for prefix in _SKIPPED_TOOL_FAMILIES
    ):
        return False
    return family.split(".", 1)[0] in {
        "project",
        "plot",
        "clip",
        "asset",
        "resource",
        "generation",
        "ss_cli",
    }


def _tool_result_record_limit(family: str) -> int:
    if family in {"plot.list", "clip.list", "clip.takes"}:
        return 32
    if family in {"asset.list", "resource.list", "asset.item.list"}:
        return 4
    return 16


def _compact_tool_call(call: dict[str, Any], source_index: int) -> dict[str, Any] | None:
    family, action, target, _ = _tool_family_action_target(call)
    if not _tool_family_is_relevant(family):
        return None

    records: list[dict[str, Any]] = []
    seen_records: set[str] = set()
    record_limit = _tool_result_record_limit(family)
    for payload in _tool_result_payloads(call):
        for record in _walk_tool_records(payload):
            key = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if key in seen_records:
                continue
            seen_records.add(key)
            records.append(record)
            if len(records) >= record_limit:
                break
        if len(records) >= record_limit:
            break

    identities = [target, *records]
    specific_fields = _TOOL_IDENTITY_FIELDS - {"project_id"}
    if not any(specific_fields & item.keys() for item in identities if item):
        return None

    execution: dict[str, Any] = {}
    phase = _safe_status(call.get("phase"))
    if phase:
        execution["phase"] = phase
    tool = call.get("tool")
    output = tool.get("output") if isinstance(tool, dict) else None
    if isinstance(output, dict):
        if isinstance(output.get("exit_code"), int):
            execution["exit_code"] = output["exit_code"]
        if isinstance(output.get("ok"), bool):
            execution["ok"] = output["ok"]
    for payload in _tool_result_payloads(call):
        if isinstance(payload.get("ok"), bool):
            execution["ok"] = payload["ok"]
            break
    if call.get("errorMessage"):
        execution["ok"] = False

    event: dict[str, Any] = {
        "family": family,
        "action": action,
        "execution": execution,
    }
    if target:
        event["target"] = target
    if records:
        event["objects"] = records
    event["_source_index"] = source_index
    return event


def _tool_event_priority(event: dict[str, Any]) -> tuple[int, int]:
    family = str(event.get("family") or "")
    target = event.get("target") or {}
    records = event.get("objects") or []
    combined = [target, *records]
    has_clip = any("clip_id" in item for item in combined)
    has_plot = any("plot_id" in item for item in combined)
    mutating = str(event.get("action") or "") in _MUTATING_TOOL_ACTIONS
    if has_clip and mutating:
        priority = 0
    elif has_clip:
        priority = 1
    elif has_plot and (mutating or family == "plot.list"):
        priority = 2
    elif has_plot:
        priority = 3
    else:
        priority = 4
    return priority, int(event["_source_index"])


def _compact_tool_evidence(
    turn: dict[str, Any],
    *,
    max_chars: int = TOOL_EVIDENCE_MAX_CHARS,
) -> dict[str, Any]:
    """Project SS tool traces to bounded identity/status evidence.

    The projection deliberately drops prompts, command prose, result text,
    logs, media metadata, and model-authored descriptions. It is routing
    evidence only; schemas still require memory content to originate in the
    explicit user message.
    """
    calls = list(_iter_recorded_tool_calls(turn))
    events = [
        event
        for index, call in enumerate(calls)
        if (event := _compact_tool_call(call, index)) is not None
    ]
    events.sort(key=_tool_event_priority)

    def document(selected: list[dict[str, Any]], *, object_truncated: bool = False):
        cleaned = [
            {key: value for key, value in event.items() if key != "_source_index"}
            for event in selected
        ]
        return {
            "source_call_count": len(calls),
            "relevant_call_count": len(events),
            "included_call_count": len(cleaned),
            "truncated": len(cleaned) < len(events) or object_truncated,
            "calls": cleaned,
        }

    selected: list[dict[str, Any]] = []
    object_truncated = False
    for event in events:
        candidate = document([*selected, event], object_truncated=object_truncated)
        if len(json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))) <= max_chars:
            selected.append(event)
            continue

        objects = event.get("objects") or []
        if not objects:
            continue
        shortened = {**event, "objects": []}
        fitted_objects: list[dict[str, Any]] = []
        for record in objects:
            shortened["objects"] = [*fitted_objects, record]
            candidate = document([*selected, shortened], object_truncated=True)
            if len(json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))) > max_chars:
                shortened["objects"] = fitted_objects
                break
            fitted_objects.append(record)
        if fitted_objects:
            selected.append(shortened)
            object_truncated = object_truncated or len(fitted_objects) < len(objects)

    compact = document(selected, object_truncated=object_truncated)
    encoded = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) > max_chars:
        raise AssertionError(f"tool evidence exceeded {max_chars} characters: {len(encoded)}")
    return compact


def _clip_parent_index(payload: dict[str, Any]) -> dict[str, str]:
    parents: dict[str, str] = {}
    for item in _walk(payload):
        if not isinstance(item, dict):
            continue
        clip_id = str(item.get("clip_id") or "")
        plot_id = str(item.get("plot_id") or "")
        if clip_id and plot_id:
            parents[clip_id] = plot_id
    return parents


def _plot_identity_index(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    plots: dict[str, dict[str, Any]] = {}
    for item in _walk(payload):
        if not isinstance(item, dict) or not isinstance(item.get("scenes"), list):
            continue
        for number, scene in enumerate(item["scenes"], start=1):
            if not isinstance(scene, dict):
                continue
            plot_id = str(scene.get("plot_id") or "")
            if not plot_id or plot_id in plots:
                continue
            identity: dict[str, Any] = {"id": plot_id, "number": number}
            if scene.get("title"):
                identity["name"] = str(scene["title"])
            plots[plot_id] = identity
    return plots


def _clip_identity_index(
    payload: dict[str, Any],
    *,
    clip_parents: dict[str, str],
) -> dict[str, dict[str, Any]]:
    candidates: dict[str, tuple[int, dict[str, Any]]] = {}
    for item in _walk(payload):
        if not isinstance(item, dict):
            continue
        clip_id = str(item.get("clip_id") or "")
        title = str(item.get("title") or "")
        if not clip_id or not title:
            continue
        match = re.match(r"^(?:片段|拆分版)\s*(\d+[A-Z]?)", title)
        score = 3 if "｜" in title else 2 if match else 1
        current = candidates.get(clip_id)
        if current is not None and current[0] >= score:
            continue
        identity: dict[str, Any] = {"id": clip_id, "name": title}
        if match:
            identity["number"] = match.group(1)
        plot_id = str(item.get("plot_id") or clip_parents.get(clip_id) or "")
        if plot_id:
            identity["plot_id"] = plot_id
        candidates[clip_id] = (score, identity)
    return {clip_id: value for clip_id, (_, value) in candidates.items()}


def _project_identity(payload: dict[str, Any], project_id: str) -> dict[str, str]:
    identity = {"id": project_id}
    for item in _walk(payload):
        if not isinstance(item, dict) or str(item.get("id") or "") != project_id:
            continue
        if item.get("title"):
            identity["name"] = str(item["title"])
            break
    return identity


def _enrich_route_target(
    *,
    plot_id: str = "",
    clip_id: str = "",
    plot_identities: dict[str, dict[str, Any]],
    clip_identities: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    target: dict[str, Any] = {}
    if plot_id:
        plot = plot_identities.get(plot_id, {"id": plot_id})
        target["plot"] = plot
    if clip_id:
        clip = clip_identities.get(clip_id, {"id": clip_id})
        target["clip"] = clip
        if "plot" not in target and clip.get("plot_id"):
            target["plot"] = plot_identities.get(clip["plot_id"], {"id": clip["plot_id"]})
    return target


def _turn_operated_clips(
    turn: dict[str, Any],
    *,
    project_id: str,
    clip_parents: dict[str, str],
) -> list[dict[str, str]]:
    operated_clips: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for source_index, call in enumerate(_iter_recorded_tool_calls(turn)):
        event = _compact_tool_call(call, source_index)
        if event is None or str(event.get("action") or "") not in _MUTATING_TOOL_ACTIONS:
            continue
        execution = event.get("execution") or {}
        if execution.get("ok") is False or execution.get("phase") in {"failed", "cancelled"}:
            continue

        for item in [event.get("target") or {}, *(event.get("objects") or [])]:
            clip_id = str(item.get("clip_id") or "")
            if not clip_id:
                continue
            item_project_id = str(item.get("project_id") or project_id)
            if item_project_id != project_id:
                continue
            plot_id = str(item.get("plot_id") or clip_parents.get(clip_id) or "")
            key = (item_project_id, plot_id, clip_id)
            if key in seen:
                continue
            seen.add(key)
            operation_record = {"clip_id": clip_id}
            if plot_id:
                operation_record["plot_id"] = plot_id
            operated_clips.append(operation_record)
    return operated_clips


def _user_turn_context_prefix(
    *,
    turn_id: str,
    project: dict[str, str],
    current_focus: dict[str, Any] | None,
    referenced_targets: list[dict[str, Any]],
    operated_clips: list[dict[str, str]],
    plot_identities: dict[str, dict[str, Any]],
    clip_identities: dict[str, dict[str, Any]],
    max_chars: int = USER_TURN_CONTEXT_MAX_CHARS,
) -> str:
    focus = dict(current_focus or {})
    focus_type = str(focus.get("type") or "")
    focus_id = str(focus.get("id") or "")
    candidates: dict[tuple[str, str], dict[str, Any]] = {}

    def merge_candidate(target: dict[str, Any], source: str, reference: dict | None = None) -> None:
        plot_id = str((target.get("plot") or {}).get("id") or "")
        clip_id = str((target.get("clip") or {}).get("id") or "")
        candidate = candidates.setdefault((plot_id, clip_id), {**target, "sources": []})
        if source not in candidate["sources"]:
            candidate["sources"].append(source)
        for input_key, output_key in (
            ("resource_id", "resource_ids"),
            ("take_id", "take_ids"),
            ("asset_id", "asset_ids"),
        ):
            value = (reference or {}).get(input_key)
            if value:
                values = candidate.setdefault(output_key, [])
                if value not in values:
                    values.append(value)

    for reference in referenced_targets:
        merge_candidate(reference["target"], "user_reference", reference)
    for operated_clip in operated_clips:
        merge_candidate(
            _enrich_route_target(
                plot_id=str(operated_clip.get("plot_id") or ""),
                clip_id=operated_clip["clip_id"],
                plot_identities=plot_identities,
                clip_identities=clip_identities,
            ),
            "assistant_clip_operation",
        )

    recorded_focus = None
    if not candidates:
        if focus_type == "project":
            candidates[("", "")] = {"scope": "project", "sources": ["current_focus"]}
        elif focus_type == "plot" and focus_id:
            merge_candidate(
                _enrich_route_target(
                    plot_id=focus_id,
                    plot_identities=plot_identities,
                    clip_identities=clip_identities,
                ),
                "current_focus",
            )
        elif focus_type == "clip" and focus_id:
            merge_candidate(
                _enrich_route_target(
                    clip_id=focus_id,
                    plot_identities=plot_identities,
                    clip_identities=clip_identities,
                ),
                "current_focus",
            )
        elif focus:
            recorded_focus = focus
    elif focus:
        routed_plot_ids = {
            str((candidate.get("plot") or {}).get("id") or "") for candidate in candidates.values()
        }
        if focus_type == "plot" and focus_id not in routed_plot_ids:
            recorded_focus = focus
        elif focus_type not in {"project", "plot", "clip"}:
            recorded_focus = focus

    all_targets = list(candidates.values())
    selected_targets: list[dict[str, Any]] = []
    total_target_count = len(all_targets)
    for target in all_targets:
        selected_candidate = [*selected_targets, target]
        candidate = {
            "routing_only": True,
            "turn_id": turn_id,
            "project": project,
            "targets": selected_candidate,
            "target_count": total_target_count,
            "targets_truncated": len(selected_candidate) < total_target_count,
        }
        if recorded_focus:
            candidate["recorded_focus"] = recorded_focus
        encoded = json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))
        rendered = f"{USER_TURN_CONTEXT_BEGIN}\n{encoded}\n{USER_TURN_CONTEXT_END}\n"
        if len(rendered) > max_chars:
            break
        selected_targets = selected_candidate

    payload = {
        "routing_only": True,
        "turn_id": turn_id,
        "project": project,
        "targets": selected_targets,
        "target_count": total_target_count,
        "targets_truncated": len(selected_targets) < total_target_count,
    }
    if recorded_focus:
        payload["recorded_focus"] = recorded_focus
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    rendered = f"{USER_TURN_CONTEXT_BEGIN}\n{encoded}\n{USER_TURN_CONTEXT_END}\n"
    if len(rendered) > max_chars:
        raise AssertionError(f"user turn context exceeded {max_chars} characters: {len(rendered)}")
    return rendered


def _iso_time(milliseconds: int | None) -> str:
    if not milliseconds:
        return datetime.now(timezone.utc).isoformat()
    return datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc).isoformat()


def _messages_from_session(
    payload: dict[str, Any],
    *,
    include_assistant_messages: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    """Build an extraction transcript with self-contained User routing context.

    Original user text is preserved and assistant prose can be omitted for source-policy
    experiments. Raw tool calls/results are omitted, matching the default user-memory provider.
    Every User message receives a bounded, routing-only prefix containing the
    identities relevant to that turn. SessionMeta only describes this convention;
    neither source is conversation evidence.
    """
    turns = payload["data"]["turns"]
    enabled_focus_types = _configured_focus_types()
    project_id = str(payload.get("projectId") or "proj_00G6pawQ")
    references = _reference_index(payload)
    clip_parents = _clip_parent_index(payload)
    plot_identities = _plot_identity_index(payload)
    clip_identities = _clip_identity_index(payload, clip_parents=clip_parents)
    project = _project_identity(payload, project_id)
    user_resource_ids = list(
        dict.fromkeys(
            resource_id
            for turn in turns
            for resource_id in RESOURCE_ID_RE.findall(str(turn.get("userMessage") or ""))
        )
    )
    resolved_references = {
        resource_id: references[resource_id]
        for resource_id in user_resource_ids
        if resource_id in references
    }
    context = {
        "routing_mode": "self_contained_user_messages",
        "rule": (
            "Each User message begins with SS_TURN_CONTEXT routing metadata followed by "
            "USER_TEXT. SS_TURN_CONTEXT is not user intent and must never contribute "
            "memory prose; only USER_TEXT may contribute memory content."
        ),
    }
    encoded_context = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
    messages: list[dict[str, Any]] = []
    tool_call_count = sum(len(list(_iter_recorded_tool_calls(turn))) for turn in turns)
    assistant_message_count = 0
    source_assistant_message_count = 0
    user_turn_context_count = 0
    user_turn_context_chars = 0
    user_turn_context_reference_count = 0
    user_turn_context_operated_clip_count = 0
    user_turn_context_operated_clip_total = 0
    user_turn_context_truncated_count = 0

    for turn_index, turn in enumerate(turns, start=1):
        user_text = str(turn.get("userMessage") or "").strip()
        if not user_text:
            continue
        current_focus = turn.get("currentFocus") or {}
        focus_type = str(current_focus.get("type") or "")
        if focus_type and focus_type not in enabled_focus_types:
            continue
        created_at = _iso_time(turn.get("userMessageOccurredAt"))
        turn_id = str(turn.get("turnId") or f"ss-turn-{turn_index}")
        referenced_targets = []
        for resource_id in dict.fromkeys(RESOURCE_ID_RE.findall(user_text)):
            for reference in references.get(resource_id, []):
                if not reference.get("plot_id") and not reference.get("clip_id"):
                    continue
                target = _enrich_route_target(
                    plot_id=str(reference.get("plot_id") or ""),
                    clip_id=str(reference.get("clip_id") or ""),
                    plot_identities=plot_identities,
                    clip_identities=clip_identities,
                )
                for key in ("take_id", "asset_id"):
                    if reference.get(key):
                        target[key] = reference[key]
                record = {"resource_id": resource_id, "target": target}
                if record not in referenced_targets:
                    referenced_targets.append(record)
        operated_clips = _turn_operated_clips(
            turn,
            project_id=project_id,
            clip_parents=clip_parents,
        )
        turn_context_prefix = _user_turn_context_prefix(
            turn_id=turn_id,
            project=project,
            current_focus=turn.get("currentFocus"),
            referenced_targets=referenced_targets,
            operated_clips=operated_clips,
            plot_identities=plot_identities,
            clip_identities=clip_identities,
        )
        context_payload = json.loads(turn_context_prefix.splitlines()[1])
        user_turn_context_count += 1
        user_turn_context_chars += len(turn_context_prefix)
        user_turn_context_reference_count += sum(
            "user_reference" in target["sources"] for target in context_payload["targets"]
        )
        user_turn_context_operated_clip_count += sum(
            "assistant_clip_operation" in target["sources"] for target in context_payload["targets"]
        )
        user_turn_context_operated_clip_total += len(operated_clips)
        user_turn_context_truncated_count += int(context_payload["targets_truncated"])
        messages.append(
            {
                "role": "user",
                "parts": [TextPart(f"{turn_context_prefix}{USER_TEXT_BEGIN}\n{user_text}")],
                "created_at": created_at,
                "turn_id": turn_id,
                "message_kind": "user_query",
            }
        )
        for assistant_index, assistant in enumerate(turn.get("agentMessages") or [], start=1):
            assistant_text = str(assistant.get("text") or "").strip()
            if not assistant_text:
                continue
            source_assistant_message_count += 1
            if not include_assistant_messages:
                continue
            messages.append(
                {
                    "role": "assistant",
                    "parts": [TextPart(assistant_text)],
                    "created_at": _iso_time(assistant.get("occurredAt")),
                    "turn_id": turn_id,
                    "message_kind": "assistant_step",
                    "source_message_ids": [
                        str(assistant.get("id") or f"{turn_id}-assistant-{assistant_index}")
                    ],
                }
            )
            assistant_message_count += 1

    manifest = {
        "source_turn_count": len(turns),
        "enabled_focus_types": sorted(enabled_focus_types),
        "projected_message_count": len(messages),
        "projected_user_messages": sum(item["role"] == "user" for item in messages),
        "projected_assistant_messages": assistant_message_count,
        "projected_system_messages": 0,
        "omitted_assistant_messages": source_assistant_message_count - assistant_message_count,
        "source_tool_calls": tool_call_count,
        "included_tool_calls": 0,
        "identity_reference_count": len(resolved_references),
        "identity_context_chars": len(encoded_context),
        "user_turn_context_count": user_turn_context_count,
        "user_turn_context_chars": user_turn_context_chars,
        "user_turn_context_reference_count": user_turn_context_reference_count,
        "user_turn_context_operated_clip_count": user_turn_context_operated_clip_count,
        "user_turn_context_operated_clip_total": user_turn_context_operated_clip_total,
        "user_turn_context_truncated_count": user_turn_context_truncated_count,
        "context_augmentation": (
            "Each User message has a self-contained SS_TURN_CONTEXT with project, "
            "focus, reference, and successful Clip-operation identities; SessionMeta "
            "only describes the routing convention; no raw tool calls or results"
        ),
        "content_source_policy": (
            "Only text after USER_TEXT contributes memory content; SS_TURN_CONTEXT is routing "
            "context only; Assistant prose is "
            + ("included as non-evidence context" if include_assistant_messages else "omitted")
        ),
    }
    return messages, manifest, encoded_context


def _load_test_config(workspace: Path, schema_dir: Path) -> dict[str, Any]:
    config_path = resolve_config_path(None, OPENVIKING_CONFIG_ENV, DEFAULT_OV_CONF)
    if config_path is None:
        raise FileNotFoundError("OpenViking ov.conf is required for the live extraction test")
    config = copy.deepcopy(load_json_config(config_path))
    config.setdefault("memory", {}).update(
        {
            "custom_templates_dir": str(schema_dir),
            "extraction_enabled": True,
            "session_skill_extraction_enabled": False,
            "link_enabled": False,
            "eager_prefetch": False,
        }
    )
    output_format = os.environ.get("SS_MEMORY_OUTPUT_FORMAT")
    if output_format:
        config["memory"]["extraction_output_format"] = output_format
    config.setdefault("agent_evolution", {})["enabled"] = False
    config.setdefault("storage", {})["workspace"] = str(workspace)
    return config


async def _wait_for_task(
    service: OpenVikingService,
    ctx: RequestContext,
    task_id: str,
    timeout: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = await service.sessions.get_commit_task(task_id, ctx) or {}
        status = last.get("status")
        print(f"[ss-memory] task={task_id} status={status}", flush=True)
        if status in {"completed", "failed", "cancelled"}:
            return last
        await asyncio.sleep(1)
    raise TimeoutError(f"Session commit did not finish in {timeout}s: {last}")


def _uri_output_path(output_dir: Path, uri: str) -> Path:
    parsed = urlsplit(uri)
    relative = Path(parsed.netloc, *[part for part in parsed.path.split("/") if part])
    return output_dir / "files" / relative


def _expected_uris(manifest_path: Path, memory_types: set[str]) -> set[str]:
    if not manifest_path.exists():
        return set()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return {
        str(item["uri"])
        for item in manifest.get("schema_instances", [])
        if item.get("uri") and item.get("memory_ids") and f"ss_{item.get('scope')}" in memory_types
    }


async def run_actual_extraction(
    *,
    source_path: Path,
    expected_manifest_path: Path,
    schema_dir: Path,
    output_dir: Path,
    workspace: Path,
    timeout: float,
) -> dict[str, Any]:
    payload = _load_source(source_path)
    include_assistant_messages = os.environ.get(
        "SS_MEMORY_INCLUDE_ASSISTANT_MESSAGES", "1"
    ).strip().lower() not in {"0", "false", "no"}
    message_specs, input_manifest, extraction_context = _messages_from_session(
        payload,
        include_assistant_messages=include_assistant_messages,
    )
    enabled_memory_types = _configured_memory_types()
    extract_max_iterations = _configured_extract_max_iterations()
    extract_thinking = _configured_extract_thinking()
    input_manifest["enabled_memory_types"] = sorted(enabled_memory_types)
    input_manifest["extract_max_iterations"] = extract_max_iterations
    input_manifest["extract_thinking"] = extract_thinking
    output_dir.mkdir(parents=True, exist_ok=False)
    (output_dir / "input_manifest.json").write_text(
        json.dumps(input_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # These caches are process-global. Reset them so the test always loads the
    # schemas and model/storage handles from this isolated configuration.
    from openviking.session.memory import memory_type_registry as registry_module
    from openviking.session.memory import streaming_memory_updater as updater_module

    OpenVikingConfigSingleton.reset_instance()
    registry_module._default_registry = None
    updater_module._streaming_memory_updater_registry.clear()
    OpenVikingConfigSingleton.initialize(
        config_dict=_load_test_config(workspace=workspace, schema_dir=schema_dir)
    )

    user = UserIdentifier("default", "ss_memory_eval_user")
    ctx = RequestContext(user=user, role=Role.USER)
    service: OpenVikingService | None = None
    try:
        service = OpenVikingService(path=str(workspace), user=user)
        await service.initialize()
        session_id = f"ss-ground-truth-{int(time.time())}"
        memory_policy = {
            "self": {"enabled": True},
            "peer": {"enabled": False},
            "working_memory": {"enabled": False},
            "memory_types": sorted(enabled_memory_types),
            "extract_max_iterations": extract_max_iterations,
            "extract_thinking": extract_thinking,
        }
        auto_commit_policy = {
            "pending_token_threshold": int(os.environ.get("SS_MEMORY_BATCH_TOKEN_LIMIT", "55000")),
            "message_count_threshold": int(os.environ.get("SS_MEMORY_BATCH_MESSAGE_LIMIT", "50")),
            "idle_timeout_seconds": 86_400,
            "keep_recent_count": 0,
            "min_commit_interval_seconds": 0,
        }
        session = await service.sessions.create(
            ctx,
            session_id=session_id,
            memory_policy=memory_policy,
            auto_commit_policy=auto_commit_policy,
            extraction_context=extraction_context,
        )
        await session.add_messages_async(message_specs)
        commit = await session.commit_async(memory_policy=memory_policy)
        task_id = str(commit.get("task_id") or "")
        if not task_id:
            raise AssertionError(f"Session commit did not return a task_id: {commit}")
        task = await _wait_for_task(service, ctx, task_id, timeout)
        if task.get("status") != "completed":
            raise AssertionError(f"Session commit failed: {task}")

        archive_uri = str(commit.get("archive_uri") or "")
        diff_uri = f"{archive_uri.rstrip('/')}/memory_diff.json"
        memory_diff = json.loads(await service.fs.read(diff_uri, ctx=ctx))
        operations = memory_diff.get("operations") or {}
        changed = [
            item
            for kind in ("adds", "updates")
            for item in (operations.get(kind) or [])
            if item.get("memory_type") in enabled_memory_types and item.get("uri")
        ]
        deleted_uris = {
            str(item["uri"])
            for item in (operations.get("deletes") or [])
            if item.get("memory_type") in enabled_memory_types and item.get("uri")
        }
        # A batched commit may update the same URI several times. Preserve the
        # operation history in memory_diff, but materialize each surviving file once.
        latest_change_by_uri = {
            str(item["uri"]): item for item in changed if str(item["uri"]) not in deleted_uris
        }
        actual_files = []
        for item in latest_change_by_uri.values():
            uri = str(item["uri"])
            raw = await service.fs.read(uri, ctx=ctx)
            destination = _uri_output_path(output_dir, uri)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(raw, encoding="utf-8")
            parsed = MemoryFileUtils.read(raw, uri=uri)
            actual_files.append(
                {
                    "memory_type": item["memory_type"],
                    "uri": uri,
                    "path": str(destination.relative_to(output_dir)),
                    "content": parsed.content,
                    "fields": parsed.extra_fields,
                }
            )

        actual_uris = {item["uri"] for item in actual_files}
        expected_uris = _expected_uris(expected_manifest_path, enabled_memory_types)
        intersection = actual_uris & expected_uris
        comparison = {
            "expected_uri_count": len(expected_uris),
            "actual_uri_count": len(actual_uris),
            "matched_uri_count": len(intersection),
            "uri_precision": len(intersection) / len(actual_uris) if actual_uris else 0.0,
            "uri_recall": len(intersection) / len(expected_uris) if expected_uris else None,
            "matched_uris": sorted(intersection),
            "missing_expected_uris": sorted(expected_uris - actual_uris),
            "unexpected_actual_uris": sorted(actual_uris - expected_uris),
            "user_memory_created": any(item["memory_type"] == "ss_user" for item in actual_files),
            "note": "URI comparison measures object routing only; semantic field correctness requires reviewing actual files against ground_truth.json.",
        }
        result = {
            "source": str(source_path),
            "schemas": str(schema_dir),
            "enabled_memory_types": sorted(enabled_memory_types),
            "session_id": session_id,
            "commit": commit,
            "task": task,
            "archive_uri": archive_uri,
            "memory_diff": memory_diff,
            "actual_files": actual_files,
            "comparison": comparison,
        }
        (output_dir / "actual_extraction.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (output_dir / "comparison.json").write_text(
            json.dumps(comparison, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(comparison, ensure_ascii=False, indent=2), flush=True)
        return result
    finally:
        if service is not None:
            await service.close()
        updater_module._streaming_memory_updater_registry.clear()
        registry_module._default_registry = None
        OpenVikingConfigSingleton.reset_instance()


def test_narrow_scope_schemas_require_substantive_user_facts() -> None:
    user_schema = (FIXTURE_SCHEMAS / "ss_user.yaml").read_text(encoding="utf-8")
    plot_schema = (FIXTURE_SCHEMAS / "ss_plot.yaml").read_text(encoding="utf-8")
    clip_schema = (FIXTURE_SCHEMAS / "ss_clip.yaml").read_text(encoding="utf-8")
    project_schema = (FIXTURE_SCHEMAS / "ss_project.yaml").read_text(encoding="utf-8")

    schemas = {
        "ss_user": user_schema,
        "ss_project": project_schema,
        "ss_plot": plot_schema,
        "ss_clip": clip_schema,
    }
    expected_fields = {
        "ss_user": ["content"],
        "ss_project": ["project_id", "content"],
        "ss_plot": ["project_id", "plot_id", "content"],
        "ss_clip": ["project_id", "plot_id", "clip_id", "content"],
    }
    expected_directories = {
        memory_type: "viking://user/{{ user_space }}/memories/ss"
        + ("" if memory_type == "ss_user" else "/projects")
        for memory_type in schemas
    }
    for memory_type, schema in schemas.items():
        assert "分类与作用层级相互独立" in schema
        for label in (
            "[基础配置]",
            "[额外配置]",
            "[单次行为]",
            "[结果评价]",
            "[任务输入]",
            "[已失效]",
            "[模型推断]",
            "[非用户来源]",
            "[上下文不足]",
        ):
            assert label in schema
        assert "只有 [额外配置] 可以写入 Memory" in schema
        assert "标签只用于内部筛选和评测，禁止写入 content" in schema
        assert "不得通过给内容加上“[额外配置]”字样绕过筛选" in schema
        assert "每条候选必须拆成一个原子事实" in schema
        assert "事实必须由用户明确表达" in schema
        assert "不得补充要求、扩大对象或改变范围" in schema
        assert "明确采纳 Assistant 提议" in schema
        assert "作用层级只按用户当时表达" in schema
        assert "不能由分类标签决定" in schema
        assert "完整可用上下文" in schema
        assert "只能使用唯一匹配的真实 ID" in schema
        assert "禁止生成 placeholder" in schema
        assert "同一规则在多个 Plot 重复出现不能自动提升为 Project" in schema
        assert "同层级冲突时只保留最新明确要求" in schema
        assert "content 只含“- ”列表" in schema
        assert "一行一个最终约束" in schema
        assert "不写标签、标题、解释、过程、行号或 Tab" in schema
        assert "尽量不超过 40 字" in schema
        parsed = yaml.safe_load(schema)
        assert [field["name"] for field in parsed["fields"]] == expected_fields[memory_type]
        assert parsed["directory"] == expected_directories[memory_type]
        assert "content_template" not in parsed
        assert parsed["embedding_template"].strip() == "{{ content }}"

    assert "跨项目或未来每次协作" in user_schema
    assert "不推断为用户习惯" in user_schema
    assert "全片" in project_schema
    assert "只有用户原话明确表达项目级范围" in project_schema
    assert "ss_project 必须 no-op" in project_schema
    assert "没有子级文件也不能用 Project 兜底" in project_schema
    assert "全片每场留转场空镜" in project_schema
    assert "不能写入 ss_project" in plot_schema
    assert "涉及同一 Plot 的多个 Clip" in plot_schema
    assert "这场景别太一致" in plot_schema
    assert "把指代唯一解析到一个真实 Clip" in clip_schema
    assert "不能写入父 Plot 或 Project" in clip_schema
    assert "其他不变，只把信封改成中文" in clip_schema
    assert "Take 不是第五层" in clip_schema
    assert (
        'filename_template: "{{ project_id }}/plots/{{ plot_id }}/clips/{{ clip_id }}/clip.md"'
        in clip_schema
    )


@pytest.mark.parametrize("output_format", ["python", "json"])
def test_explicit_clip_scope_rules_are_rendered_from_ss_schemas(output_format) -> None:
    from openviking.session.memory.extraction_output_protocol import (
        ExtractionOutputContext,
        create_extraction_output_protocol,
    )
    from openviking.session.memory.memory_type_registry import MemoryTypeRegistry
    from openviking.session.memory.merge_policy import MEMORY_MERGE_POLICY
    from openviking.session.memory.page_id_map import PageIdMap
    from openviking.session.memory.schema_model_generator import SchemaModelGenerator

    registry = MemoryTypeRegistry(load_schemas=False)
    for path in sorted(FIXTURE_SCHEMAS.glob("*.yaml")):
        registry.load_from_yaml(str(path))
    schemas = registry.list_all(include_disabled=False)
    by_type = {schema.memory_type: schema for schema in schemas}
    priority = (
        "单个 Clip 的局部要求已唯一定位时，targets 只含 clip 或同时含 plot 均归 ss_clip；"
        "recorded_focus 为 Plot 不能改变该归属。"
    )
    for memory_type in ("ss_clip", "ss_plot"):
        assert priority in by_type[memory_type].description
        assert "用户明确要求整个 Plot" in by_type[memory_type].description
    clip = by_type["ss_clip"]
    assert "缺少父级信息也不能改写父级兜底" in clip.description
    parent_field = next(field for field in clip.fields if field.name == "plot_id")
    assert "可确认的父子关系" in parent_field.description
    assert "不能仅凭 recorded_focus 猜测" in parent_field.description
    context = ExtractionOutputContext(
        operations_model=SchemaModelGenerator(schemas).create_structured_operations_model(),
        schemas=tuple(schemas),
        page_id_map=PageIdMap(),
        read_file_contents={},
        link_enabled=False,
    )
    rendered = create_extraction_output_protocol(output_format).render_contract(context)
    assert priority in rendered
    # These concepts belong to the SS schemas, not the generic maintenance policy.
    assert "recorded_focus" not in MEMORY_MERGE_POLICY
    assert "ss_clip" not in MEMORY_MERGE_POLICY
    assert "ss_plot" not in MEMORY_MERGE_POLICY


def test_configured_extract_thinking_defaults_to_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SS_MEMORY_EXTRACT_THINKING", raising=False)
    assert _configured_extract_thinking() is False


@pytest.mark.parametrize("value, expected", [("true", True), ("false", False)])
def test_configured_extract_thinking_accepts_booleans(monkeypatch, value, expected) -> None:
    monkeypatch.setenv("SS_MEMORY_EXTRACT_THINKING", value)
    assert _configured_extract_thinking() is expected


@pytest.mark.parametrize("value", ["1", "null", '"true"'])
def test_configured_extract_thinking_rejects_nonbooleans(monkeypatch, value) -> None:
    from openviking_cli.exceptions import InvalidArgumentError

    monkeypatch.setenv("SS_MEMORY_EXTRACT_THINKING", value)
    with pytest.raises(InvalidArgumentError, match="extract_thinking must be a boolean"):
        _configured_extract_thinking()


def test_configured_extract_max_iterations_defaults_to_six(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SS_MEMORY_EXTRACT_MAX_ITERATIONS", raising=False)
    assert _configured_extract_max_iterations() == 6
    monkeypatch.setenv("SS_MEMORY_EXTRACT_MAX_ITERATIONS", "9")
    assert _configured_extract_max_iterations() == 9


def test_configured_extract_max_iterations_rejects_nonpositive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openviking_cli.exceptions import InvalidArgumentError

    monkeypatch.setenv("SS_MEMORY_EXTRACT_MAX_ITERATIONS", "0")
    with pytest.raises(
        InvalidArgumentError, match="extract_max_iterations must be a positive integer"
    ):
        _configured_extract_max_iterations()


def test_configured_memory_types_supports_isolated_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SS_MEMORY_TYPES", "ss_plot")
    assert _configured_memory_types() == {"ss_plot"}

    monkeypatch.setenv("SS_MEMORY_TYPES", "ss_plot,unknown")
    with pytest.raises(ValueError, match="Unknown SS_MEMORY_TYPES"):
        _configured_memory_types()


def test_configured_focus_types_filter_non_memory_entities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SS_MEMORY_FOCUS_TYPES", "plot,clip")
    payload = {
        "projectId": "proj_test",
        "data": {
            "turns": [
                {
                    "turnId": "turn-asset",
                    "userMessage": "把角色服装改成红色。",
                    "currentFocus": {"type": "character_asset", "id": "ca_test"},
                },
                {
                    "turnId": "turn-plot",
                    "userMessage": "这场改成夜景。",
                    "currentFocus": {"type": "plot", "id": "plot_test"},
                },
            ]
        },
    }

    messages, manifest, _ = _messages_from_session(payload, include_assistant_messages=False)

    assert len(messages) == 1
    assert messages[0]["turn_id"] == "turn-plot"
    assert manifest["source_turn_count"] == 2
    assert manifest["projected_user_messages"] == 1
    assert manifest["enabled_focus_types"] == ["clip", "plot"]


def test_expected_uris_filters_manifest_scope(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_instances": [
                    {"scope": "project", "uri": "viking://project", "memory_ids": ["P01"]},
                    {"scope": "plot", "uri": "viking://plot", "memory_ids": ["S01"]},
                    {"scope": "clip", "uri": "viking://clip", "memory_ids": []},
                ]
            }
        ),
        encoding="utf-8",
    )

    assert _expected_uris(manifest, {"ss_plot"}) == {"viking://plot"}


def test_compact_tool_evidence_is_bounded_and_drops_tool_prose() -> None:
    secret_prompt = "TOOL_PROMPT_MUST_NOT_SURVIVE"
    secret_result_text = "TOOL_RESULT_PROSE_MUST_NOT_SURVIVE"
    items = [
        {
            "clip_id": f"clip_{index:03d}",
            "plot_id": "plot_scene01",
            "duration_sec": 10,
            "title": f"片段 {index}",
            "text": secret_result_text,
        }
        for index in range(40)
    ]
    result = json.dumps({"ok": True, "data": {"items": items}}, ensure_ascii=False)
    turn = {
        "toolCalls": [
            {
                "callId": "call-segment-list",
                "phase": "completed",
                "input": json.dumps(
                    {
                        "command": (
                            f'ss-cli clip list -plot-id plot_scene01 -prompt "{secret_prompt}"'
                        ),
                        "description": secret_prompt,
                    },
                    ensure_ascii=False,
                ),
                "result": f"exit_code: 0\n--- stdout ---\n{result}\n\n--- stderr ---\n",
            }
        ]
    }

    compact = _compact_tool_evidence(turn, max_chars=700)
    encoded = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))

    assert len(encoded) <= 700
    assert compact["truncated"] is True
    assert compact["included_call_count"] == 1
    assert "plot_scene01" in encoded
    assert "clip_000" in encoded
    assert "identity_title" not in encoded
    assert "duration_sec" not in encoded
    assert secret_prompt not in encoded
    assert secret_result_text not in encoded


def test_turn_operated_clips_keep_only_successful_mutations() -> None:
    turn = {
        "toolCalls": [
            {
                "callId": "call-update",
                "phase": "completed",
                "tool": {
                    "family": "clip.patch",
                    "action": "update",
                    "target": {"clip_id": "clip_target"},
                    "output": {
                        "ok": True,
                        "raw_stdout_json": {
                            "ok": True,
                            "data": {"clip_id": "clip_target"},
                        },
                    },
                },
            },
            {
                "callId": "call-list",
                "phase": "completed",
                "tool": {
                    "family": "clip.list",
                    "action": "list",
                    "output": {
                        "ok": True,
                        "raw_stdout_json": {
                            "ok": True,
                            "data": {
                                "items": [{"plot_id": "plot_parent", "clip_id": "clip_read_only"}]
                            },
                        },
                    },
                },
            },
            {
                "callId": "call-failed",
                "phase": "failed",
                "tool": {
                    "family": "clip.patch",
                    "action": "update",
                    "target": {"clip_id": "clip_failed"},
                    "output": {"ok": False},
                },
            },
        ]
    }

    assert _turn_operated_clips(
        turn,
        project_id="proj_test",
        clip_parents={"clip_target": "plot_parent"},
    ) == [
        {
            "plot_id": "plot_parent",
            "clip_id": "clip_target",
        }
    ]


def test_user_turn_context_prefix_is_bounded() -> None:
    operations = [
        {
            "project_id": "proj_test",
            "plot_id": "plot_parent",
            "clip_id": f"clip_{index:03d}",
        }
        for index in range(200)
    ]

    prefix = _user_turn_context_prefix(
        turn_id="turn-many-clips",
        project={"id": "proj_test", "name": "测试项目"},
        current_focus={"id": "plot_parent", "type": "plot"},
        referenced_targets=[],
        operated_clips=operations,
        plot_identities={"plot_parent": {"id": "plot_parent", "number": 1, "name": "测试场"}},
        clip_identities={},
        max_chars=700,
    )
    encoded = prefix.split("\n", 2)[1]
    payload = json.loads(encoded)

    assert len(prefix) <= 700
    assert payload["routing_only"] is True
    assert payload["turn_id"] == "turn-many-clips"
    assert payload["target_count"] == 200
    assert payload["targets_truncated"] is True
    assert 0 < len(payload["targets"]) < 200


def test_messages_put_self_contained_context_on_user_and_omit_tools() -> None:
    payload = {
        "projectId": "proj_test",
        "data": {
            "routingCatalog": {
                "scenes": [{"plot_id": "plot_actual", "title": "测试场"}],
                "clips": [
                    {
                        "plot_id": "plot_actual",
                        "clip_id": "clip_actual",
                        "title": "片段 2｜测试片段",
                    }
                ],
            },
            "turns": [
                {
                    "turnId": "turn-1",
                    "userMessage": ('<reference source="video|ra_test">把这个片段调整为十秒。'),
                    "userMessageOccurredAt": 1_700_000_000_000,
                    "currentFocus": {"id": "clip_old", "type": "clip"},
                    "agentMessages": [
                        {"text": "FIRST_ASSISTANT_PROSE"},
                        {"text": "SECOND_ASSISTANT_PROSE"},
                    ],
                    "toolCalls": [],
                    "subagents": [
                        {
                            "toolCalls": [
                                {
                                    "callId": "call-patch",
                                    "phase": "completed",
                                    "tool": {
                                        "family": "clip.patch",
                                        "action": "update",
                                        "target": {
                                            "project_id": "proj_test",
                                            "plot_id": "plot_actual",
                                            "clip_id": "clip_actual",
                                        },
                                        "output": {
                                            "ok": True,
                                            "raw_stdout_json": {
                                                "data": {
                                                    "reference_resources": {
                                                        "ra_test": {
                                                            "owner_context": {
                                                                "clip_id": "clip_actual",
                                                                "plot_id": "plot_actual",
                                                            }
                                                        }
                                                    }
                                                }
                                            },
                                        },
                                    },
                                    "input": json.dumps(
                                        {
                                            "command": (
                                                "ss-cli clip patch -clip-id clip_actual "
                                                "-duration-sec 10 -text TOOL_TEXT_MUST_NOT_SURVIVE"
                                            )
                                        }
                                    ),
                                    "result": (
                                        "exit_code: 0\n--- stdout ---\n"
                                        '{"ok":true,"data":{"clip_id":"clip_actual",'
                                        '"plot_id":"plot_actual","duration_sec":10,'
                                        '"text":"TOOL_RESULT_MUST_NOT_SURVIVE"}}'
                                        "\n\n--- stderr ---\n"
                                    ),
                                }
                            ]
                        }
                    ],
                }
            ],
        },
    }

    messages, manifest, extraction_context = _messages_from_session(payload)
    all_text = "\n".join(message["parts"][0].text for message in messages)

    assert [message["role"] for message in messages] == ["user", "assistant", "assistant"]
    assert "clip_actual" not in extraction_context
    assert "plot_actual" not in extraction_context
    user_text = messages[0]["parts"][0].text
    assert user_text.startswith(USER_TURN_CONTEXT_BEGIN)
    assert user_text.endswith(
        f'{USER_TEXT_BEGIN}\n<reference source="video|ra_test">把这个片段调整为十秒。'
    )
    assert user_text.count(USER_TURN_CONTEXT_BEGIN) == 1
    assert '"turn_id":"turn-1"' in user_text
    assert '"id":"plot_actual","number":1,"name":"测试场"' in user_text
    assert '"id":"clip_actual","name":"片段 2｜测试片段","number":"2"' in user_text
    assert messages[1]["parts"][0].text == "FIRST_ASSISTANT_PROSE"
    assert messages[2]["parts"][0].text == "SECOND_ASSISTANT_PROSE"
    assert "SS_CONTEXT" not in all_text
    assert "TOOL_TEXT_MUST_NOT_SURVIVE" not in all_text
    assert "TOOL_RESULT_MUST_NOT_SURVIVE" not in all_text
    assert manifest["source_tool_calls"] == 1
    assert manifest["included_tool_calls"] == 0
    assert manifest["identity_reference_count"] == 1
    assert manifest["user_turn_context_count"] == 1
    assert manifest["user_turn_context_reference_count"] == 1
    assert manifest["user_turn_context_operated_clip_count"] == 1
    assert manifest["user_turn_context_operated_clip_total"] == 1
    assert manifest["user_turn_context_truncated_count"] == 0
    assert manifest["user_turn_context_chars"] > 0
    assert manifest["projected_system_messages"] == 0
    assert manifest["projected_assistant_messages"] == 2


def test_messages_can_omit_assistant_prose_without_losing_user_routing() -> None:
    payload = {
        "projectId": "proj_test",
        "data": {
            "routingCatalog": {
                "scenes": [{"plot_id": "plot_actual", "title": "测试场"}],
                "clips": [],
            },
            "turns": [
                {
                    "turnId": "turn-1",
                    "userMessage": "把这个场景调整为低饱和。",
                    "userMessageOccurredAt": 1_700_000_000_000,
                    "currentFocus": {"id": "plot_actual", "type": "plot"},
                    "agentMessages": [
                        {"text": "ASSISTANT_PROSE_MUST_NOT_SURVIVE"},
                        {"text": "SECOND_ASSISTANT_PROSE_MUST_NOT_SURVIVE"},
                    ],
                    "toolCalls": [],
                    "subagents": [],
                }
            ],
        },
    }

    messages, manifest, extraction_context = _messages_from_session(
        payload,
        include_assistant_messages=False,
    )
    all_text = "\n".join(message["parts"][0].text for message in messages)

    assert [message["role"] for message in messages] == ["user"]
    assert USER_TURN_CONTEXT_BEGIN in all_text
    assert '"id":"plot_actual","number":1,"name":"测试场"' in all_text
    assert f"{USER_TEXT_BEGIN}\n把这个场景调整为低饱和。" in all_text
    assert "ASSISTANT_PROSE_MUST_NOT_SURVIVE" not in all_text
    assert manifest["projected_user_messages"] == 1
    assert manifest["projected_assistant_messages"] == 0
    assert manifest["omitted_assistant_messages"] == 2
    assert manifest["content_source_policy"].endswith("Assistant prose is omitted")
    assert "plot_actual" not in extraction_context


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("RUN_SS_MEMORY_EXTRACTION_TEST") != "1",
    reason="set RUN_SS_MEMORY_EXTRACTION_TEST=1 to run the live SS memory extraction",
)
def test_real_ss_session_extracts_four_level_memory(tmp_path: Path) -> None:
    source = _configured_path("SS_MEMORY_SESSION_FILE", DEFAULT_SOURCE)
    expected = _configured_path("SS_MEMORY_EXPECTED_MANIFEST", DEFAULT_EXPECTED)
    output_base = _configured_path("SS_MEMORY_ACTUAL_DIR", tmp_path / "actual")
    run_id = os.environ.get("SS_MEMORY_RUN_ID") or datetime.now().strftime("run-%Y%m%d-%H%M%S")
    output = output_base / run_id
    initialized = init_tracer_from_server_config(load_server_config())
    if initialized is None or not tracer.is_enabled():
        pytest.fail("failed to initialize tracer; check server.observability.traces")
    try:
        with tracer.start_as_current_span("tests.integration.ss_four_level_memory_extraction"):
            trace_id = tracer.get_trace_id()
            print(f"[ss-memory] trace_id={trace_id}", flush=True)
            result = asyncio.run(
                run_actual_extraction(
                    source_path=source,
                    expected_manifest_path=expected,
                    schema_dir=_configured_path("SS_MEMORY_SCHEMA_DIR", FIXTURE_SCHEMAS),
                    output_dir=output,
                    workspace=tmp_path / "workspace",
                    timeout=float(os.environ.get("SS_MEMORY_TIMEOUT", "1800")),
                )
            )
            (output / "trace_id.txt").write_text(trace_id + "\n", encoding="utf-8")
    finally:
        _flush_tracer_provider()
    actual_files = result["actual_files"]
    assert actual_files, "the real extraction pipeline produced no SS memory files"
    enabled_memory_types = set(result["enabled_memory_types"])
    assert {item["memory_type"] for item in actual_files} <= enabled_memory_types
    for item in actual_files:
        content_lines = [line for line in item["content"].splitlines() if line.strip()]
        assert content_lines
        assert all(line.startswith("- ") for line in content_lines), item["uri"]
    if "ss_user" in enabled_memory_types:
        assert not result["comparison"]["user_memory_created"]
