# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Bound native conversation messages without creating a second evidence representation."""

from __future__ import annotations

import copy
import json
from typing import Any

MAX_SESSION_STATE_CHARS = 128 * 1024
_TRUNCATED = "\n...[truncated]...\n"


def serialize_state(state: Any) -> str:
    return json.dumps(state, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def bound_session_state(messages: list[dict[str, Any]], max_chars: int) -> list[dict[str, Any]]:
    """Keep recent whole messages, clipping one oversized latest message if necessary.

    The budget includes serialized metadata and JSON escaping, not only content. Roles,
    tool names and call IDs are never truncated. This is model input data, not a replayable
    chat request: a tool call alone is not evidence that the tool executed successfully.
    """
    if max_chars < 2:
        raise ValueError("Session state budget must fit a JSON array")
    selected = []
    used = 2  # JSON array delimiters.
    for message in reversed(messages):
        size = len(serialize_state(message))
        if used + size + bool(selected) > max_chars:
            if selected:
                break
            message = _clip_message(message, max_chars - 2)
            selected.append(message)
            break
        selected.append(copy.deepcopy(message))
        used += size + (len(selected) > 1)
    selected.reverse()
    return selected


def _clip_message(message: dict[str, Any], max_chars: int) -> dict[str, Any]:
    # Keep structural fields intact. Only message text and tool arguments are payloads.
    fields: list[tuple[dict[str, Any], str, str]] = []
    result = copy.deepcopy(message)
    content = result.get("content")
    if isinstance(content, str):
        fields.append((result, "content", content))
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                fields.append((part, "text", part["text"]))
    for call in result.get("tool_calls") or []:
        function = call.get("function") if isinstance(call, dict) else None
        if isinstance(function, dict) and isinstance(function.get("arguments"), str):
            fields.append((function, "arguments", function["arguments"]))

    def render(limit: int) -> int:
        for owner, key, value in fields:
            owner[key] = (
                value
                if len(value) <= limit
                else value[: limit // 2]
                + _TRUNCATED
                + (value[-(limit // 2) :] if limit // 2 else "")
            )
        return len(serialize_state(result))

    if render(0) > max_chars:
        raise ValueError("Session message metadata exceeds the state budget")
    low, high = 0, max((len(value) for _, _, value in fields), default=0)
    while low < high:
        mid = (low + high + 1) // 2
        if render(mid) <= max_chars:
            low = mid
        else:
            high = mid - 1
    render(low)
    return result
