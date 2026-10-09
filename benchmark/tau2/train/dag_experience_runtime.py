"""Recall and deliver Tau2 advice at user turns and consequential tool boundaries."""

from __future__ import annotations

import time
import uuid
from collections import Counter
from collections.abc import AsyncIterator
from typing import Any

from openviking.session.memory.experience.dag import DagTriggerCondition
from openviking.session.memory.experience.state import MAX_SESSION_STATE_CHARS, bound_session_state
from openviking.utils.multimodal import redact_image_data_urls

EXPERIENCE_REMINDER_MARKER = "[Experience Reminder]"
_MAX_QUERY_CHARS = 16_384


def _render_advice(advice: list[dict[str, Any]]) -> str:
    """Render model-facing guidance, keeping machine identity out of its display text."""
    sections = [EXPERIENCE_REMINDER_MARKER]
    for index, item in enumerate(advice, start=1):
        conditions = [DagTriggerCondition.model_validate(value) for value in item["conditions"]]
        sections.extend(["", f"### 建议 {index}", "", "**适用条件**"])
        if len(conditions) > 1:
            sections.append("以下条件需同时满足：")
        if not conditions:
            sections.append("- 未设置额外条件，请结合当前任务核对。")
        for condition in conditions:
            text = condition.description
            if condition.operator == "is_false":
                text = f"以下整体条件不成立：{text}"
            elif condition.operator == "equals":
                text = f"{text} —— 选择分支：{condition.values[0]}"
            elif condition.operator == "not_in":
                if not condition.values:
                    raise ValueError("Default branch must identify its excluded choices")
                text = f"{text} —— 选择其他明确分支（不属于：{'、'.join(condition.values)}）"
            sections.append("- " + text.replace("\n", "\n  "))
        sections.extend(["", "**建议**", item["instruction"]])
    return "\n".join(sections)


class Tau2DagExperienceRuntime:
    """One rollout's serial recall/insertion callbacks; no server-side session state."""

    def __init__(self, *, review_tools: set[str] | None = None) -> None:
        self.runtime_id = f"tau2_advice_{uuid.uuid4().hex}"
        self.review_tools = set(review_tools or ()) | {"transfer_to_human_agents", "done"}
        self.client: Any = None
        self.events: list[dict[str, Any]] = []
        self.recalls: list[dict[str, Any]] = []
        self._user_messages: list[tuple[str, str]] = []
        self._next_user_message = 0
        self._delivered_assistant_messages: list[str] = []
        self._delivered: set[tuple[str, str, str]] = set()
        self._pending: tuple[str, list[dict[str, Any]]] | None = None
        self._manager: Any = None
        self._session: Any = None
        self._dirty = False

    @staticmethod
    def _delivery_keys(metadata: dict[str, Any]) -> set[tuple[str, str, str]]:
        records = metadata.get("delivered_experience_advice", [])
        if not isinstance(records, list) or any(
            not isinstance(row, list)
            or len(row) != 3
            or any(not isinstance(value, str) or not value for value in row)
            for row in records
        ):
            raise ValueError("Invalid client advice delivery state")
        return {tuple(row) for row in records}

    def bind_session(self, manager: Any, key: Any) -> None:
        session = manager.get_or_create(key)
        self._delivered = self._delivery_keys(session.metadata)
        self._manager, self._session = manager, session

    async def _save(self) -> None:
        if self._session is None:
            raise RuntimeError("Advice delivery requires a bound client session")
        session = self._manager.get_or_create(self._session.key)
        keys = self._delivery_keys(session.metadata) | self._delivered
        session.metadata["delivered_experience_advice"] = [list(k) for k in sorted(keys)]
        await self._manager.save(session)  # Atomic save merges concurrent delivery records.
        self._session = session
        self._delivered.update(self._delivery_keys(session.metadata))
        self._dirty = False

    def record_user_message(self, content: str, assistant_content: str = "") -> None:
        if content := str(content or "").strip():
            self._user_messages.append((content, str(assistant_content or "").strip()))

    def record_delivered_assistant_message(self, content: str) -> None:
        if content := str(content or "").strip():
            self._delivered_assistant_messages.append(content)

    def _query(self, index: int) -> str:
        if index < 0:
            return ""
        content, previous = self._user_messages[index]
        if index == 0:
            return content
        query = f"Latest user message:\n{content}"
        if previous:
            query += f"\n\nPrevious assistant message:\n{previous}"
        return query + f"\n\nInitial user request:\n{self._user_messages[0][0]}"

    async def recall(
        self, messages: list[dict[str, Any]], *, before_tools: bool = False
    ) -> AsyncIterator[str]:
        if before_tools:
            calls = messages[-1].get("tool_calls", []) if messages else []
            names = [call["function"]["name"] for call in calls]
            if self.review_tools.isdisjoint(names):
                return
            index = len(self._user_messages) - 1
            query = f"Before executing tools: {', '.join(names)}\n\n{self._query(index)}"
            if note := await self._search(query, messages, index, "before_action"):
                yield note
            return
        while self._next_user_message < len(self._user_messages):
            index = self._next_user_message
            self._next_user_message += 1
            if note := await self._search(self._query(index), messages, index, "user_message"):
                yield note

    async def _search(
        self, query: str, messages: list[dict], index: int, trigger: str
    ) -> str | None:
        started = time.perf_counter()
        query, state, matched, instructions = query[:_MAX_QUERY_CHARS], [], [], []
        self._pending = None
        status, note = "unavailable", None
        try:
            state = _session_state(
                messages, delivered_assistant_messages=self._delivered_assistant_messages
            )
            if self.client is None:
                from vikingbot.openviking_mount.ov_server import VikingClient

                self.client = await VikingClient.create()
            result = await self.client.search_advice(state, query=query)
            self.events.extend(
                {**item, "decision_state": state} for item in result.get("experiences", [])
            )
            self.events.extend(
                {
                    **error,
                    "state": "decision_failed" if error.get("retryable") else "invalid",
                    "decision_state": state,
                }
                for error in result.get("errors", [])
            )
            matched = [
                uri.rsplit("/", 1)[-1].removesuffix(".md")
                for uri in result.get("matched_experience_uris", [])
            ]
            seen = set(self._delivered)
            for item in result.get("instructions", []):
                text = (
                    item["provider"].get("instruction") or item.get("description") or ""
                ).strip()
                key = (item["experience_uri"], item["slot_name"], text)
                if not all(isinstance(value, str) and value for value in key):
                    raise ValueError("Advice requires a URI, stable slot and nonempty instruction")
                if key in seen:
                    continue
                seen.add(key)
                instructions.append(
                    {
                        "experience_uri": key[0],
                        "slot_name": key[1],
                        "instruction": text,
                        "provider_type": item["provider"]["type"],
                        "conditions": item["conditions"],
                    }
                )
            if instructions:
                note = _render_advice(instructions)
                self._pending = (note, instructions)
                status = "advice"
            elif result.get("errors"):
                status = "unavailable"
            else:
                status = (
                    "no_applicable_advice" if matched or result.get("experiences") else "no_match"
                )
        except Exception as exc:
            self.events.append(
                {
                    "state": "search_failed",
                    "stage": trigger,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "decision_state": state,
                }
            )
            instructions = []
        self.recalls.append(
            {
                "user_message_index": index,
                "trigger": trigger,
                "query": query,
                "status": status,
                "matched_experiences": matched,
                "advice_count": len(instructions),
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
            }
        )
        return note

    async def record_injected(self, note: str) -> None:
        if self._pending is None or self._pending[0] != note:
            return
        _, instructions = self._pending
        self._pending = None
        for item in instructions:
            self._delivered.add((item["experience_uri"], item["slot_name"], item["instruction"]))
        for uri in dict.fromkeys(item["experience_uri"] for item in instructions):
            self.events.append(
                {
                    "experience_uri": uri,
                    "event": "reminder_delivered",
                    "delivered_advice": [
                        {k: item[k] for k in ("slot_name", "instruction", "provider_type")}
                        for item in instructions
                        if item["experience_uri"] == uri
                    ],
                }
            )
        self._dirty = True
        try:
            await self._save()
        except Exception as exc:
            self.events.append({"state": "delivery_state_write_failed", "error": str(exc)})
            raise

    async def close(self) -> None:
        if self._dirty:
            try:
                await self._save()
            except Exception as exc:
                self.events.append({"state": "delivery_state_write_failed", "error": str(exc)})
        if self.client is not None:
            client, self.client = self.client, None
            await client.close()


def _session_state(
    messages: list[dict[str, Any]], *, delivered_assistant_messages: list[str] | None = None
) -> list[dict[str, Any]]:
    """Keep native policy/history/tool fields, not private model reasoning or synthetic evidence."""
    delivered = Counter(delivered_assistant_messages or [])
    selected = []
    for message in reversed(messages):
        text = str(message.get("content") or "")
        if text.strip() == "Reflect on the results and decide next steps.":
            continue
        item = {
            key: message[key]
            for key in ("role", "content", "name", "tool_call_id", "tool_calls")
            if key in message
        }
        if not text.strip() and not item.get("tool_calls"):
            continue
        if item.get("role") == "assistant" and text.strip():
            item["delivered_to_user"] = delivered[text.strip()] > 0
            if item["delivered_to_user"]:
                delivered[text.strip()] -= 1
        selected.append(item)
    return bound_session_state(
        redact_image_data_urls(list(reversed(selected))), MAX_SESSION_STATE_CHARS
    )
