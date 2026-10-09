# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""
Test that provider instruction correctly instructs LLM.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from openviking.message import ImagePart, Message, TextPart, ToolPart
from openviking.session.memory.session_extract_context_provider import SessionExtractContextProvider
from openviking.session.memory.tools import get_tool
from openviking.session.memory.vision_message_normalizer import IMAGE_DESCRIPTION_PROMPT


@pytest.fixture(autouse=True)
def _runtime_config(monkeypatch):
    config = SimpleNamespace(
        output_language_override="",
        language_fallback="en",
        memory=SimpleNamespace(
            eager_prefetch=False,
            prefetch_search_topn=5,
            link_enabled=False,
        ),
    )
    monkeypatch.setattr(
        "openviking.session.memory.session_extract_context_provider.get_openviking_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "openviking.session.memory.utils.language.get_openviking_config",
        lambda: config,
    )


class TestProviderInstruction:
    """Test the provider instruction contains correct instructions."""

    def test_instruction_contains_read_before_edit_instructions(self):
        """Test that instruction explicitly tells LLM to read files before editing."""
        # Create provider with mock messages
        mock_messages = []
        provider = SessionExtractContextProvider(messages=mock_messages)

        instruction = provider.instruction()

        # Check for critical instructions
        assert (
            "Before editing ANY existing memory file, you MUST first read its complete content"
            in instruction
        )
        assert "Available callable tools: `eval`." in instruction
        assert "Use `eval` only for the read-only functions" in instruction
        assert "Prefer schema-specific `sdk.read_<memory_type>` functions" in instruction
        assert "use the available tools (read/search)" not in instruction
        assert "Never call `search_replace`" in instruction

    def test_instruction_disallows_tool_calls_when_prefetch_is_eager(self):
        provider = SessionExtractContextProvider(messages=[])
        provider._eager_prefetch = True

        instruction = provider.instruction()

        assert "no callable tools are available" in instruction
        assert "No callable tools are available. Do not emit tool calls." in instruction

    def test_eval_tool_description_is_generated_from_enabled_schemas(self):
        schema = SimpleNamespace(
            memory_type="ss_plot",
            stage="user",
            peer_enabled=False,
            identity_fields=lambda include_peer_id=False: ("project_id", "plot_id"),
        )
        registry = MagicMock()
        registry.list_all.return_value = [schema]
        provider = SessionExtractContextProvider(messages=[], memory_registry=registry)

        tool_schema = provider.customize_tool_schema("eval", get_tool("eval").to_schema())
        description = tool_schema["function"]["description"]

        assert "sdk.read(uri: str" in description
        assert "sdk.search(query: str" in description
        assert "sdk.search_by_session(limit: int = 10)" in description
        assert "sdk.ls(uri: str, limit: int = 100)" in description
        assert "sdk.read_ss_plot(project_id: str, plot_id: str" in description

    def test_instruction_includes_extraction_and_maintenance_objective(self):
        provider = SessionExtractContextProvider(messages=[])

        instruction = provider.instruction()

        assert (
            "You are a memory extraction and maintenance agent. Analyze the conversation "
            "together with the pre-fetched existing memories, then produce a single atomic "
            "plan to create, update, delete, or reorganize memories so that the final memory "
            "collection conforms to all enabled memory schemas." in instruction
        )

    def test_instruction_contains_output_language(self):
        """Test that instruction includes the output language setting."""
        mock_messages = []
        provider = SessionExtractContextProvider(messages=mock_messages)

        instruction = provider.instruction()

        # Check that output language instruction is present
        assert "Target Output Language" in instruction
        assert "All memory content MUST be written in" in instruction

    def test_instruction_includes_session_extraction_context_as_non_evidence(self):
        provider = SessionExtractContextProvider(
            messages=[Message(id="m1", role="user", parts=[TextPart("user evidence")])],
            extraction_context='{"project_id":"project-1"}',
        )

        instruction = provider.instruction()

        assert "[SESSION_EXTRACTION_CONTEXT BEGIN]" in instruction
        assert '{"project_id":"project-1"}' in instruction
        assert "not conversation" in instruction
        assert "evidence and MUST NOT be copied into memory content" in instruction
        assert "[SESSION_EXTRACTION_CONTEXT END]" in instruction
        assert provider.get_conversation_text() == "user evidence"

    def test_instruction_omits_empty_session_extraction_context(self):
        provider = SessionExtractContextProvider(messages=[])

        assert "SESSION_EXTRACTION_CONTEXT" not in provider.instruction()

    def test_instruction_explains_peer_memory_routing(self):
        provider = SessionExtractContextProvider(messages=[])

        instruction = provider.instruction()

        assert "Peer Memory" in instruction
        assert "Message role is authoritative for newly extracted facts" in instruction
        assert "attribute each fact to the speaker whose" in instruction
        assert "follow each enabled memory type's own schema rules" in instruction
        assert "Do not infer ownership from neighboring messages" in instruction
        assert "Facts already stored in pre-fetched or explicitly read memories" in instruction
        assert "remain valid sources for" in instruction
        assert "maintenance and may be preserved" in instruction
        assert "or moved between enabled memory" in instruction
        assert "types when required by their schemas" in instruction
        assert "same schema-defined identity" in instruction
        assert "Similarity or shared topics are insufficient" in instruction
        assert "If identity, validity, or destination is uncertain, keep the source" in instruction
        assert "Split only into distinct schema-valid identities" in instruction
        assert "Keep one identity in one object" in instruction
        assert "Compact duplicate wording only" in instruction

    def test_instruction_omits_resource_uri_handling_without_resource_uri(self):
        provider = SessionExtractContextProvider(
            messages=[Message(id="m1", role="user", parts=[TextPart("我喜欢越前龙马。")])]
        )

        instruction = provider.instruction()

        assert "Resource URI Handling" not in instruction
        assert "Affected memory URIs" not in instruction

    def test_instruction_includes_resource_uri_handling_for_user_scoped_resource_uri(self):
        provider = SessionExtractContextProvider(
            messages=[
                Message(
                    id="m1",
                    role="user",
                    parts=[
                        TextPart(
                            "这张图是越前龙马："
                            "viking://user/ryoma/peers/fuji/resources/images/yueqian_jpeg"
                        )
                    ],
                )
            ]
        )

        instruction = provider.instruction()

        assert "Resource URI Handling" in instruction
        assert "viking://user/{user_id}/resources/..." in instruction
        assert "viking://user/{user_id}/peers/{peer_id}/resources/..." in instruction
        assert (
            "system-generated `## Resource Deletion` block's `Affected memory URIs`" in instruction
        )

    def test_resource_deletion_memory_uris_are_allowed_for_eval_read(self):
        provider = SessionExtractContextProvider(
            messages=[
                Message(
                    id="m1",
                    role="user",
                    parts=[
                        TextPart(
                            "## Resource Deletion\n"
                            "Affected memory URIs:\n"
                            "- viking://user/alice/memories/preferences/editor.md\n\n"
                            "Update existing memories."
                        )
                    ],
                )
            ]
        )

        assert provider._resource_deletion_memory_uris() == {
            "viking://user/alice/memories/preferences/editor.md"
        }


class TestSessionConversationToolFiltering:
    def test_session_conversation_includes_existing_turn_id_in_header(self):
        provider = SessionExtractContextProvider(
            messages=[
                Message(
                    id="m1",
                    role="user",
                    parts=[TextPart("continue this plot")],
                    turn_id="turn-123",
                )
            ]
        )

        conversation = provider._assemble_conversation(provider.messages)

        assert "[0][user][user][turn_id=turn-123]: continue this plot" in conversation

    def test_session_conversation_omits_skill_tool_call(self):
        messages = [
            Message(
                id="m1",
                role="assistant",
                parts=[
                    TextPart("Running a skill."),
                    ToolPart(
                        tool_id="tool_1",
                        tool_name="read",
                        tool_uri="viking://session/test/tools/tool_1",
                        skill_uri="viking://user/skills/create_presentation",
                        tool_input={"file_path": "/skills/ppt/SKILL.md"},
                        tool_output="ok",
                        tool_status="completed",
                        duration_ms=123,
                    ),
                ],
            )
        ]
        provider = SessionExtractContextProvider(messages=messages)

        conversation = provider._assemble_conversation(messages)

        assert "ToolCall:" not in conversation
        assert "create_presentation" not in conversation
        assert "Running a skill." in conversation

    def test_session_conversation_omits_regular_tool_call(self):
        messages = [
            Message(
                id="m1",
                role="assistant",
                parts=[
                    TextPart("Running a tool."),
                    ToolPart(
                        tool_id="tool_1",
                        tool_name="read",
                        tool_uri="viking://session/test/tools/tool_1",
                        tool_input={"file_path": "README.md"},
                        tool_output="ok",
                        tool_status="completed",
                        duration_ms=123,
                    ),
                ],
            )
        ]
        provider = SessionExtractContextProvider(messages=messages)

        conversation = provider._assemble_conversation(messages)

        assert "ToolCall:" not in conversation
        assert "tool_name=read" not in conversation
        assert "Running a tool." in conversation

    def test_agent_provider_conversation_includes_tool_call_evidence(self):
        from openviking.session.memory.experience.trajectory_context import (
            AgentTrajectoryContextProvider,
        )

        messages = [
            Message(
                id="m1",
                role="assistant",
                parts=[
                    TextPart("Checking the reservation."),
                    ToolPart(
                        tool_id="tool_1",
                        tool_name="get_reservation_details",
                        tool_input={"reservation_id": "EHGLP3"},
                        tool_output="available",
                        tool_status="completed",
                    ),
                ],
            )
        ]
        provider = AgentTrajectoryContextProvider(messages=messages)

        conversation = provider._assemble_conversation(messages)

        assert "ToolCall: tool_name=get_reservation_details" in conversation
        assert "input={'reservation_id': 'EHGLP3'}" in conversation
        assert "output=available" in conversation

    def test_assemble_conversation_uses_peer_id_when_present(self):
        messages = [
            Message(
                id="m1",
                role="user",
                parts=[TextPart("My invoice is still missing.")],
                peer_id="web-visitor-alice",
            )
        ]
        provider = SessionExtractContextProvider(messages=messages)

        conversation = provider._assemble_conversation(messages)

        assert "[0][user][web-visitor-alice]" in conversation
        assert "[0][user][default]" not in conversation

    def test_detect_language_only_uses_text_parts(self):
        messages = [
            Message(
                id="m1",
                role="assistant",
                parts=[TextPart("Please keep the memory in English.")],
            ),
            Message(
                id="m2",
                role="assistant",
                parts=[
                    ToolPart(
                        tool_id="tool_1",
                        tool_name="read",
                        tool_uri="viking://session/test/tools/tool_1",
                        tool_input={"file_path": "README.md"},
                        tool_output="这是中文工具输出",
                        tool_status="completed",
                    )
                ],
            ),
        ]

        provider = SessionExtractContextProvider(messages=messages)

        assert provider._detect_language() == "en"

    def test_detect_language_prefers_user_text_over_assistant_text(self):
        messages = [
            Message(
                id="m1",
                role="user",
                parts=[TextPart("请把记忆保持为中文，继续优化。")],
            ),
            Message(
                id="m2",
                role="assistant",
                parts=[TextPart("한국어 응답이 섞였습니다")],
            ),
        ]

        provider = SessionExtractContextProvider(messages=messages)

        assert provider._detect_language() == "zh-CN"

    async def test_prepare_extraction_messages_replaces_image_part_with_vlm_description(self):
        class FakeVisionVLM:
            def __init__(self):
                self.messages = None

            def is_available(self):
                return True

            async def get_vision_completion_async(self, **kwargs):
                self.messages = kwargs.get("messages")
                return "A high level image description."

        messages = [
            Message(
                id="m1",
                role="user",
                turn_id="turn-1",
                message_kind="user_query",
                source_message_ids=["source-1"],
                parts=[
                    TextPart("Please remember this image."),
                    ImagePart(url="https://example.com/image.png", detail="auto"),
                ],
            )
        ]
        fake_vlm = FakeVisionVLM()
        provider = SessionExtractContextProvider(
            messages=messages,
            vlm_config=fake_vlm,
        )

        await provider.prepare_extraction_messages()
        prompt_message = provider._build_conversation_message()

        assert provider.messages[0].turn_id == "turn-1"
        assert provider.messages[0].message_kind == "user_query"
        assert provider.messages[0].source_message_ids == ["source-1"]
        assert "Please remember this image." in prompt_message["content"]
        assert "A high level image description." in prompt_message["content"]
        assert "https://example.com/image.png" not in prompt_message["content"]
        assert fake_vlm.messages == [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": IMAGE_DESCRIPTION_PROMPT,
                    },
                    {"type": "text", "text": "Please remember this image."},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "https://example.com/image.png",
                            "detail": "auto",
                        },
                    },
                ],
            }
        ]

    async def test_prepare_extraction_messages_skips_image_only_without_description(self):
        messages = [
            Message(
                id="m1",
                role="user",
                parts=[ImagePart(url="https://example.com/private-family-photo.png")],
            )
        ]
        provider = SessionExtractContextProvider(
            messages=messages,
            vlm_config=SimpleNamespace(is_available=lambda: False),
        )

        await provider.prepare_extraction_messages()
        prompt_message = provider._build_conversation_message()

        assert "[Image description]" not in prompt_message["content"]
        assert "https://example.com/private-family-photo.png" not in prompt_message["content"]

    async def test_prepare_extraction_messages_keeps_text_when_image_is_undescribed(self):
        messages = [
            Message(
                id="m1",
                role="user",
                parts=[
                    TextPart("Please remember the surrounding text."),
                    ImagePart(url="https://example.com/private-family-photo.png"),
                ],
            )
        ]
        provider = SessionExtractContextProvider(
            messages=messages,
            vlm_config=SimpleNamespace(is_available=lambda: False),
        )

        await provider.prepare_extraction_messages()
        prompt_message = provider._build_conversation_message()

        assert "Please remember the surrounding text." in prompt_message["content"]
        assert "[Image description]" not in prompt_message["content"]
        assert "https://example.com/private-family-photo.png" not in prompt_message["content"]

    async def test_prepare_extraction_messages_does_not_replace_caller_message_list(self):
        messages = [
            Message(
                id="m1",
                role="user",
                parts=[
                    TextPart("Please remember this image."),
                    ImagePart(url="https://example.com/image.png"),
                ],
            )
        ]
        provider = SessionExtractContextProvider(
            messages=messages,
            vlm_config=SimpleNamespace(is_available=lambda: False),
        )

        await provider.prepare_extraction_messages()

        assert len(messages) == 1
        assert any(isinstance(part, ImagePart) for part in messages[0].parts)
        assert provider.messages is not messages


def test_session_provider_empty_messages_still_uses_environment_fallback(monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    provider = SessionExtractContextProvider(messages=[])

    assert provider.get_output_language() == "zh-CN"


@pytest.mark.asyncio
async def test_execute_tool_rejects_registered_tool_outside_provider_allowlist(monkeypatch):
    search_tool = SimpleNamespace(execute=AsyncMock(return_value={"memories": []}))
    monkeypatch.setattr(
        "openviking.session.memory.session_extract_context_provider.get_tool",
        lambda _name: search_tool,
    )
    provider = SessionExtractContextProvider(messages=[])

    result = await provider.execute_tool(SimpleNamespace(name="search", arguments={}))

    assert result == {"error": "Tool not allowed: search"}
    search_tool.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_tool_rejects_raw_read_tool(monkeypatch):
    read_tool = SimpleNamespace(execute=AsyncMock(return_value={"content": "unused"}))
    monkeypatch.setattr(
        "openviking.session.memory.session_extract_context_provider.get_tool",
        lambda _name: read_tool,
    )
    provider = SessionExtractContextProvider(messages=[])

    result = await provider.execute_tool(SimpleNamespace(name="read", arguments={}))

    assert result == {"error": "Tool not allowed: read"}
    read_tool.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_eval_uses_enabled_schema_function_to_generate_uri():
    schema = SimpleNamespace(
        memory_type="ss_clip",
        stage="user",
        peer_enabled=False,
        directory="viking://user/{{ user_space }}/memories/ss/projects",
        filename_template=("{{ project_id }}/plots/{{ plot_id }}/clips/{{ clip_id }}/clip.md"),
        identity_fields=lambda include_peer_id=False: ("project_id", "plot_id", "clip_id"),
    )
    registry = MagicMock()
    registry.list_all.return_value = [schema]
    request_ctx = MagicMock()
    request_ctx.user.user_id = "ss_memory_eval_user"
    viking_fs = MagicMock()
    viking_fs.read_file = AsyncMock(
        return_value=(
            "- 只把信封改成中文\n\n"
            '<!-- MEMORY_FIELDS\n{"memory_type":"ss_clip","clip_id":"clip_1"}\n-->'
        )
    )
    provider = SessionExtractContextProvider(
        messages=[],
        ctx=request_ctx,
        viking_fs=viking_fs,
        memory_registry=registry,
    )

    result = await provider.execute_tool(
        SimpleNamespace(
            name="eval",
            arguments={
                "code": (
                    'a = sdk.read_ss_clip(project_id="proj_1", plot_id="plot_1", clip_id="clip_1")'
                )
            },
        )
    )

    expected_uri = (
        "viking://user/ss_memory_eval_user/memories/ss/projects/"
        "proj_1/plots/plot_1/clips/clip_1/clip.md"
    )
    viking_fs.read_file.assert_awaited_once_with(expected_uri, ctx=request_ctx)
    assert result["a"]["memory_type"] == "ss_clip"
    assert result["a"]["content"] == "1\t- 只把信封改成中文"
    assert expected_uri in provider.read_file_contents


@pytest.mark.asyncio
async def test_eval_schema_function_rejects_fields_outside_schema_identity():
    schema = SimpleNamespace(
        memory_type="ss_project",
        stage="user",
        peer_enabled=False,
        directory="viking://user/{{ user_space }}/memories/ss/projects",
        filename_template="{{ project_id }}/project.md",
        identity_fields=lambda include_peer_id=False: ("project_id",),
    )
    registry = MagicMock()
    registry.list_all.return_value = [schema]
    provider = SessionExtractContextProvider(
        messages=[],
        ctx=MagicMock(user=MagicMock(user_id="ss_memory_eval_user")),
        viking_fs=MagicMock(),
        memory_registry=registry,
    )

    result = await provider.execute_tool(
        SimpleNamespace(
            name="eval",
            arguments={
                "code": (
                    'a = sdk.read_ss_project(project_id="proj_1", content="not an identity field")'
                )
            },
        )
    )

    assert result == {"error": "Line 1: unsupported arguments for sdk.read_ss_project: content"}


@pytest.mark.asyncio
async def test_eval_schema_function_rejects_missing_identity_field():
    schema = SimpleNamespace(
        memory_type="ss_plot",
        stage="user",
        peer_enabled=False,
        directory="viking://user/{{ user_space }}/memories/ss/projects",
        filename_template="{{ project_id }}/plots/{{ plot_id }}/plot.md",
        identity_fields=lambda include_peer_id=False: ("project_id", "plot_id"),
    )
    registry = MagicMock()
    registry.list_all.return_value = [schema]
    provider = SessionExtractContextProvider(
        messages=[],
        ctx=MagicMock(user=MagicMock(user_id="ss_memory_eval_user")),
        viking_fs=MagicMock(),
        memory_registry=registry,
    )

    result = await provider.execute_tool(
        SimpleNamespace(
            name="eval",
            arguments={
                "code": 'a = sdk.read_ss_plot(project_id="proj_1")',
            },
        )
    )

    assert result == {"error": "Line 1: missing required arguments for sdk.read_ss_plot: plot_id"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "session_text",
    ["", "PRIVATE_SESSION_QUERY " + "detail " * 1000],
    ids=["empty-session", "long-session"],
)
async def test_prefetch_searches_by_session_without_echoing_query(session_text):
    schema = SimpleNamespace(
        memory_type="ss_project",
        stage="user",
        peer_enabled=False,
        directory="viking://user/{user_space}/memories/ss/projects",
        operation_mode="merge",
        filename_has_variables=lambda: True,
        identity_fields=lambda include_peer_id=False: ("project_id",),
    )
    isolation_handler = MagicMock()
    isolation_handler.allows_schema.return_value = True
    isolation_handler.get_read_scope.return_value = SimpleNamespace(
        user_ids=["ss_memory_eval_user"], peer_ids=[]
    )
    isolation_handler.render_schema_directories.return_value = [
        "viking://user/ss_memory_eval_user/memories/ss/projects"
    ]
    registry = MagicMock()
    registry.list_all.return_value = [schema]
    request_ctx = MagicMock()
    request_ctx.user.user_id = "ss_memory_eval_user"
    search_result = MagicMock()
    search_result.to_dict.return_value = {
        "memories": [
            {
                "uri": (
                    "viking://user/ss_memory_eval_user/memories/ss/projects/"
                    "proj_00G6pawQ/project.md"
                ),
                "score": 0.9,
            }
        ]
    }
    viking_fs = MagicMock()
    viking_fs.search = AsyncMock(return_value=search_result)
    viking_fs.read_file = AsyncMock(
        return_value=(
            "- project rule\n\n"
            '<!-- MEMORY_FIELDS\n{"memory_type":"ss_project",'
            '"project_id":"proj_00G6pawQ"}\n-->'
        )
    )
    provider = SessionExtractContextProvider(
        messages=[Message(id="m1", role="user", parts=[TextPart(session_text)])],
        isolation_handler=isolation_handler,
        memory_registry=registry,
        ctx=request_ctx,
        viking_fs=viking_fs,
    )
    provider._build_conversation_message = MagicMock(
        return_value={"role": "user", "content": "conversation"}
    )
    messages = await provider.prefetch()

    assert [message["role"] for message in messages] == ["user", "assistant", "tool"]
    call = messages[1]["tool_calls"][0]
    assert call["function"]["name"] == "eval"
    assert (
        json.loads(call["function"]["arguments"])["code"]
        == "prefetched_search = sdk.search_by_session(limit=5)"
    )
    assert messages[2]["tool_call_id"] == call["id"]
    assert "PRIVATE_SESSION_QUERY" not in json.dumps(messages[1:])
    assert (
        json.loads(messages[2]["content"])["prefetched_search"][0]["content"] == "1\t- project rule"
    )
    viking_fs.search.assert_awaited_once_with(
        provider._build_prefetch_search_query() or "conversation",
        target_uri=isolation_handler.render_schema_directories.return_value,
        limit=15,
        ctx=request_ctx,
    )
    viking_fs.read_file.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "expected_limit", "expected_error"),
    [
        ("hits = sdk.search_by_session()", 10, None),
        ("hits = sdk.search_by_session(limit=2)", 2, None),
        (
            "hits = sdk.search_by_session(limit=0)",
            None,
            "limit must be an integer between 1 and 10",
        ),
        (
            "hits = sdk.search_by_session(limit=-1)",
            None,
            "limit must be an integer between 1 and 10",
        ),
        (
            "hits = sdk.search_by_session(limit=11)",
            None,
            "limit must be an integer between 1 and 10",
        ),
        (
            "hits = sdk.search_by_session(limit=True)",
            None,
            "limit must be an integer between 1 and 10",
        ),
        (
            'hits = sdk.search_by_session(limit="2")',
            None,
            "limit must be an integer between 1 and 10",
        ),
        (
            'hits = sdk.search_by_session(query="other")',
            None,
            "unsupported arguments for sdk.search_by_session: query",
        ),
        (
            'hits = sdk.search_by_session(target_uri="viking://user/other/memories")',
            None,
            "unsupported arguments for sdk.search_by_session: target_uri",
        ),
    ],
)
async def test_search_by_session_uses_current_context_and_validates_limit(
    code, expected_limit, expected_error
):
    registry = MagicMock()
    registry.list_all.return_value = []
    request_ctx = MagicMock()
    request_ctx.user.user_id = "ss_memory_eval_user"
    viking_fs = MagicMock()
    viking_fs.search = AsyncMock(return_value=SimpleNamespace(to_dict=lambda: {"memories": []}))
    provider = SessionExtractContextProvider(
        messages=[Message(id="m1", role="user", parts=[TextPart("Keep the original framing")])],
        memory_registry=registry,
        ctx=request_ctx,
        viking_fs=viking_fs,
    )
    provider._schema_directories = ["viking://user/ss_memory_eval_user/memories/ss/projects"]

    result = await provider.execute_tool(SimpleNamespace(name="eval", arguments={"code": code}))

    if expected_error:
        assert expected_error in result["error"]
        viking_fs.search.assert_not_awaited()
    else:
        assert result == {"hits": []}
        viking_fs.search.assert_awaited_once_with(
            "user: Keep the original framing",
            target_uri=provider._schema_directories,
            limit=expected_limit + 10,
            ctx=request_ctx,
        )


@pytest.mark.asyncio
async def test_prefetch_reads_singleton_schema_through_simulated_eval():
    schema = SimpleNamespace(
        memory_type="ss_user",
        stage="user",
        peer_enabled=False,
        directory="viking://user/{{ user_space }}/memories/ss",
        filename_template="user.md",
        operation_mode="merge",
        filename_has_variables=lambda: False,
        identity_fields=lambda include_peer_id=False: (),
    )
    isolation_handler = MagicMock()
    isolation_handler.allows_schema.return_value = True
    isolation_handler.get_read_scope.return_value = SimpleNamespace(
        user_ids=["ss_memory_eval_user"], peer_ids=[]
    )
    isolation_handler.render_schema_directories.return_value = [
        "viking://user/ss_memory_eval_user/memories/ss"
    ]
    registry = MagicMock()
    registry.list_all.return_value = [schema]
    request_ctx = MagicMock()
    request_ctx.user.user_id = "ss_memory_eval_user"
    viking_fs = MagicMock()
    viking_fs.read_file = AsyncMock(
        return_value='- concise\n\n<!-- MEMORY_FIELDS\n{"memory_type":"ss_user"}\n-->'
    )
    provider = SessionExtractContextProvider(
        messages=[],
        isolation_handler=isolation_handler,
        memory_registry=registry,
        ctx=request_ctx,
        viking_fs=viking_fs,
    )
    provider._build_conversation_message = MagicMock(
        return_value={"role": "user", "content": "conversation"}
    )

    messages = await provider.prefetch()

    assert [message["role"] for message in messages] == ["user", "assistant", "tool"]
    call = messages[1]["tool_calls"][0]
    expected_uri = "viking://user/ss_memory_eval_user/memories/ss/user.md"
    assert call["function"]["name"] == "eval"
    assert (
        json.loads(call["function"]["arguments"])["code"]
        == f"prefetched_memory_1 = sdk.read(uri={expected_uri!r})"
    )
    assert messages[2]["tool_call_id"] == call["id"]
    assert json.loads(messages[2]["content"])["prefetched_memory_1"]["content"] == "1\t- concise"
    assert expected_uri in provider.read_file_contents


def test_domain_context_does_not_add_framework_provenance_requirements():
    text = (
        "[SS_TURN_CONTEXT BEGIN]\n"
        '{"routing_only": true, "turn_id": "turn-1", "project": {"id": "proj-1"}, '
        '"targets": [{"plot": {"id": "plot-1"}, "clip": {"id": "clip-1", "plot_id": "plot-1"}}], '
        '"target_count": 1, "targets_truncated": false}\n'
        "[SS_TURN_CONTEXT END]\n[USER_TEXT]\nMake this voice more sinister."
    )
    provider = SessionExtractContextProvider(
        messages=[Message(id="msg-1", role="user", turn_id="turn-1", parts=[TextPart(text)])]
    )
    instruction = provider.instruction()
    assert "source_turn_ids" not in instruction
    assert "SS Write Provenance" not in instruction
    assert provider.get_conversation_text() == text
    assert provider.messages[0].turn_id == "turn-1"
