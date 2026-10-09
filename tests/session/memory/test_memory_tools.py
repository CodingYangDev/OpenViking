# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""
Tests for memory tools.
"""

import json
from unittest.mock import AsyncMock

import pytest

from openviking.server.identity import RequestContext, Role, ToolContext
from openviking.session.memory.dataclass import MemoryFile
from openviking.session.memory.tools import (
    MEMORY_TOOLS_REGISTRY,
    MemoryEvalFunction,
    MemoryEvalTool,
    MemoryLsTool,
    MemoryReadByFieldsTool,
    MemoryReadTool,
    MemorySearchTool,
    add_tool_call_pair_to_messages,
    get_tool,
    memory_maintenance_notice,
)
from openviking_cli.session.user_id import UserIdentifier


def test_native_tool_pairs_preserve_json_and_generate_unique_prefetch_ids():
    messages = []
    params = {"code": "first = sdk.search_by_session(limit=5)"}
    result = {"first": [], "value": None, "enabled": True}
    for _ in range(2):
        add_tool_call_pair_to_messages(messages, None, "eval", params, result)

    assert [m["role"] for m in messages] == ["assistant", "tool", "assistant", "tool"]
    ids = []
    for index in (0, 2):
        request, response = messages[index : index + 2]
        call = request["tool_calls"][0]
        ids.append(call["id"])
        assert call["type"] == "function"
        assert call["function"]["name"] == "eval"
        assert json.loads(call["function"]["arguments"]) == params
        assert response["tool_call_id"] == call["id"]
        assert json.loads(response["content"]) == result
    assert ids[0] != ids[1]


class TestMemoryTools:
    """Tests for memory tools."""

    def test_read_tool_properties(self):
        """Test MemoryReadTool properties."""
        tool = MemoryReadTool()

        assert tool.name == "read"
        assert "Read single file" in tool.description
        assert "uri" in tool.parameters["properties"]
        assert "offset" in tool.parameters["properties"]
        assert "limit" in tool.parameters["properties"]
        assert "required" in tool.parameters

    def test_read_by_fields_tool_properties(self):
        tool = MemoryReadByFieldsTool()

        assert tool.name == "read_by_fields"
        assert "memory_type" in tool.parameters["properties"]
        assert "fields" in tool.parameters["properties"]
        assert tool.parameters["required"] == ["memory_type", "fields"]

    def test_eval_tool_properties(self):
        tool = MemoryEvalTool()

        assert tool.name == "eval"
        assert tool.parameters["required"] == ["code"]
        assert "not a Python runtime" in tool.description

    @pytest.mark.asyncio
    async def test_eval_executes_schema_read_and_returns_assigned_result(self):
        class MockVikingFS:
            async def read_file(self, uri, ctx=None, **kwargs):
                return (
                    "- keep the close-up restrained\n\n"
                    '<!-- MEMORY_FIELDS\n{"memory_type":"ss_clip","clip_id":"clip_1"}\n-->'
                )

        def resolve(memory_type, fields):
            assert memory_type == "ss_clip"
            assert fields == {
                "project_id": "proj_1",
                "plot_id": "plot_1",
                "clip_id": "clip_1",
            }
            return "viking://user/default/memories/ss/projects/proj_1/plots/plot_1/clips/clip_1/clip.md"

        function = MemoryEvalFunction(
            name="read_ss_clip",
            signature="read_ss_clip(project_id: str, plot_id: str, clip_id: str)",
            description="Read one ss_clip memory.",
            allowed_arguments=frozenset({"project_id", "plot_id", "clip_id", "offset", "limit"}),
            required_arguments=frozenset({"project_id", "plot_id", "clip_id"}),
            kind="schema_read",
            memory_type="ss_clip",
        )
        tool_ctx = ToolContext(
            viking_fs=MockVikingFS(),
            request_ctx=RequestContext(user=UserIdentifier.the_default_user(), role=Role.USER),
            read_file_contents={},
            memory_uri_resolver=resolve,
            memory_eval_functions={function.name: function},
        )

        result = await MemoryEvalTool().execute(
            tool_ctx,
            code=('a = sdk.read_ss_clip(project_id="proj_1", plot_id="plot_1", clip_id="clip_1")'),
        )

        assert result["a"]["memory_type"] == "ss_clip"
        assert result["a"]["content"] == "1\t- keep the close-up restrained"

    @pytest.mark.asyncio
    async def test_eval_search_returns_read_contents(self):
        class MockSearchResult:
            def to_dict(self):
                return {
                    "memories": [{"uri": "viking://user/default/memories/ss/user.md", "score": 0.9}]
                }

        class MockVikingFS:
            async def search(self, *args, **kwargs):
                return MockSearchResult()

            async def read_file(self, uri, ctx=None, **kwargs):
                return '- concise replies\n\n<!-- MEMORY_FIELDS\n{"memory_type":"ss_user"}\n-->'

        function = MemoryEvalFunction(
            name="search",
            signature="search(query: str, limit: int = 10)",
            description="Search and read memories.",
            allowed_arguments=frozenset({"query", "limit"}),
            required_arguments=frozenset({"query"}),
            kind="search",
        )
        tool_ctx = ToolContext(
            viking_fs=MockVikingFS(),
            request_ctx=RequestContext(user=UserIdentifier.the_default_user(), role=Role.USER),
            read_file_contents={},
            memory_eval_functions={function.name: function},
            allowed_memory_read_uris=set(),
        )

        result = await MemoryEvalTool().execute(
            tool_ctx,
            code='hits = sdk.search(query="concise", limit=1)',
        )

        assert result["hits"] == [
            {
                "memory_type": "ss_user",
                "version": 1,
                "content": "1\t- concise replies",
                "uri": "viking://user/default/memories/ss/user.md",
                "score": 0.9,
            }
        ]

    @pytest.mark.asyncio
    async def test_eval_can_read_uri_returned_by_ls(self):
        memory_uri = "viking://user/default/memories/ss/user.md"

        class MockVikingFS:
            async def ls(self, *args, **kwargs):
                return [{"uri": memory_uri, "name": "user.md", "isDir": False, "size": 12}]

            async def read_file(self, uri, ctx=None, **kwargs):
                assert uri == memory_uri
                return '- concise replies\n\n<!-- MEMORY_FIELDS\n{"memory_type":"ss_user"}\n-->'

        functions = {
            "ls": MemoryEvalFunction(
                name="ls",
                signature="ls(uri: str, limit: int = 100)",
                description="List memory files.",
                allowed_arguments=frozenset({"uri", "limit"}),
                required_arguments=frozenset({"uri"}),
                kind="ls",
            ),
            "read": MemoryEvalFunction(
                name="read",
                signature="read(uri: str)",
                description="Read a memory.",
                allowed_arguments=frozenset({"uri", "offset", "limit"}),
                required_arguments=frozenset({"uri"}),
                kind="read",
            ),
        }
        tool_ctx = ToolContext(
            viking_fs=MockVikingFS(),
            request_ctx=RequestContext(user=UserIdentifier.the_default_user(), role=Role.USER),
            read_file_contents={},
            memory_eval_functions=functions,
            allowed_memory_read_uris=set(),
        )

        result = await MemoryEvalTool().execute(
            tool_ctx,
            code=(
                'files = sdk.ls(uri="viking://user/default/memories/ss")\n'
                'memory = sdk.read(uri=files[0]["uri"])'
            ),
        )

        assert result["files"] == [
            {"uri": memory_uri, "name": "user.md", "is_dir": False, "size": 12}
        ]
        assert result["memory"]["content"] == "1\t- concise replies"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "code",
        [
            'sdk.read(uri="viking://user/default/memories/test.md")',
            'a = read(uri="viking://user/default/memories/test.md")',
            'a = other.read(uri="viking://user/default/memories/test.md")',
            'a = sdk.nested.read(uri="viking://user/default/memories/test.md")',
            'a = sdk.__getattribute__("read")',
            "a = sdk.__dict__",
            'sdk = sdk.read(uri="viking://user/default/memories/test.md")',
            'a = sdk.create_ss_plot(project_id="p", plot_id="s", content="x")',
            'a = sdk.read("viking://user/default/memories/test.md")',
            'a = sdk.read(uri="viking://user/default/memories/test.md")\n'
            "b = sdk.read(uri=a.__class__)",
        ],
    )
    async def test_eval_requires_assignment_and_exact_read_only_sdk_namespace(self, code):
        executor = AsyncMock(return_value={"content": "read should not run"})
        function = MemoryEvalFunction(
            name="read",
            signature="read(uri: str)",
            description="Read a memory.",
            allowed_arguments=frozenset({"uri"}),
            required_arguments=frozenset({"uri"}),
            kind="read",
            executor=executor,
        )
        context = ToolContext(
            viking_fs=None,
            request_ctx=RequestContext(user=UserIdentifier.the_default_user(), role=Role.USER),
            memory_eval_functions={"read": function},
        )

        result = await MemoryEvalTool().execute(context, code=code)

        assert "error" in result
        executor.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("statement_count", [21, 22, 256])
    async def test_eval_statement_count_is_unbounded(self, statement_count):
        async def read(ctx, arguments):
            del ctx
            return {"uri": arguments["uri"], "content": "a fact"}

        executor = AsyncMock(side_effect=read)
        function = MemoryEvalFunction(
            name="read",
            signature="read(uri: str)",
            description="Read a memory.",
            allowed_arguments=frozenset({"uri"}),
            required_arguments=frozenset({"uri"}),
            kind="read",
            executor=executor,
        )
        context = ToolContext(
            viking_fs=None,
            request_ctx=RequestContext(user=UserIdentifier.the_default_user(), role=Role.USER),
            memory_eval_functions={"read": function},
        )
        uri = "viking://user/default/memories/test.md"
        code = "\n".join(
            [f"r0 = sdk.read(uri={uri!r})"]
            + [f"r{i} = sdk.read(uri=r{i - 1}['uri'])" for i in range(1, statement_count)]
        )
        # Statement count is independent of the existing code-size safeguard.
        assert len(code) < 12_000

        result = await MemoryEvalTool().execute(context, code=code)

        assert result == {
            f"r{i}": {"uri": uri, "content": "a fact"} for i in range(statement_count)
        }
        assert list(result) == [f"r{i}" for i in range(statement_count)]
        assert executor.await_count == statement_count

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("tail", "error"),
        [
            ("bad = sdk.write(uri='x')", "function 'sdk.write' is not allowed"),
            ("sdk.read(uri='x')", "only `name = sdk.allowed_function"),
            ("bad = sdk.read(path='x')", "unsupported arguments for sdk.read"),
            ("bad = sdk.read(uri=missing)", "unknown result variable"),
        ],
    )
    async def test_eval_validates_long_program_before_any_read(self, tail, error):
        executor = AsyncMock()
        function = MemoryEvalFunction(
            name="read",
            signature="read(uri: str)",
            description="Read a memory.",
            allowed_arguments=frozenset({"uri"}),
            required_arguments=frozenset({"uri"}),
            kind="read",
            executor=executor,
        )
        context = ToolContext(
            viking_fs=None,
            request_ctx=RequestContext(user=UserIdentifier.the_default_user(), role=Role.USER),
            memory_eval_functions={"read": function},
        )
        prefix = [
            f"r{i} = sdk.read(uri='viking://user/default/memories/test.md')" for i in range(22)
        ]

        result = await MemoryEvalTool().execute(context, code="\n".join([*prefix, tail]))

        assert error in result["error"]
        executor.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_eval_sdk_calls_preserve_explicit_result_names_and_dependencies(self):
        async def read(ctx, arguments):
            del ctx
            return {"uri": arguments["uri"], "content": "a fact"}

        function = MemoryEvalFunction(
            name="read",
            signature="read(uri: str)",
            description="Read a memory.",
            allowed_arguments=frozenset({"uri"}),
            required_arguments=frozenset({"uri"}),
            kind="read",
            executor=read,
        )
        context = ToolContext(
            viking_fs=None,
            request_ctx=RequestContext(user=UserIdentifier.the_default_user(), role=Role.USER),
            memory_eval_functions={"read": function},
        )
        uri = "viking://user/default/memories/test.md"

        result = await MemoryEvalTool().execute(
            context,
            code=f'first = sdk.read(uri="{uri}")\nsecond = sdk.read(uri=first["uri"])',
        )

        assert result == {
            "first": {"uri": uri, "content": "a fact"},
            "second": {"uri": uri, "content": "a fact"},
        }

    @pytest.mark.asyncio
    async def test_eval_rejects_raw_uri_not_returned_by_trusted_context(self):
        function = MemoryEvalFunction(
            name="read",
            signature="read(uri: str)",
            description="Read a memory.",
            allowed_arguments=frozenset({"uri", "offset", "limit"}),
            required_arguments=frozenset({"uri"}),
            kind="read",
        )
        tool_ctx = ToolContext(
            viking_fs=None,
            request_ctx=RequestContext(user=UserIdentifier.the_default_user(), role=Role.USER),
            memory_eval_functions={function.name: function},
            allowed_memory_read_uris=set(),
        )

        result = await MemoryEvalTool().execute(
            tool_ctx,
            code='memory = sdk.read(uri="viking://user/default/memories/ss/guessed.md")',
        )

        assert result == {
            "memory": {"error": "URI not allowed: viking://user/default/memories/ss/guessed.md"}
        }

    @pytest.mark.asyncio
    async def test_eval_rejects_arbitrary_python_before_execution(self):
        function = MemoryEvalFunction(
            name="read",
            signature="read(uri: str)",
            description="Read a memory.",
            allowed_arguments=frozenset({"uri"}),
            required_arguments=frozenset({"uri"}),
            kind="read",
        )
        tool_ctx = ToolContext(
            viking_fs=None,
            request_ctx=RequestContext(user=UserIdentifier.the_default_user(), role=Role.USER),
            memory_eval_functions={function.name: function},
        )

        result = await MemoryEvalTool().execute(
            tool_ctx,
            code="import os\na = sdk.read(uri='viking://user/default/memories/ss/user.md')",
        )

        assert result == {
            "error": "Line 1: only `name = sdk.allowed_function(keyword=value, ...)` "
            "or assignments from existing result variables/subscripts are allowed"
        }

    @pytest.mark.asyncio
    async def test_eval_traces_compact_input_and_output(self, monkeypatch):
        events = []
        monkeypatch.setattr("openviking.session.memory.tools.tracer.info", events.append)
        function = MemoryEvalFunction(
            name="read",
            signature="read(uri: str)",
            description="Read a memory.",
            allowed_arguments=frozenset({"uri"}),
            required_arguments=frozenset({"uri"}),
            kind="read",
        )
        tool_ctx = ToolContext(
            viking_fs=None,
            request_ctx=RequestContext(user=UserIdentifier.the_default_user(), role=Role.USER),
            memory_eval_functions={function.name: function},
            allowed_memory_read_uris=set(),
        )

        result = await MemoryEvalTool().execute(
            tool_ctx,
            code='memory = sdk.read(uri="viking://user/default/memories/ss/unknown.md")',
        )

        assert result["memory"]["error"].startswith("URI not allowed:")
        assert events[0].startswith('input={"code":')
        assert events[-1].startswith('output={"memory":{"error":')

    @pytest.mark.asyncio
    async def test_read_by_fields_resolves_uri_then_reuses_read(self):
        class MockVikingFS:
            def __init__(self):
                self.received_uri = None
                self.received_ctx = None

            async def read_file(self, uri, ctx=None, **kwargs):
                self.received_uri = uri
                self.received_ctx = ctx
                return (
                    "- keep the close-up restrained\n\n"
                    '<!-- MEMORY_FIELDS\n{"memory_type":"ss_clip","clip_id":"clip_1"}\n-->'
                )

        request_ctx = RequestContext(user=UserIdentifier.the_default_user(), role=Role.USER)
        viking_fs = MockVikingFS()
        resolved = []

        def resolve(memory_type, fields):
            resolved.append((memory_type, fields))
            return "viking://user/default/memories/ss/projects/proj_1/plots/plot_1/clips/clip_1/clip.md"

        tool_ctx = ToolContext(
            viking_fs=viking_fs,
            request_ctx=request_ctx,
            read_file_contents={},
            memory_uri_resolver=resolve,
        )

        result = await MemoryReadByFieldsTool().execute(
            tool_ctx,
            memory_type="ss_clip",
            fields={"project_id": "proj_1", "plot_id": "plot_1", "clip_id": "clip_1"},
        )

        assert resolved == [
            (
                "ss_clip",
                {"project_id": "proj_1", "plot_id": "plot_1", "clip_id": "clip_1"},
            )
        ]
        assert viking_fs.received_ctx is request_ctx
        assert viking_fs.received_uri.endswith("/proj_1/plots/plot_1/clips/clip_1/clip.md")
        assert result["memory_type"] == "ss_clip"
        assert result["content"] == "1\t- keep the close-up restrained"
        assert viking_fs.received_uri in tool_ctx.read_file_contents

    @pytest.mark.asyncio
    async def test_read_tool_strips_local_memory_links_from_llm_content(self):
        class MockPageIdMap:
            def get_page_id(self, uri):
                return None

        class MockVikingFS:
            async def read_file(self, uri, ctx=None, **kwargs):
                return (
                    "Gina values [emotional support](../../events/2023/03/23/mutual_business_support.md) "
                    "with Jon.\n\n"
                    "<!-- MEMORY_FIELDS\n"
                    '{"memory_type": "experiences"}\n'
                    "-->"
                )

        tool_ctx = ToolContext(
            viking_fs=MockVikingFS(),
            request_ctx=RequestContext(user=UserIdentifier.the_default_user(), role=Role.USER),
            default_search_uris=[],
            read_file_contents={},
            page_id_map=MockPageIdMap(),
        )

        result = await MemoryReadTool().execute(
            tool_ctx,
            uri="viking://user/default/memories/experiences/test.md",
        )

        assert result["content"] == "1\tGina values emotional support with Jon."

    @pytest.mark.asyncio
    async def test_read_tool_uses_memory_file_plain_content(self, monkeypatch):
        class MockPageIdMap:
            def get_page_id(self, uri):
                return None

        class MockVikingFS:
            async def read_file(self, uri, ctx=None, **kwargs):
                return (
                    "Gina values [emotional support](../../events/2023/03/23/mutual_business_support.md) "
                    "with Jon.\n\n"
                    "<!-- MEMORY_FIELDS\n"
                    '{"memory_type": "experiences"}\n'
                    "-->"
                )

        called = False
        original_plain_content = MemoryFile.plain_content

        def tracking_plain_content(self):
            nonlocal called
            called = True
            return original_plain_content(self)

        monkeypatch.setattr(MemoryFile, "plain_content", tracking_plain_content)

        tool_ctx = ToolContext(
            viking_fs=MockVikingFS(),
            request_ctx=RequestContext(user=UserIdentifier.the_default_user(), role=Role.USER),
            default_search_uris=[],
            read_file_contents={},
            page_id_map=MockPageIdMap(),
        )

        result = await MemoryReadTool().execute(
            tool_ctx,
            uri="viking://user/default/memories/experiences/test.md",
        )

        assert called is True
        assert result["content"] == "1\tGina values emotional support with Jon."
        assert "memory_maintenance_notice" not in result

    def test_memory_maintenance_notice_is_conditional_and_explains_two_strategies(self):
        assert memory_maintenance_notice("abcd", review_after_tokens=1) is None

        notice = memory_maintenance_notice("abcde", review_after_tokens=1)

        assert notice["maintenance_required"] is True
        assert set(notice) == {"maintenance_required", "guidance"}
        guidance = notice["guidance"]
        assert "estimated at 2 tokens" in guidance
        assert "above the 1-token readability target" in guidance
        assert "maintenance is required" in guidance
        assert "do not leave it as one broad oversized memory" in guidance
        assert "split different behavioral choice dimensions" in guidance
        assert "never invent multiple identities for one object" in guidance
        assert "compact only duplicate wording" in guidance.lower()
        assert "fact integrity takes priority" in guidance
        assert "Preserve every distinct valid fact exactly once" in guidance
        assert "only after every fact has one clear destination" in guidance
        assert "Delete or replace the oversized source" in guidance

    @pytest.mark.asyncio
    async def test_read_tool_uses_offset_and_limit_for_visible_content(self):
        class MockPageIdMap:
            def get_page_id(self, uri):
                return 42

        class MockVikingFS:
            async def read_file(self, uri, ctx=None, **kwargs):
                return (
                    'line1\nline2\nline3\n\n<!-- MEMORY_FIELDS\n{"memory_type": "experiences"}\n-->'
                )

        tool_ctx = ToolContext(
            viking_fs=MockVikingFS(),
            request_ctx=RequestContext(user=UserIdentifier.the_default_user(), role=Role.USER),
            default_search_uris=[],
            read_file_contents={},
            page_id_map=MockPageIdMap(),
        )

        result = await MemoryReadTool().execute(
            tool_ctx,
            uri="viking://user/default/memories/experiences/test.md",
            offset=1,
            limit=1,
        )

        assert result["page_id"] == 42
        assert result["content"] == "2\tline2"

    @pytest.mark.asyncio
    async def test_read_tool_warns_when_offset_is_past_end_of_file(self):
        class MockPageIdMap:
            def get_page_id(self, uri):
                return None

        class MockVikingFS:
            async def read_file(self, uri, ctx=None, **kwargs):
                return 'line1\nline2\n\n<!-- MEMORY_FIELDS\n{"memory_type": "experiences"}\n-->'

        tool_ctx = ToolContext(
            viking_fs=MockVikingFS(),
            request_ctx=RequestContext(user=UserIdentifier.the_default_user(), role=Role.USER),
            default_search_uris=[],
            read_file_contents={},
            page_id_map=MockPageIdMap(),
        )

        result = await MemoryReadTool().execute(
            tool_ctx,
            uri="viking://user/default/memories/experiences/test.md",
            offset=5,
        )

        assert result["content"] == (
            "<system-reminder>Warning: the file exists but is shorter than the provided "
            "offset (6). The file has 2 lines.</system-reminder>"
        )

    def test_search_tool_properties(self):
        """Test MemorySearchTool properties."""
        tool = MemorySearchTool()

        assert tool.name == "search"
        assert "Semantic search" in tool.description
        assert "query" in tool.parameters["properties"]
        assert "limit" in tool.parameters["properties"]

    @pytest.mark.asyncio
    async def test_search_tool_uses_request_context(self):
        """Test MemorySearchTool passes RequestContext into VikingFS search."""

        class MockSearchResult:
            def to_dict(self):
                return {
                    "memories": [
                        {
                            "uri": "viking://user/test-account/test-user/memories/profile.md",
                            "score": 0.9,
                        }
                    ],
                    "resources": [],
                    "skills": [],
                }

        class MockVikingFS:
            def __init__(self):
                self.received_ctx = None
                self.received_target_uri = None
                self.received_limit = None

            async def search(self, query, target_uri="", limit=10, ctx=None, **kwargs):
                self.received_ctx = ctx
                self.received_target_uri = target_uri
                self.received_limit = limit
                return MockSearchResult()

        request_ctx = RequestContext(
            user=UserIdentifier(
                account_id="test-account",
                user_id="test-user",
            ),
            role=Role.USER,
        )
        viking_fs = MockVikingFS()
        # Create tool_ctx with viking_fs included
        tool_ctx = ToolContext(
            viking_fs=viking_fs,
            request_ctx=request_ctx,
            default_search_uris=["viking://user/test-account/test-user/memories"],
            read_file_contents={},
        )

        result = await MemorySearchTool().execute(
            tool_ctx,
            query="profile",
            limit=2,
        )

        assert result == [
            {"uri": "viking://user/test-account/test-user/memories/profile.md", "score": 0.9}
        ]
        assert viking_fs.received_ctx is request_ctx
        assert viking_fs.received_target_uri == tool_ctx.default_search_uris
        assert viking_fs.received_limit == 12

    def test_ls_tool_properties(self):
        """Test MemoryLsTool properties."""
        tool = MemoryLsTool()

        assert tool.name == "ls"
        assert "List directory" in tool.description
        assert "uri" in tool.parameters["properties"]

    def test_to_schema(self):
        """Test tool to_schema method."""
        tool = MemoryReadTool()
        schema = tool.to_schema()

        assert schema["type"] == "function"
        assert schema["function"]["name"] == "read"
        assert "description" in schema["function"]
        assert "parameters" in schema["function"]

    def test_tool_registry(self):
        """Test tool registry functions."""
        # Check that default tools are registered
        assert "read" in MEMORY_TOOLS_REGISTRY
        assert "read_by_fields" in MEMORY_TOOLS_REGISTRY
        assert "search" in MEMORY_TOOLS_REGISTRY
        assert "ls" in MEMORY_TOOLS_REGISTRY
        assert "eval" in MEMORY_TOOLS_REGISTRY

        # Check get_tool
        read_tool = get_tool("read")
        assert read_tool is not None
        assert isinstance(read_tool, MemoryReadTool)
        assert isinstance(get_tool("read_by_fields"), MemoryReadByFieldsTool)
        assert isinstance(get_tool("eval"), MemoryEvalTool)


def _eval_alias_context(bindings=None, executor=None):
    function = MemoryEvalFunction(
        name="read",
        signature="read(uri: str)",
        description="Read a memory.",
        allowed_arguments=frozenset({"uri"}),
        required_arguments=frozenset({"uri"}),
        kind="read",
        executor=executor,
    )
    return ToolContext(
        viking_fs=None,
        request_ctx=RequestContext(user=UserIdentifier.the_default_user(), role=Role.USER),
        memory_eval_functions={"read": function},
        memory_eval_bindings=bindings or {},
        allowed_memory_read_uris=set(),
    )


@pytest.mark.asyncio
async def test_eval_aliases_trusted_results_and_only_returns_new_assignments():
    memory = {"uri": "viking://user/default/memories/test.md", "content": "known"}
    bindings = {"prefetched_search": [memory], "nested": {"items": [memory]}, "unused": 42}
    executor = AsyncMock(return_value={"content": "fresh"})
    result = await MemoryEvalTool().execute(
        _eval_alias_context(bindings, executor),
        code="project = prefetched_search[0]\nother = nested['items'][-1]\n"
        "alias = project\nfresh = sdk.read(uri=alias['uri'])",
    )
    assert result == {
        "project": memory,
        "other": memory,
        "alias": memory,
        "fresh": {"content": "fresh"},
    }
    assert list(bindings) == ["prefetched_search", "nested", "unused"]
    assert executor.call_args.args[1] == {"uri": memory["uri"]}


@pytest.mark.asyncio
async def test_eval_can_rebind_prior_name_without_mutating_input_snapshot():
    bindings = {"selected": ["first", "second"], "indices": {"last": -1}}
    result = await MemoryEvalTool().execute(
        _eval_alias_context(bindings),
        code="index = indices['last']\nselected = selected[index]\ncopy = selected",
    )
    assert result == {"index": -1, "selected": "second", "copy": "second"}
    assert bindings["selected"] == ["first", "second"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tail",
    [
        "alias = missing",
        "alias = known[missing]",
        "alias = known.__class__",
        "alias = known[0].__class__",
        "known[0] = 'changed'",
        "alias = known[:]",
        "alias = sdk.write(uri='x')",
        "alias = known.append('x')",
        "alias = __import__('os')",
        "alias = {'uri': 'forged'}",
        "alias = [known[0]]",
        "alias = 7",
        "alias = [x for x in known]",
        "alias = (lambda: 7)()",
        "sdk.read(uri='x')",
        "alias = known\nalias = known",
        "sdk = known",
        "_hidden = known",
        "read = known",
    ],
)
async def test_eval_alias_program_validates_all_statements_before_any_read(tail):
    executor = AsyncMock()
    context = _eval_alias_context({"known": [{"uri": "x"}]}, executor)
    result = await MemoryEvalTool().execute(context, code="first = sdk.read(uri='x')\n" + tail)
    assert "error" in result
    executor.assert_not_awaited()
    assert context.memory_eval_bindings == {"known": [{"uri": "x"}]}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    [
        "alias = known[2]",
        "alias = known['bad']",
        "alias = known[True]",
        "alias = mapping['missing']",
        "alias = mapping[[]]",
        "alias = text[0]",
    ],
)
async def test_eval_alias_rejects_invalid_subscripts_without_publishing_partial_results(code):
    context = _eval_alias_context({"known": [1], "mapping": {}, "text": "private"})
    result = await MemoryEvalTool().execute(context, code="first = known\n" + code)
    assert "error" in result and "first" not in result
    assert set(context.memory_eval_bindings) == {"known", "mapping", "text"}


@pytest.mark.asyncio
async def test_eval_alias_does_not_dispatch_arbitrary_getitem_or_expand_read_scope():
    class Trap:
        def __getitem__(self, key):
            raise AssertionError("arbitrary __getitem__ must never execute")

    result = await MemoryEvalTool().execute(
        _eval_alias_context({"trap": Trap()}), code="alias = trap[0]"
    )
    assert "error" in result and "arbitrary __getitem__" not in result["error"]
    uri = "viking://user/other/memories/private.md"
    result = await MemoryEvalTool().execute(
        _eval_alias_context({"hits": [{"uri": uri}]}),
        code="alias = hits[0]\nreadback = sdk.read(uri=alias['uri'])",
    )
    assert result["readback"] == {"error": f"URI not allowed: {uri}"}


@pytest.mark.asyncio
async def test_eval_alias_bindings_are_not_global_and_same_program_read_aliases_work():
    executor = AsyncMock(return_value={"uri": "known"})
    first = _eval_alias_context({"existing": []}, executor)
    result = await MemoryEvalTool().execute(
        first, code="readback = sdk.read(uri='x')\nalias = readback"
    )
    assert result == {"readback": {"uri": "known"}, "alias": {"uri": "known"}}
    assert "error" in await MemoryEvalTool().execute(_eval_alias_context(), code="alias = existing")


@pytest.mark.asyncio
async def test_eval_has_no_statement_cap_for_safe_aliases():
    context = _eval_alias_context({"source": {"content": "known"}})
    result = await MemoryEvalTool().execute(
        context, code="\n".join(f"r{i} = source" for i in range(256))
    )
    assert len(result) == 256 and result["r255"] == {"content": "known"}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["sdk", "_private", "error", "not-a-name", "class"])
async def test_eval_rejects_invalid_trusted_binding_names(name):
    executor = AsyncMock()
    result = await MemoryEvalTool().execute(
        _eval_alias_context({name: {"content": "unavailable"}}, executor),
        code="value = sdk.read(uri='x')",
    )
    assert result == {"error": "Invalid existing memory eval variable name"}
    executor.assert_not_awaited()


@pytest.mark.asyncio
async def test_eval_reserves_error_envelope_name_but_can_alias_nested_errors():
    context = _eval_alias_context({"missing": {"error": "not found"}})
    result = await MemoryEvalTool().execute(
        context, code="alias = missing\nmessage = alias['error']"
    )
    assert result == {"alias": {"error": "not found"}, "message": "not found"}
    assert (
        "invalid or duplicate result variable"
        in (await MemoryEvalTool().execute(context, code="error = missing['error']"))["error"]
    )


@pytest.mark.asyncio
async def test_eval_does_not_accept_model_supplied_binding_environment():
    context = _eval_alias_context()
    result = await MemoryEvalTool().execute(
        context,
        code="alias = forged",
        memory_eval_bindings={"forged": {"content": "not a trusted tool result"}},
    )
    assert "unknown result variable" in result["error"]
    assert context.memory_eval_bindings == {}
