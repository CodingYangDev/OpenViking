# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Shared contract for memory extraction output protocols."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from openviking.session.memory.dataclass import MemoryFile, MemoryTypeSchema


class ExtractionOutputProtocolError(ValueError):
    """A model response does not satisfy the selected output protocol."""

    def __init__(self, message: str, *, allow_tool_retry: bool = False) -> None:
        super().__init__(message)
        # True when the failure means "the model tried to read/search from the
        # write-only SDK"; the loop should re-open native tools for the retry.
        self.allow_tool_retry = allow_tool_retry


@dataclass(slots=True)
class ExtractionOutputContext:
    operations_model: type[BaseModel]
    schemas: tuple[MemoryTypeSchema, ...]
    page_id_map: Any
    read_file_contents: dict[str, MemoryFile]
    link_enabled: bool
    role_scope: Any | None = None
    available_tools: tuple[str, ...] = ()
    # Populated only by a successfully parsed Python create_* program.
    create_page_ids: set[int] = field(default_factory=set)
    template_context: dict[str, Any] = field(default_factory=dict)
    memory_eval_bindings: dict[str, Any] = field(default_factory=dict)
    # Presentation only; link availability and validation still use link_enabled.
    include_link_example: bool = True


class ExtractionOutputProtocol(ABC):
    """Render and parse one model-facing extraction output protocol."""

    name: str

    @abstractmethod
    def render_contract(self, context: ExtractionOutputContext) -> str:
        """Render the output API/schema shown to the model."""

    @abstractmethod
    def render_reference_rules(self, context: ExtractionOutputContext) -> str:
        """Render rules for referring to existing and newly created memories."""

    @abstractmethod
    def parse(
        self, content: str, context: ExtractionOutputContext
    ) -> tuple[Any | None, str | None]:
        """Parse model output into the shared dynamic operations model."""

    @abstractmethod
    def render_final_instruction(self, context: ExtractionOutputContext) -> str:
        """Render the instruction used when tool iterations are exhausted."""

    @abstractmethod
    def render_format_retry(self, error: str | None = None, *, failed_content: str = "") -> str:
        """Render repair guidance, optionally including bounded failed-source context."""

    @abstractmethod
    def render_patch_repair(self, patch_errors: list[dict[str, Any]]) -> str:
        """Render guidance after a patch cannot be applied."""

    @abstractmethod
    def render_resolution_repair(self, issues: list[dict[str, Any]]) -> str:
        """Render guidance after event operations fail to resolve a write target.

        The loop preserves every already-successful operation and asks the model
        to re-emit only the corrected event operations, so the wording MUST match
        this protocol's own output shape (JSON object vs restricted Python SDK).
        """

    def get_memory_eval_bindings(self, context: ExtractionOutputContext) -> dict[str, Any]:
        """Snapshot server-provided result variables for the read-only eval tool."""
        return deepcopy(context.memory_eval_bindings)

    def render_binding_index(self) -> str:
        """Render a current access-expression index, without adding it to message history."""
        return ""

    def render_new_bindings(self, context: ExtractionOutputContext, *, source: str) -> str:
        """Render newly available existing-memory references, if the protocol needs them."""
        del context, source
        return ""

    @abstractmethod
    def render_tool_result_messages(
        self,
        context: ExtractionOutputContext,
        *,
        call_id: str | int,
        tool_name: str,
        params: dict[str, Any],
        result: Any,
        source: str,
    ) -> list[dict[str, Any]]:
        """Render one completed tool invocation into model-facing context."""

    def render_prefetch_messages(
        self,
        messages: list[dict[str, Any]],
        context: ExtractionOutputContext,
    ) -> list[dict[str, Any]]:
        rendered: list[dict[str, Any]] = []
        pending: dict[str, dict[str, Any]] = {}
        seen_ids: set[str] = set()
        for message in messages:
            if message.get("role") == "assistant" and message.get("tool_calls"):
                if pending:
                    raise ValueError("Prefetch started a tool call before answering prior calls")
                for call in message["tool_calls"]:
                    call_id = call["id"]
                    if call_id in seen_ids:
                        raise ValueError(f"Duplicate prefetch tool call ID: {call_id}")
                    seen_ids.add(call_id)
                    pending[call_id] = call["function"]
                rendered.append(message)
            elif message.get("role") == "tool":
                call_id = message["tool_call_id"]
                call = pending.pop(call_id, None)
                if call is None:
                    raise ValueError(f"Prefetch tool result has no matching request: {call_id}")
                pair = self.render_tool_result_messages(
                    context,
                    call_id=call_id,
                    tool_name=call["name"],
                    params=json.loads(call["arguments"]),
                    result=json.loads(message["content"]),
                    source="prefetch",
                )
                rendered.extend(item for item in pair if item["role"] == "tool")
            else:
                if pending:
                    raise ValueError("Prefetch context interrupted pending tool results")
                rendered.append(self._normalize_message_instruction(message))
        if pending:
            raise ValueError("Prefetch tool calls are missing results")
        return rendered

    def _normalize_message_instruction(self, message: dict[str, Any]) -> dict[str, Any]:
        content = message.get("content")
        if not isinstance(content, str):
            return message
        normalized = self.normalize_provider_instruction(content)
        if normalized == content:
            return message
        return {**message, "content": normalized}

    def normalize_provider_instruction(self, instruction: str) -> str:
        """Adapt provider-specific output wording to this protocol."""
        return instruction

    def describe_empty_response(self) -> str | None:
        """Return the parse-error text for an empty model response, if any."""
        return None

    def keep_tools_enabled_after_parse_error(self, error: str | None) -> bool:
        """Return whether native tools should remain available for the retry."""
        del error
        return False
