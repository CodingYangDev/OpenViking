# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""
Memory tools - encapsulate VikingFS read operations for ReAct loop.

Reference: bot/vikingbot/agent/tools/base.py design pattern
"""

import ast
import asyncio
import json
import keyword
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Union
from uuid import uuid4

from openviking.session.memory.utils import add_line_numbers, line_count, slice_content_lines
from openviking.session.memory.utils.memory_file_utils import MemoryFileUtils
from openviking.telemetry import tracer
from openviking.utils.token_estimation import estimate_text_tokens
from openviking_cli.exceptions import NotFoundError
from openviking_cli.utils import get_logger

if TYPE_CHECKING:
    from openviking.server.identity import ToolContext

logger = get_logger(__name__)

_LLM_HIDDEN_MEMORY_FIELDS = {
    "source_extraction_id",
    "source_extraction_ids",
    "last_update_trace_id",
}
_MAX_MEMORY_EVAL_CODE_CHARS = 12_000


@dataclass(frozen=True)
class MemoryEvalFunction:
    """One callable exposed inside the restricted memory eval DSL."""

    name: str
    signature: str
    description: str
    allowed_arguments: frozenset[str]
    required_arguments: frozenset[str]
    kind: str
    memory_type: Optional[str] = None
    executor: Any = None


def memory_eval_function_name(memory_type: str) -> str:
    """Return a stable Python identifier for a schema-backed read function."""
    alias = re.sub(r"\W", "_", memory_type, flags=re.UNICODE).strip("_")
    if not alias:
        raise ValueError("memory_type cannot be converted to a function name")
    if alias[0].isdigit():
        alias = f"type_{alias}"
    return f"read_{alias}"


class MemoryEvalError(ValueError):
    """The model-provided memory eval program violates the restricted DSL."""


def memory_maintenance_notice(
    content: str, *, review_after_tokens: int | None = None
) -> Optional[Dict[str, Any]]:
    """Return maintenance metadata only when a memory exceeds the soft size threshold."""
    if review_after_tokens is None:
        from openviking_cli.utils.config import get_openviking_config

        review_after_tokens = get_openviking_config().memory.maintenance_review_tokens
    estimated_tokens = estimate_text_tokens(content)
    if estimated_tokens <= review_after_tokens:
        return None
    guidance = (
        f"This memory is estimated at {estimated_tokens:,} tokens, above the "
        f"{review_after_tokens:,}-token readability target, so maintenance is required: do not "
        "leave it as one broad oversized memory. Preserve every distinct valid fact exactly once. "
        "For a topic-keyed collection such as preferences, split different behavioral choice "
        "dimensions into focused same-type memories. For an identity-keyed collection such as "
        "entities, never invent multiple identities for one object; split only if distinct "
        "identities were mixed. Compact only duplicate wording, never concrete facts. Aim for the "
        f"{review_after_tokens:,}-token target, but fact integrity takes priority. Delete or replace "
        "the oversized source only after every fact has one clear destination."
    )
    return {
        "maintenance_required": True,
        "guidance": guidance,
    }


def optimize_search_result(result: Any, limit: int = 10) -> Any:
    """优化搜索结果以减少 Token 消耗，并过滤掉抽象文件。"""
    if isinstance(result, dict) and "error" in result:
        return {"error": extract_error_summary(result["error"])}
    if isinstance(result, dict) and "memories" in result:
        filtered = [
            item
            for item in result["memories"]
            if not (
                item.get("uri", "").endswith(".abstract.md")
                or item.get("uri", "").endswith(".overview.md")
            )
        ]
        return [{"uri": item["uri"], "score": item["score"]} for item in filtered[:limit]]
    return []


def extract_error_summary(error: str) -> str:
    if "File not found" in error:
        return "File not found"
    if "Permission denied" in error:
        return "Permission denied"
    if "Timeout" in error:
        return "Timeout"
    return error[:50]


def add_tool_call_pair_to_messages(
    messages: List[Dict[str, Any]],
    call_id: Optional[Union[str, int]],
    tool_name: str,
    params: Dict[str, Any],
    result: Any,
) -> None:
    """Append a native tool request and its JSON result; generate IDs for prefetch."""
    call_id = str(call_id) if call_id is not None else f"call_memory_{uuid4().hex}"
    messages.extend(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": tool_name,
                            "arguments": json.dumps(params, ensure_ascii=False, default=str),
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": json.dumps(result, ensure_ascii=False, default=str),
            },
        ]
    )


class MemoryTool(ABC):
    """
    Abstract base class for memory tools.

    Similar to bot/vikingbot/agent/tools/base.py Tool,
    but simplified for memory module's internal use.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Tool name used in function calls."""
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        """Description of what the tool does."""
        pass

    @property
    @abstractmethod
    def parameters(self) -> Dict[str, Any]:
        """JSON Schema for tool parameters."""
        pass

    @abstractmethod
    async def execute(
        self,
        ctx: Optional["ToolContext"],
        **kwargs: Any,
    ) -> Any:
        """
        Execute the tool with given parameters.

        Args:
            ctx: Tool context (contains viking_fs)
            **kwargs: Tool-specific parameters

        Returns:
            Result of the tool execution (can be dict, list, string, etc.)
        """
        pass

    def to_schema(self) -> Dict[str, Any]:
        """Convert tool to OpenAI function schema format."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class MemoryReadTool(MemoryTool):
    """Tool to read single memory file."""

    @property
    def name(self) -> str:
        return "read"

    @property
    def description(self) -> str:
        return "Read single file"

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "uri": {
                    "type": "string",
                    "description": (
                        "Memory URI to read. e.g., current user: 'viking://~/memories/profile.md'"
                    ),
                },
                "offset": {
                    "type": "integer",
                    "description": "Starting line number to read from (0-indexed)",
                    "default": 0,
                    "minimum": 0,
                },
                "limit": {
                    "type": "integer",
                    "description": "Number of lines to read. -1 means read to end",
                    "default": -1,
                    "minimum": -1,
                },
            },
            "required": ["uri"],
        }

    async def execute(
        self,
        ctx: Optional["ToolContext"],
        **kwargs: Any,
    ) -> Any:
        uri = kwargs.get("uri", "")
        offset = kwargs.get("offset", 0)
        limit = kwargs.get("limit", -1)
        try:
            content = await ctx.viking_fs.read_file(
                uri,
                ctx=ctx.request_ctx,
            )
            # Parse MEMORY_FIELDS from comment and return dict directly
            mf = MemoryFileUtils.read(content, uri=uri)
            ctx.read_file_contents[uri] = mf
            # Remove links/backlinks from LLM-visible output (not needed for extraction)
            llm_result = mf.to_metadata()
            llm_result.pop("links", None)
            llm_result.pop("backlinks", None)
            for hidden_field in _LLM_HIDDEN_MEMORY_FIELDS:
                llm_result.pop(hidden_field, None)
            # Annotate with page_id for link extraction
            if ctx and ctx.page_id_map:
                page_id = ctx.page_id_map.get_page_id(uri)
                if page_id is not None:
                    llm_result["page_id"] = page_id
            plain_content = mf.plain_content() or ""
            maintenance_notice = memory_maintenance_notice(plain_content)
            if maintenance_notice is not None:
                llm_result["memory_maintenance_notice"] = maintenance_notice
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
            return llm_result
        except NotFoundError as e:
            return {"error": str(e)}
        except Exception as e:
            tracer.error(f"Failed to execute read: {e}")
            return {"error": str(e)}


class MemoryReadByFieldsTool(MemoryTool):
    """Read a memory file whose URI is derived from a memory schema."""

    @property
    def name(self) -> str:
        return "read_by_fields"

    @property
    def description(self) -> str:
        return "Read a memory file by memory type and URI identity fields"

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "memory_type": {
                    "type": "string",
                    "description": "Enabled memory schema name",
                },
                "fields": {
                    "type": "object",
                    "description": (
                        "Only the fields referenced by the schema directory or filename template"
                    ),
                    "additionalProperties": True,
                },
                "offset": {
                    "type": "integer",
                    "description": "Starting line number to read from (0-indexed)",
                    "default": 0,
                    "minimum": 0,
                },
                "limit": {
                    "type": "integer",
                    "description": "Number of lines to read. -1 means read to end",
                    "default": -1,
                    "minimum": -1,
                },
            },
            "required": ["memory_type", "fields"],
        }

    async def execute(
        self,
        ctx: Optional["ToolContext"],
        **kwargs: Any,
    ) -> Any:
        memory_type = str(kwargs.get("memory_type") or "").strip()
        fields = kwargs.get("fields")
        if not memory_type:
            return {"error": "memory_type must be non-empty"}
        if not isinstance(fields, dict):
            return {"error": "fields must be an object"}
        if ctx is None or ctx.memory_uri_resolver is None:
            return {"error": "Schema-based memory URI resolution is unavailable"}

        try:
            uri = ctx.memory_uri_resolver(memory_type, fields)
        except Exception as e:
            return {"error": str(e)}

        return await MemoryReadTool().execute(
            ctx,
            uri=uri,
            offset=kwargs.get("offset", 0),
            limit=kwargs.get("limit", -1),
        )


class MemorySearchTool(MemoryTool):
    """Tool to perform semantic search."""

    @property
    def name(self) -> str:
        return "search"

    @property
    def description(self) -> str:
        return "Semantic search with session context"

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query text",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum results to return, default 10",
                    "default": 10,
                },
            },
            "required": ["query"],
        }

    async def execute(
        self,
        ctx: Optional["ToolContext"],
        **kwargs: Any,
    ) -> Any:
        try:
            query = kwargs.get("query", "")
            # Get target_uri from ctx.default_search_uris
            target_uri = ""
            if ctx.default_search_uris:
                target_uri = ctx.default_search_uris
            limit = kwargs.get("limit", 10)
            request_ctx = ctx.request_ctx if ctx else None
            # 多搜索 10 个，过滤抽象文件后再截断
            search_result = await ctx.viking_fs.search(
                query,
                target_uri=target_uri,
                limit=limit + 10,
                ctx=request_ctx,
            )
            return optimize_search_result(search_result.to_dict(), limit=limit)
        except Exception as e:
            tracer.error(f"Failed to execute search: {e}")
            return {"error": str(e)}


def _format_size(size_bytes: int) -> str:
    """Format size in bytes to human readable format."""
    if size_bytes >= 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f}M"
    elif size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f}K"
    else:
        return f"{size_bytes}B"


class MemoryLsTool(MemoryTool):
    """Tool to list directory contents."""

    @property
    def name(self) -> str:
        return "ls"

    @property
    def description(self) -> str:
        return "List directory content, includes abstract field when output='agent'"

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "uri": {
                    "type": "string",
                    "description": (
                        "Directory URI to list. e.g., current user: 'viking://~/memories'"
                    ),
                },
            },
            "required": ["uri"],
        }

    async def execute(
        self,
        ctx: Optional["ToolContext"],
        **kwargs: Any,
    ) -> Any:
        try:
            uri = kwargs.get("uri", "")
            entries = await ctx.viking_fs.ls(
                uri,
                output="agent",
                abs_limit=256,
                show_all_hidden=False,
                node_limit=1000,
                ctx=ctx.request_ctx,
            )
            # Format: filename size (e.g., "file.md 1.2K")
            result_lines = []
            for e in entries:
                if not e.get("isDir", False):
                    # Extract name from entry or fallback to uri
                    name = e.get("name", "")
                    if not name:
                        uri = e.get("uri", "")
                        name = uri.rsplit("/", 1)[-1] if "/" in uri else uri
                    size = e.get("size", 0)
                    result_lines.append(f"{name} {_format_size(size)}")
            if not result_lines:
                return "Directory is empty. You can write new files to create memory content."
            return "\n".join(result_lines)
        except Exception as e:
            tracer.info(f"Failed to execute ls: {e}")
            return {"error": str(e)}


class MemoryEvalTool(MemoryTool):
    """Execute a small read-only memory DSL without running Python code."""

    @property
    def name(self) -> str:
        return "eval"

    @property
    def description(self) -> str:
        return self.describe_functions({})

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Restricted read-only memory DSL program",
                }
            },
            "required": ["code"],
        }

    @staticmethod
    def describe_functions(functions: Dict[str, MemoryEvalFunction]) -> str:
        lines = [
            "Execute a restricted read-only memory DSL. This is not a Python runtime.",
            "Assignments only: name = sdk.function(keyword=value, ...), name = existing, "
            "or name = existing[index]. Existing variables include earlier tool results. "
            "Return assigned variables only; no bare calls.",
            "Only literals, existing variables, and list/dict subscripts are allowed as arguments.",
        ]
        if functions:
            lines.append("Available functions:")
            lines.extend(
                f"- sdk.{spec.signature}: {spec.description}" for spec in functions.values()
            )
            if "search_by_session" in functions:
                lines.append("Example: matches = sdk.search_by_session(limit=5)")
        return "\n".join(lines)

    @tracer("memory.tool.eval")
    async def execute(
        self,
        ctx: Optional["ToolContext"],
        **kwargs: Any,
    ) -> Any:
        tracer.info(
            "input="
            + json.dumps(
                {"code": kwargs.get("code")},
                ensure_ascii=False,
                default=str,
                separators=(",", ":"),
            )
        )
        result = await self._execute_restricted(ctx, **kwargs)
        tracer.info(
            "output=" + json.dumps(result, ensure_ascii=False, default=str, separators=(",", ":"))
        )
        return result

    async def _execute_restricted(
        self,
        ctx: Optional["ToolContext"],
        **kwargs: Any,
    ) -> Any:
        code = kwargs.get("code")
        if not isinstance(code, str) or not code.strip():
            return {"error": "code must be a non-empty string"}
        if len(code) > _MAX_MEMORY_EVAL_CODE_CHARS:
            return {
                "error": (
                    f"code exceeds the {_MAX_MEMORY_EVAL_CODE_CHARS:,}-character memory eval limit"
                )
            }
        functions = ctx.memory_eval_functions if ctx is not None else None
        if not isinstance(functions, dict) or not functions:
            return {"error": "Memory eval functions are unavailable"}

        try:
            values = dict(ctx.memory_eval_bindings or {})
            if any(
                not isinstance(name, str)
                or not name.isidentifier()
                or keyword.iskeyword(name)
                or name in {"sdk", "error"}
                or name.startswith("_")
                for name in values
            ):
                raise MemoryEvalError("Invalid existing memory eval variable name")
            program = self._parse_program(code, functions, set(values))
            assigned: Dict[str, Any] = {}
            for statement in program:
                name = statement.targets[0].id
                expression = statement.value
                if isinstance(expression, ast.Call):
                    function = functions[expression.func.attr]
                    call_kwargs = {
                        item.arg: self._eval_value(item.value, values)
                        for item in expression.keywords
                    }
                    value = await self._execute_function(ctx, function, call_kwargs)
                else:
                    value = self._eval_value(expression, values)
                values[name] = assigned[name] = value
            return assigned
        except MemoryEvalError as e:
            return {"error": str(e)}
        except Exception as e:
            tracer.error(f"Failed to execute memory eval: {e}")
            return {"error": f"Memory eval failed: {e}"}

    def _parse_program(
        self,
        code: str,
        functions: Dict[str, MemoryEvalFunction],
        existing_names: Optional[set[str]] = None,
    ) -> List[ast.Assign]:
        try:
            module = ast.parse(code, mode="exec")
        except SyntaxError as e:
            raise MemoryEvalError(
                f"Line {e.lineno or 1}: invalid memory eval syntax: {e.msg}"
            ) from e
        if not module.body:
            raise MemoryEvalError("Memory eval program must contain at least one assignment")

        known_names = set(existing_names or ())
        assigned_names: set[str] = set()
        program = []
        for statement in module.body:
            assignment_error = (
                "only `name = sdk.allowed_function(keyword=value, ...)` "
                "or assignments from existing result variables/subscripts are allowed"
            )
            if (
                not isinstance(statement, ast.Assign)
                or len(statement.targets) != 1
                or not isinstance(statement.targets[0], ast.Name)
            ):
                self._error(statement, assignment_error)
            target = statement.targets[0].id
            if (
                target in {"sdk", "error"}
                or target.startswith("_")
                or target in assigned_names
                or target in functions
            ):
                self._error(statement, f"invalid or duplicate result variable: {target!r}")

            if isinstance(statement.value, (ast.Name, ast.Subscript)):
                self._validate_value(statement.value, known_names)
                program.append(statement)
                assigned_names.add(target)
                known_names.add(target)
                continue

            call = statement.value
            if not (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "sdk"
            ):
                self._error(statement, assignment_error)
            function_name = call.func.attr
            qualified_name = f"sdk.{function_name}"
            function = functions.get(function_name)
            if function is None or function_name.startswith("_"):
                self._error(call, f"function {qualified_name!r} is not allowed")
            if call.args:
                self._error(call, "memory eval functions accept keyword arguments only")
            if any(keyword.arg is None for keyword in call.keywords):
                self._error(call, "**kwargs is not allowed")

            keyword_nodes: Dict[str, ast.AST] = {}
            for keyword_arg in call.keywords:
                if keyword_arg.arg in keyword_nodes:
                    self._error(call, f"duplicate argument: {keyword_arg.arg}")
                keyword_nodes[keyword_arg.arg] = keyword_arg.value
                self._validate_value(keyword_arg.value, known_names)

            unknown = sorted(set(keyword_nodes) - function.allowed_arguments)
            if unknown:
                self._error(
                    call,
                    f"unsupported arguments for {qualified_name}: {', '.join(unknown)}",
                )
            missing = sorted(function.required_arguments - set(keyword_nodes))
            if missing:
                self._error(
                    call,
                    f"missing required arguments for {qualified_name}: {', '.join(missing)}",
                )

            program.append(statement)
            assigned_names.add(target)
            known_names.add(target)
        return program

    def _validate_value(self, node: ast.AST, known_names: set[str]) -> None:
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (str, int, float, bool)) or node.value is None:
                return
            self._error(node, f"unsupported literal type: {type(node.value).__name__}")
        if isinstance(node, ast.Name):
            if node.id not in known_names:
                self._error(node, f"unknown result variable: {node.id!r}")
            return
        if isinstance(node, (ast.List, ast.Tuple)):
            for item in node.elts:
                self._validate_value(item, known_names)
            return
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if key is None:
                    self._error(node, "dictionary unpacking is not allowed")
                self._validate_value(key, known_names)
                self._validate_value(value, known_names)
            return
        if isinstance(node, ast.Subscript):
            self._validate_value(node.value, known_names)
            self._validate_value(node.slice, known_names)
            return
        if (
            isinstance(node, ast.UnaryOp)
            and isinstance(node.op, (ast.UAdd, ast.USub))
            and isinstance(node.operand, ast.Constant)
            and isinstance(node.operand.value, (int, float))
            and not isinstance(node.operand.value, bool)
        ):
            return
        self._error(node, f"unsupported argument expression: {type(node).__name__}")

    def _eval_value(self, node: ast.AST, values: Dict[str, Any]) -> Any:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return values[node.id]
        if isinstance(node, ast.List):
            return [self._eval_value(item, values) for item in node.elts]
        if isinstance(node, ast.Tuple):
            return tuple(self._eval_value(item, values) for item in node.elts)
        if isinstance(node, ast.Dict):
            return {
                self._eval_value(key, values): self._eval_value(value, values)
                for key, value in zip(node.keys, node.values, strict=True)
            }
        if isinstance(node, ast.Subscript):
            container = self._eval_value(node.value, values)
            index = self._eval_value(node.slice, values)
            if type(container) not in (list, dict):
                self._error(node, "subscripts require a list or dict result")
            if type(container) is list and type(index) is not int:
                self._error(node, "list result indices must be integers")
            if type(index) not in (str, int, float, bool, type(None)):
                self._error(node, "dict result keys must be scalar values")
            try:
                return container[index]
            except (IndexError, KeyError, TypeError) as e:
                self._error(node, f"cannot resolve subscript: {e}")
        if isinstance(node, ast.UnaryOp):
            value = node.operand.value
            return -value if isinstance(node.op, ast.USub) else value
        self._error(node, f"unsupported argument expression: {type(node).__name__}")

    async def _execute_function(
        self,
        ctx: "ToolContext",
        function: MemoryEvalFunction,
        kwargs: Dict[str, Any],
    ) -> Any:
        if function.executor is not None:
            return await function.executor(ctx, kwargs)
        if function.kind == "read":
            self._validate_read_arguments(kwargs)
            if (
                ctx.allowed_memory_read_uris is not None
                and kwargs["uri"] not in ctx.allowed_memory_read_uris
            ):
                return {"error": f"URI not allowed: {kwargs['uri']}"}
            return await MemoryReadTool().execute(ctx, **kwargs)
        if function.kind == "schema_read":
            offset = kwargs.pop("offset", 0)
            limit = kwargs.pop("limit", -1)
            self._validate_read_arguments({"uri": "schema", "offset": offset, "limit": limit})
            return await MemoryReadByFieldsTool().execute(
                ctx,
                memory_type=function.memory_type,
                fields=kwargs,
                offset=offset,
                limit=limit,
            )
        if function.kind == "search":
            return await self.search_and_read(ctx, **kwargs)
        if function.kind == "ls":
            return await self._structured_ls(ctx, kwargs)
        return {"error": f"Unsupported memory eval function: {function.name}"}

    @staticmethod
    def _validate_read_arguments(kwargs: Dict[str, Any]) -> None:
        uri = kwargs.get("uri")
        offset = kwargs.get("offset", 0)
        limit = kwargs.get("limit", -1)
        if not isinstance(uri, str) or not uri.strip():
            raise MemoryEvalError("uri must be a non-empty string")
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise MemoryEvalError("offset must be a non-negative integer")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < -1:
            raise MemoryEvalError("limit must be -1 or a non-negative integer")

    async def search_and_read(self, ctx: "ToolContext", *, query: str, limit: int = 10) -> Any:
        """Search within the allowed scope and load each hit through the shared read path."""
        if not isinstance(query, str) or not query.strip():
            raise MemoryEvalError("query must be a non-empty string")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10:
            raise MemoryEvalError("search limit must be an integer between 1 and 10")

        hits = await MemorySearchTool().execute(ctx, query=query, limit=limit)
        if not isinstance(hits, list):
            return hits
        if ctx.allowed_memory_read_uris is not None:
            ctx.allowed_memory_read_uris.update(
                hit["uri"] for hit in hits if isinstance(hit.get("uri"), str)
            )

        async def load(hit: Dict[str, Any]) -> Dict[str, Any]:
            uri = hit.get("uri")
            result = await MemoryReadTool().execute(ctx, uri=uri)
            if isinstance(result, dict):
                return {**result, **hit}
            return {**hit, "result": result}

        return await asyncio.gather(*(load(hit) for hit in hits))

    async def _structured_ls(self, ctx: "ToolContext", kwargs: Dict[str, Any]) -> Any:
        uri = kwargs.get("uri")
        limit = kwargs.get("limit", 100)
        if not isinstance(uri, str) or not uri.strip():
            raise MemoryEvalError("uri must be a non-empty string")
        allowed_directories = {item.rstrip("/") for item in ctx.default_search_uris or []}
        if allowed_directories and uri.rstrip("/") not in allowed_directories:
            return {"error": f"URI not allowed: {uri}"}
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
            raise MemoryEvalError("ls limit must be an integer between 1 and 200")
        try:
            entries = await ctx.viking_fs.ls(
                uri,
                output="agent",
                abs_limit=256,
                show_all_hidden=False,
                node_limit=limit,
                ctx=ctx.request_ctx,
            )
            result = []
            for entry in entries:
                entry_uri = entry.get("uri")
                name = entry.get("name") or (
                    entry_uri.rsplit("/", 1)[-1] if isinstance(entry_uri, str) else ""
                )
                result.append(
                    {
                        "uri": entry_uri,
                        "name": name,
                        "is_dir": bool(entry.get("isDir", False)),
                        "size": entry.get("size", 0),
                    }
                )
            if ctx.allowed_memory_read_uris is not None:
                ctx.allowed_memory_read_uris.update(
                    item["uri"] for item in result if isinstance(item.get("uri"), str)
                )
            return result
        except Exception as e:
            tracer.info(f"Failed to execute eval ls: {e}")
            return {"error": str(e)}

    @staticmethod
    def _error(node: ast.AST, message: str) -> None:
        raise MemoryEvalError(f"Line {getattr(node, 'lineno', 1)}: {message}")


# Tool registry
MEMORY_TOOLS_REGISTRY: Dict[str, MemoryTool] = {}


def register_tool(tool: MemoryTool) -> None:
    """Register a memory tool."""
    MEMORY_TOOLS_REGISTRY[tool.name] = tool


def get_tool(name: str) -> Optional[MemoryTool]:
    """Get a memory tool by name."""
    return MEMORY_TOOLS_REGISTRY.get(name)


# Register default tools
register_tool(MemoryReadTool())
register_tool(MemoryReadByFieldsTool())
register_tool(MemorySearchTool())
register_tool(MemoryLsTool())
register_tool(MemoryEvalTool())
register_tool(MemoryEvalTool())
