# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Session skill extraction provider for ReAct-based skill asset updates."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Tuple

from openviking.core.namespace import canonical_user_root
from openviking.core.skill_loader import SkillLoader
from openviking.prompts.manager import PromptManager
from openviking.session.memory.dataclass import MemoryFile
from openviking.session.memory.memory_type_registry import MemoryTypeRegistry
from openviking.session.memory.session_extract_context_provider import SessionExtractContextProvider
from openviking.session.memory.tools import MemoryEvalFunction, add_tool_call_pair_to_messages
from openviking.session.memory.utils import add_line_numbers, line_count, slice_content_lines
from openviking.session.memory.utils.messages import parse_memory_file_with_fields
from openviking_cli.exceptions import NotFoundError
from openviking_cli.utils import get_logger

logger = get_logger(__name__)

SESSION_SKILL_MEMORY_TYPE = "session_skills"


def resolve_skill_extract_templates_dir() -> Path:
    """Resolve the session skill schema directory."""
    return PromptManager._resolve_templates_dir(None) / "skill_extract"


def load_skill_extract_registry() -> MemoryTypeRegistry:
    registry = MemoryTypeRegistry(load_schemas=False)
    loaded = registry.load_from_directory(str(resolve_skill_extract_templates_dir()))
    if loaded == 0:
        raise RuntimeError("No session skill schemas loaded from skill_extract templates")
    return registry


def parse_skill_extract_file(raw_content: str) -> Dict[str, Any]:
    try:
        parsed = SkillLoader.parse(raw_content)
        return {
            "name": parsed.get("name", ""),
            "description": parsed.get("description", ""),
            "content": parsed.get("content", ""),
            "allowed_tools": parsed.get("allowed_tools", []),
            "tags": parsed.get("tags", []),
        }
    except Exception:
        return parse_memory_file_with_fields(raw_content)


def build_skill_read_result(
    raw_content: str,
    *,
    uri: str,
    offset: int = 0,
    limit: int = -1,
) -> Tuple[Dict[str, Any], MemoryFile]:
    parsed = parse_skill_extract_file(raw_content)
    stored = MemoryFile.from_parsed(uri=uri, parsed=dict(parsed))

    llm_result = {
        "name": parsed.get("name", ""),
        "description": parsed.get("description", ""),
        "allowed_tools": parsed.get("allowed_tools", []),
        "tags": parsed.get("tags", []),
    }
    plain_content = parsed.get("content", "") or ""
    visible_content = slice_content_lines(plain_content, offset=offset, limit=limit)
    if visible_content:
        llm_result["content"] = add_line_numbers(visible_content, start_line=offset + 1)
    elif line_count(plain_content) == 0:
        llm_result["content"] = (
            "<system-reminder>Warning: the file exists but the contents are empty.</system-reminder>"
        )
    else:
        llm_result["content"] = (
            "<system-reminder>Warning: the file exists but is shorter than the provided "
            f"offset ({offset + 1}). The file has {line_count(plain_content)} lines.</system-reminder>"
        )
    return llm_result, stored


class SessionSkillContextProvider(SessionExtractContextProvider):
    """Provider that reuses session ReAct extraction for real skill assets."""

    def instruction(self) -> str:
        return (
            "You are an extraction agent. Analyze the archived conversation, use eval's "
            "read-only functions when needed, and output only restricted Python memory SDK "
            "code that matches the schema descriptions."
        )

    def _skill_roots(self) -> List[str]:
        if not self._ctx:
            return []
        return [f"{canonical_user_root(self._ctx)}/skills", "viking://agent/skills"]

    def _get_memory_eval_functions(self) -> Dict[str, MemoryEvalFunction]:
        return {
            "read": MemoryEvalFunction(
                name="read",
                signature="read(uri: str, offset: int = 0, limit: int = -1)",
                description="Read one SKILL.md URI returned by ls.",
                allowed_arguments=frozenset({"uri", "offset", "limit"}),
                required_arguments=frozenset({"uri"}),
                kind="skill_read",
                executor=self._eval_read_skill,
            ),
            "ls": MemoryEvalFunction(
                name="ls",
                signature="ls(uri: str | list[str], limit: int = 100)",
                description="List skill directories under an allowed skill root.",
                allowed_arguments=frozenset({"uri", "limit"}),
                required_arguments=frozenset({"uri"}),
                kind="skill_ls",
                executor=self._eval_ls_skills,
            ),
        }

    async def _eval_read_skill(self, ctx, arguments: Dict[str, Any]) -> Any:
        uri = arguments.get("uri")
        offset = arguments.get("offset", 0)
        limit = arguments.get("limit", -1)
        if not isinstance(uri, str) or not uri.endswith("/SKILL.md"):
            return {"error": "read requires an allowed SKILL.md URI"}
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            return {"error": "offset must be a non-negative integer"}
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < -1:
            return {"error": "limit must be -1 or a non-negative integer"}
        if ctx.allowed_memory_read_uris is not None and uri not in ctx.allowed_memory_read_uris:
            return {"error": f"URI not allowed: {uri}"}
        try:
            raw_content = await self._viking_fs.read_file(uri, ctx=self._ctx)
        except NotFoundError as exc:
            logger.info("Session skill read not found: %s", uri)
            return {"error": str(exc)}
        except Exception as exc:
            logger.warning("Session skill read failed for %s: %s", uri, exc)
            return {"error": str(exc)}

        result, stored = build_skill_read_result(
            raw_content,
            uri=uri,
            offset=offset,
            limit=limit,
        )
        self._read_file_contents[uri] = stored
        return result

    async def _eval_ls_skills(self, ctx, arguments: Dict[str, Any]) -> Any:
        uri_arg = arguments.get("uri")
        limit = arguments.get("limit", 100)
        uris = uri_arg if isinstance(uri_arg, list) else [uri_arg]
        allowed_roots = set(self._skill_roots())
        if not uris or any(not isinstance(uri, str) or uri not in allowed_roots for uri in uris):
            return {"error": f"URI not allowed: {uri_arg}"}
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
            return {"error": "ls limit must be an integer between 1 and 200"}
        listed_skills = []
        seen_names: set[str] = set()
        for uri in uris:
            try:
                entries = await self._viking_fs.ls(
                    uri,
                    output="agent",
                    abs_limit=256,
                    show_all_hidden=False,
                    node_limit=limit,
                    ctx=self._ctx,
                )
            except Exception as exc:
                logger.warning("Session skill list failed for %s: %s", uri, exc)
                return {"error": str(exc)}
            for entry in entries:
                if not entry.get("isDir", False):
                    continue
                skill_root = entry.get("uri") or f"{uri.rstrip('/')}/{entry.get('name', '')}"
                skill_name = entry.get("name") or skill_root.rstrip("/").split("/")[-1]
                if skill_name in seen_names:
                    continue
                seen_names.add(skill_name)
                skill_uri = f"{skill_root.rstrip('/')}/SKILL.md"
                listed_skills.append(
                    {
                        "skill_name": skill_name,
                        "uri": skill_uri,
                        "abstract": entry.get("abstract", ""),
                    }
                )
                self._allowed_memory_read_uris.add(skill_uri)
        return listed_skills

    async def prefetch(self) -> List[Dict[str, Any]]:
        pre_fetch_messages = [self._build_conversation_message()]
        if not self._ctx or not self._viking_fs:
            return pre_fetch_messages

        skill_roots = self._skill_roots()
        code = f"skills = sdk.ls(uri={skill_roots!r})"
        tool_result = await self._eval_memory(code)

        add_tool_call_pair_to_messages(
            messages=pre_fetch_messages,
            call_id=None,
            tool_name="eval",
            params={"code": code},
            result=tool_result,
        )
        return pre_fetch_messages

    async def execute_tool(self, tool_call) -> Any:
        return await super().execute_tool(tool_call)

    def get_tools(self) -> List[str]:
        return ["eval"]

    def _get_registry(self) -> MemoryTypeRegistry:
        if self._registry is None:
            self._registry = load_skill_extract_registry()
        return self._registry
