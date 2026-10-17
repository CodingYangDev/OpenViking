# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""
Agent Trajectory Context Provider - Phase 1 of agent-scope extraction.

Extracts execution trajectories from the conversation and can optionally
co-extract reusable executable skills in the same ReAct pass.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from openviking.server.identity import RequestContext
from openviking.session.memory.dataclass import ResolvedOperations
from openviking.session.memory.experience.dag import clip_evidence_summary, tool_evidence_summary
from openviking.session.memory.experience.feedback import normalize_action_scores
from openviking.session.memory.experience.rubric import snapshot_rubric_results
from openviking.session.memory.memory_type_registry import MemoryTypeRegistry
from openviking.session.memory.session_extract_context_provider import SessionExtractContextProvider
from openviking.session.skill.session_skill_context_provider import (
    SESSION_SKILL_MEMORY_TYPE,
    SessionSkillContextProvider,
    resolve_skill_extract_templates_dir,
)
from openviking_cli.utils import get_logger

logger = get_logger(__name__)

TRAJECTORY_MEMORY_TYPE = "trajectories"
SYSTEM_STEP_MAX_CHARS = 32_768


class AgentTrajectoryContextProvider(SessionExtractContextProvider):
    """Phase 1 provider: extract trajectories and optional session skills."""

    include_tool_parts_in_conversation = True
    split_long_text_messages_for_extraction = False

    _SHARED_SKILL_STATE = {
        "messages",
        "latest_archive_overview",
        "_output_language",
        "_extract_context",
        "_isolation_handler",
        "_read_file_contents",
        "_allowed_memory_read_uris",
        "_ctx",
        "_viking_fs",
        "_transaction_handle",
    }

    def __init__(
        self,
        *args,
        include_trajectories: bool = True,
        include_session_skills: bool = False,
        case: dict[str, Any] | None = None,
        task_spec: dict[str, Any] | None = None,
        external_evaluation: dict[str, Any] | None = None,
        trajectory_name: str = "",
        case_name: str = "",
        outcome: str = "",
        target_uri: str = "",
        source_steps: list[dict[str, str]] | None = None,
        injected_actions: list[dict[str, Any]] | None = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self._include_trajectories = include_trajectories
        self._include_session_skills = include_session_skills
        self.case = dict(case or {})
        self.rubric = [
            {"id": item["name"], "description": item["description"]}
            for item in self.case.get("rubric", {}).get("criteria", [])
        ]
        self.case["rubric"] = self.rubric
        self.task_spec = task_spec
        self.external_evaluation = external_evaluation
        self.trajectory_name = trajectory_name
        self.case_name = case_name
        self.outcome = outcome
        self.target_uri = target_uri
        self.source_steps = source_steps
        self.injected_actions = list(injected_actions or [])
        self.action_score_sources = {
            item["id"]: {key: item[key] for key in ("experience_uri", "slot_name", "instruction")}
            for item in self.injected_actions
        }
        self._skill_provider = SessionSkillContextProvider(*args, **kwargs)
        self._sync_skill_provider_state()

    def __setattr__(self, name: str, value: Any) -> None:
        super().__setattr__(name, value)
        if name not in self._SHARED_SKILL_STATE:
            return
        skill_provider = self.__dict__.get("_skill_provider")
        if skill_provider is not None:
            setattr(skill_provider, name, value)

    def _sync_skill_provider_state(self, *, include_extract_context: bool = False) -> None:
        skill_provider = getattr(self, "_skill_provider", None)
        if skill_provider is None:
            return
        if include_extract_context and self._extract_context is None:
            self.get_extract_context()
        for attr in self._SHARED_SKILL_STATE:
            setattr(skill_provider, attr, getattr(self, attr))

    def instruction(self) -> str:
        language_instruction = f"Write new prose in {self.get_output_language()}.\n"
        if self.source_steps is not None:
            schema = self._get_registry().get(TRAJECTORY_MEMORY_TYPE)
            text_fields = {
                field.name: field
                for field in schema.fields
                if field.name in {"context_rules", "steps"}
            }
            if set(text_fields) != {"context_rules", "steps"} or any(
                not str(field.description or "").strip() for field in text_fields.values()
            ):
                raise ValueError(
                    "Trajectory context_rules and steps require extraction instructions"
                )
            field_instructions = "\n\n".join(
                f"{name}:\n{text_fields[name].description.strip()}"
                for name in ("context_rules", "steps")
            )
            return (
                language_instruction + field_instructions + "\n"
                "Create exactly one Trajectory and no other memory operation. Use:\n"
                "sdk.create_trajectories("
                "context_rules='''<relevant verbatim rules, or empty>''', "
                "steps='''1. `user` factual step\n...''', "
                "evaluation_criteria=r'''<YAML list>''', action_scores=r'''<YAML list>''')\n"
                "then sdk.commit(). The server supplies the trajectory identity, canonical Case, "
                "outcome and lifecycle metadata. Do not generate those fields.\n"
                "Keep context_rules and steps as separate triple-single-quoted string arguments. "
                "Do not add section titles; the content template renders them. "
                "System rules in source_steps are context, not dialogue steps; do not emit "
                "a step such as 'user received the policy'. Genuine user policy quotations "
                "and assistant explanations remain dialogue. "
                "Assess each supplied injected action in action_scores; keep steps factual. "
                "Use 1 for supported user-goal benefit or -1 for supported side effects. "
                "Otherwise omit score and explain why; do not use 0 or null."
            )
        return (
            language_instruction
            + "Compress each supplied user message, assistant message, Experience Reminder and tool "
            "record into one minimal factual step in the same order. Use system rules only as "
            "Session Context, never as numbered execution steps. Preserve exact relevant "
            "values. Do not analyze causes, write a future procedure or invent a corrected path."
        )

    def get_memory_schemas(self, ctx: RequestContext) -> List[Any]:
        """Expose trajectory schema and optionally session skill schema."""
        del ctx
        registry = self._get_registry()
        memory_types: List[str] = []
        if self._include_trajectories:
            memory_types.append(TRAJECTORY_MEMORY_TYPE)
        if self._include_session_skills:
            memory_types.append(SESSION_SKILL_MEMORY_TYPE)

        schemas: List[Any] = []
        for memory_type in memory_types:
            schema = registry.get(memory_type)
            if schema is None or not schema.enabled:
                continue
            schemas.append(schema)
        return schemas

    async def prefetch(self) -> List[Dict[str, Any]]:
        if self.source_steps is not None:
            return [
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "case": self.case,
                            "task_spec": self.task_spec,
                            "external_evaluation": self.external_evaluation,
                            "source_steps": self.source_steps,
                            "injected_actions": self.injected_actions,
                        },
                        ensure_ascii=False,
                    ),
                }
            ]
        if not self._include_session_skills:
            if not isinstance(self.messages, list):
                logger.warning(f"Expected List[Message], got {type(self.messages)}")
                return []
            return [self._build_conversation_message()]
        self._sync_skill_provider_state()
        return await self._skill_provider.prefetch()

    async def execute_tool(self, tool_call) -> Any:
        if not self._include_session_skills:
            return await super().execute_tool(tool_call)
        self._sync_skill_provider_state(include_extract_context=True)
        return await self._skill_provider.execute_tool(tool_call)

    def get_tools(self) -> List[str]:
        return self._skill_provider.get_tools() if self._include_session_skills else []

    def customize_tool_schema(self, tool_name: str, schema: Dict[str, Any]) -> Dict[str, Any]:
        if not self._include_session_skills:
            return schema
        self._sync_skill_provider_state(include_extract_context=True)
        return self._skill_provider.customize_tool_schema(tool_name, schema)

    def prepare_operation_fields(self, memory_type: str, fields: dict[str, Any]) -> dict[str, Any]:
        """Supply server-owned identity before the normal URI/isolation resolution."""
        if memory_type != TRAJECTORY_MEMORY_TYPE or self.source_steps is None:
            return fields
        return {
            **fields,
            "trajectory_name": self.trajectory_name,
            "case_name": self.case_name,
            "outcome": self.outcome,
        }

    def validate_resolved_operations(self, operations: ResolvedOperations) -> list[dict[str, Any]]:
        if self.source_steps is None:
            return []
        errors = []
        if operations.delete_file_contents or operations.delete_replacements:
            errors.append({"error": "Trajectory extraction cannot delete memories"})
        if len(operations.upsert_operations) != 1:
            errors.append({"error": "Trajectory extraction must create exactly one memory"})
            return errors
        operation = operations.upsert_operations[0]
        if operation.memory_type != TRAJECTORY_MEMORY_TYPE:
            errors.append({"error": "The only operation must create a Trajectory"})
            return errors
        try:
            criteria = snapshot_rubric_results(
                operation.memory_fields.get("evaluation_criteria", []), self.rubric
            )
            scores = normalize_action_scores(
                operation.memory_fields.get("action_scores", []),
                self.action_score_sources,
            )
        except ValueError as exc:
            return [{"error": str(exc)}]
        operation.memory_fields.update(
            {
                "trajectory_name": self.trajectory_name,
                "case_name": self.case_name,
                "outcome": self.outcome,
                "action_scores": scores,
                "evaluation_criteria": criteria,
                "action_score_sources": dict(self.action_score_sources),
            }
        )
        operation.uris = [self.target_uri]
        operation.old_memory_file_content = None
        operations.delete_file_contents = []
        operations.delete_replacements = {}
        operations.resolved_links = []
        return []

    @staticmethod
    def render_operation_repair(errors: list[dict[str, Any]]) -> str:
        return (
            "The Trajectory operation was invalid. Re-emit the complete Python SDK program with "
            "exactly one sdk.create_trajectories(...) and sdk.commit(). Keep context_rules and steps "
            "each as a triple-single-quoted string ('''...'''). The template generates section titles; "
            "do not change the extracted text merely to match numbering or a step count. Do not "
            "emit raw Markdown outside the SDK call. Errors: "
            + json.dumps(errors, ensure_ascii=False)
        )

    def _get_registry(self) -> MemoryTypeRegistry:
        if self._registry is None:
            registry = MemoryTypeRegistry(load_schemas=self._include_trajectories)
            if self._include_session_skills:
                loaded = registry.load_from_directory(str(resolve_skill_extract_templates_dir()))
                if loaded == 0:
                    raise RuntimeError(
                        "No session skill schemas loaded from skill_extract templates"
                    )
            self._registry = registry
        return self._registry


def trajectory_source_steps(messages: list[Any]) -> list[dict[str, str]]:
    """Preserve message order/roles, with a larger independent budget for system text."""
    records: list[tuple[str, Any]] = []
    for message in messages:
        content = str(message.content or "")
        step_type = (
            "experience_reminder"
            if content.startswith("[Experience Reminder]")
            else str(message.role)
        )
        tool_parts = [part for part in message.parts if getattr(part, "tool_name", None)]
        # A tool-only transport message is not an empty user/assistant utterance.
        # Keep real empty dialogue and the text of mixed text/tool messages.
        if content.strip() or not tool_parts:
            records.append((step_type, content))
        for part in tool_parts:
            records.append((f"tool:{part.tool_name}", part))
    item_budget = min(16_384, max(1, 64_000 // max(1, len(records))))
    source_steps = []
    for step_type, value in records:
        if step_type.startswith("tool:"):
            content = tool_evidence_summary(
                value.tool_name,
                value.tool_output or "",
                tool_input=value.tool_input,
                tool_status=value.tool_status,
                max_chars=item_budget,
            )
        else:
            text = str(value)
            # Tau2 also records system policy in a "system:" text envelope.
            # Keep its original role/text, but do not squeeze rules as record count grows.
            text_budget = (
                SYSTEM_STEP_MAX_CHARS
                if step_type == "system" or text.lstrip().startswith("system:")
                else item_budget
            )
            content = clip_evidence_summary("text", text, text_budget)
        source_steps.append({"type": step_type, "content": content})
    return source_steps
