import copy
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from vikingbot.agent import loop as loop_module
from vikingbot.agent.context import ContextBuilder
from vikingbot.agent.loop import AgentLoop
from vikingbot.agent.tools.base import MultimodalToolResult
from vikingbot.agent.tools.registry import ToolExecutionResult
from vikingbot.bus.events import InboundMessage, OutboundEventType
from vikingbot.bus.queue import MessageBus
from vikingbot.config.schema import Config, SessionKey
from vikingbot.openviking_mount.session_state import make_openviking_storage_session_id
from vikingbot.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from vikingbot.session.manager import SessionManager


class _FakeProvider(LLMProvider):
    async def chat(self, *args, **kwargs):  # pragma: no cover - should not be called
        raise AssertionError("provider.chat should not be called in no-reply outcome test")

    def get_default_model(self) -> str:
        return "fake-model"


class _FakeSubagentManager:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _RecordingProvider(LLMProvider):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def chat(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return LLMResponse(content="ok")

    def get_default_model(self) -> str:
        return "fake-model"


class _FakeLangfuseClient:
    def __init__(self):
        self.calls = []

    def update_generation_metadata(self, response_id, metadata):
        self.calls.append((response_id, metadata))
        return metadata

    def update_response_outcome(self, response_id, outcome_label, outcome_payload):
        self.calls.append((response_id, outcome_label, outcome_payload))
        return outcome_payload


class _FakeOVClient:
    def __init__(self, *, context_payload):
        self.context_payload = context_payload
        self.context_calls = []

    async def get_session_context(self, session_id, token_budget):
        self.context_calls.append((session_id, token_budget))
        return self.context_payload


@pytest.fixture
def make_loop(temp_dir, monkeypatch):
    """Construct a real loop with external integrations stubbed for these tests."""
    monkeypatch.setattr(AgentLoop, "_register_builtin_hooks", lambda self: None)
    monkeypatch.setattr("vikingbot.agent.loop.SubagentManager", _FakeSubagentManager)

    def create(*, config=None, provider=None, bus=None, register_tools=False, **kwargs):
        with monkeypatch.context() as setup:
            if not register_tools:
                setup.setattr(AgentLoop, "_register_default_tools", lambda self: None)
            return AgentLoop(
                bus=bus if bus is not None else MessageBus(),
                provider=provider if provider is not None else _FakeProvider(),
                workspace=temp_dir / "workspace",
                config=config if config is not None else Config(storage_workspace=str(temp_dir)),
                **kwargs,
            )

    return create


def _image_call(call_id, label=None):
    return ToolCallRequest(
        id=call_id, name="read_image", arguments={"label": label} if label else {}, tokens=1
    )


class _MediaProvider(_FakeProvider):
    def __init__(self, rounds, *, supports_media=True):
        super().__init__()
        self.calls = []
        self.responses = iter(
            [LLMResponse(content=None, tool_calls=calls) for calls in rounds]
            + [LLMResponse(content="done")]
        )
        self.supports_media = supports_media

    async def chat(self, messages, **kwargs):
        self.calls.append(copy.deepcopy(messages))
        return next(self.responses)

    def supports_tool_result_media(self, model=None):
        return self.supports_media


class _ImageRegistry:
    def __init__(self, content=None):
        self.content = content

    def get_definitions(self, **kwargs):
        return [
            {
                "type": "function",
                "function": {
                    "name": "read_image",
                    "description": "Read an image",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]

    async def execute_detailed(self, name, params, **kwargs):
        label = params.get("label", "Image resource.")
        content = (
            self.content
            if self.content is not None
            else [
                {"type": "text", "text": label},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,ZGF0YQ=="}},
            ]
        )
        return ToolExecutionResult(
            result=MultimodalToolResult(text=label, content=content), effective_params=params
        )


def test_context_keeps_multimodal_tool_result_on_tool_message(temp_dir: Path):
    context = ContextBuilder(workspace=temp_dir / "workspace")
    content = [
        {"type": "text", "text": "Source: viking://resources/image.png"},
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,aW1hZ2U="},
        },
    ]
    messages = context.add_tool_result(
        [],
        "call-1",
        "openviking_multi_read",
        MultimodalToolResult(text="Image resource.", content=content),
    )

    assert messages == [
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "name": "openviking_multi_read",
            "content": content,
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("supports_media", [False, True])
async def test_agent_loop_gates_multimodal_result_by_provider(make_loop, supports_media: bool):
    content = [
        {"type": "text", "text": "Source: viking://resources/image.png"},
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,aW1hZ2U="},
        },
    ]

    provider = _MediaProvider([[_image_call("call-1")]], supports_media=supports_media)
    loop = make_loop(provider=provider, max_iterations=2)

    final, _reasoning, tools_used, _usage, _iteration = await loop._run_agent_loop(
        messages=[{"role": "user", "content": "read it"}],
        session_key=SessionKey(type="cli", channel_id="default", chat_id="multimodal"),
        publish_events=False,
        tool_registry=_ImageRegistry(content),
    )

    assert final == "done"
    tool_message = next(message for message in provider.calls[1] if message["role"] == "tool")
    if supports_media:
        assert tool_message["content"] == content
    else:
        assert tool_message["content"].startswith("Image resource.")
        assert "does not support media in tool results" in tool_message["content"]
    assert tools_used[0]["result"] == "Image resource."


@pytest.mark.asyncio
async def test_agent_loop_keeps_experience_reminder_and_does_not_reinject_it(make_loop):
    provider = _MediaProvider([[_image_call("call-1")]])
    loop = make_loop(provider=provider, max_iterations=2)
    reminder = "[Experience Reminder]\nRemember the complete total."
    provider_calls = []

    async def experience_context_provider(messages, *, before_tools=False):
        if before_tools:
            return
        provider_calls.append(messages)
        yield reminder

    captured_experience_messages = []

    final, *_ = await loop._run_agent_loop(
        messages=[{"role": "user", "content": "read it"}],
        session_key=SessionKey(type="cli", channel_id="default", chat_id="experience-reminder"),
        publish_events=False,
        tool_registry=_ImageRegistry(),
        experience_context_provider=experience_context_provider,
        captured_experience_messages=captured_experience_messages,
    )

    assert final == "done"
    assert len(provider_calls) == 2
    assert [message for message in provider.calls[0] if message.get("content") == reminder] == [
        {"role": "user", "content": reminder}
    ]
    assert [message for message in provider.calls[1] if message.get("content") == reminder] == [
        {"role": "user", "content": reminder}
    ]
    assert captured_experience_messages == [
        {"role": "user", "content": reminder, "after_tool_count": 0}
    ]


@pytest.mark.asyncio
async def test_agent_loop_limits_media_across_parallel_tool_results(make_loop, monkeypatch):
    monkeypatch.setattr(loop_module, "MAX_INLINE_TOOL_RESULT_MEDIA_BYTES", 5)

    provider = _MediaProvider([[_image_call("call-1", "first"), _image_call("call-2", "second")]])
    loop = make_loop(provider=provider, max_iterations=2)

    final, *_ = await loop._run_agent_loop(
        messages=[{"role": "user", "content": "read both"}],
        session_key=SessionKey(type="cli", channel_id="default", chat_id="media-budget"),
        publish_events=False,
        tool_registry=_ImageRegistry(),
    )

    assert final == "done"
    tool_messages = [message for message in provider.calls[1] if message["role"] == "tool"]
    assert isinstance(tool_messages[0]["content"], list)
    assert isinstance(tool_messages[1]["content"], str)
    assert "make this model request exceed" in tool_messages[1]["content"]


@pytest.mark.asyncio
async def test_agent_loop_prefers_new_media_across_consecutive_tool_rounds(make_loop, monkeypatch):
    monkeypatch.setattr(loop_module, "MAX_INLINE_TOOL_RESULT_MEDIA_BYTES", 5)

    provider = _MediaProvider(
        [[_image_call("call-1", "image-1")], [_image_call("call-2", "image-2")]]
    )
    loop = make_loop(provider=provider, max_iterations=3)

    final, *_ = await loop._run_agent_loop(
        messages=[{"role": "user", "content": "read two images in sequence"}],
        session_key=SessionKey(type="cli", channel_id="default", chat_id="media-rounds"),
        publish_events=False,
        tool_registry=_ImageRegistry(),
    )

    assert final == "done"
    first_round_tool = next(message for message in provider.calls[1] if message["role"] == "tool")
    assert isinstance(first_round_tool["content"], list)

    tool_messages = [message for message in provider.calls[-1] if message["role"] == "tool"]
    assert isinstance(tool_messages[0]["content"], str)
    assert "earlier tool result was omitted" in tool_messages[0]["content"]
    assert isinstance(tool_messages[1]["content"], list)


def test_agent_loop_omits_spawn_tool_when_subagents_disabled(make_loop, temp_dir: Path):
    bus = MessageBus()
    provider = _RecordingProvider()
    config = Config(storage_workspace=str(temp_dir), agents={"subagent_enabled": False})

    loop = make_loop(
        bus=bus,
        provider=provider,
        model=config.agents.model,
        temperature=config.agents.temperature,
        config=config,
        register_tools=True,
    )

    assert "spawn" not in loop.tools.tool_names


def test_agent_loop_standalone_omits_openviking_tools(make_loop, temp_dir: Path):
    config = Config(storage_workspace=str(temp_dir))
    loop = make_loop(provider=_RecordingProvider(), config=config, register_tools=True)

    assert config.ov_server.server_url == ""
    assert not any(name.startswith("openviking_") for name in loop.tools.tool_names)
    session_key = SessionKey(type="cli", channel_id="default", chat_id="standalone")
    assert loop._get_ov_tools_enable(session_key) is False


@pytest.mark.asyncio
async def test_agent_loop_passes_configured_temperature_to_provider(make_loop, temp_dir: Path):
    bus = MessageBus()
    provider = _RecordingProvider()
    config = Config(storage_workspace=str(temp_dir), agents={"temperature": 0.2})
    loop = make_loop(
        bus=bus,
        provider=provider,
        model=config.agents.model,
        temperature=config.agents.temperature,
        config=config,
    )

    session_key = SessionKey(type="cli", channel_id="default", chat_id="session-1")
    response, _, _ = await loop._chat_with_stream_events(
        messages=[{"role": "user", "content": "hello"}],
        tools=[],
        session_key=session_key,
        publish_events=False,
    )

    assert response.content == "ok"
    assert provider.calls[0][1]["temperature"] == 0.2
    assert loop.subagents.kwargs["temperature"] == 0.2


@pytest.mark.asyncio
async def test_agent_loop_makes_final_no_tool_call_when_iteration_limit_reached(
    make_loop, temp_dir: Path
):
    class _ToolLimitProvider(LLMProvider):
        def __init__(self):
            super().__init__()
            self.calls = []

        async def chat(self, messages, tools=None, **kwargs):
            self.calls.append(
                {
                    "messages": [dict(message) for message in messages],
                    "tools": list(tools or []),
                    "kwargs": kwargs,
                }
            )
            if len(self.calls) == 1:
                return LLMResponse(
                    content="Let me check these sources.",
                    tool_calls=[
                        ToolCallRequest(
                            id="call-1",
                            name="lookup_fact",
                            arguments={"query": "current facts 1"},
                            tokens=3,
                        ),
                        ToolCallRequest(
                            id="call-2",
                            name="lookup_fact",
                            arguments={"query": "current facts 2"},
                            tokens=3,
                        ),
                        ToolCallRequest(
                            id="call-3",
                            name="lookup_fact",
                            arguments={"query": "current facts 3"},
                            tokens=3,
                        ),
                    ],
                    usage={"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
                )
            return LLMResponse(
                content="final answer from gathered tool results",
                usage={"prompt_tokens": 7, "completion_tokens": 5, "total_tokens": 12},
            )

        def get_default_model(self) -> str:
            return "fake-model"

    class _ToolRegistry:
        def __init__(self):
            self.execute_calls = []

        def get_definitions(self, **kwargs):
            return [
                {
                    "type": "function",
                    "function": {
                        "name": "lookup_fact",
                        "description": "Lookup fact",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ]

        async def execute(self, tool_name, arguments, **kwargs):
            self.execute_calls.append((tool_name, arguments, kwargs))
            return "tool result: useful context"

    provider = _ToolLimitProvider()
    tools = _ToolRegistry()
    bus = MessageBus()
    config = Config(storage_workspace=str(temp_dir))
    loop = make_loop(bus=bus, provider=provider, config=config, max_iterations=1)
    loop.tools = tools

    session_key = SessionKey(type="cli", channel_id="default", chat_id="session-limit")
    captured_turns = []
    final_content, _reasoning, tools_used, token_usage, iteration = await loop._run_agent_loop(
        messages=[{"role": "user", "content": "please answer with lookup"}],
        session_key=session_key,
        publish_events=False,
        captured_turns=captured_turns,
    )

    assert final_content == "final answer from gathered tool results"
    assert iteration == 1
    assert len(provider.calls) == 2
    assert provider.calls[0]["tools"]
    assert provider.calls[1]["tools"] == []
    assert provider.calls[1]["messages"][-1]["content"].startswith(
        "Tool-use iteration limit reached."
    )
    assert any(
        message.get("content") == "tool result: useful context"
        for message in provider.calls[1]["messages"]
    )
    assert len(tools.execute_calls) == 3
    assert tools.execute_calls[0][:2] == ("lookup_fact", {"query": "current facts 1"})
    assert [tool["tool_name"] for tool in tools_used] == [
        "lookup_fact",
        "lookup_fact",
        "lookup_fact",
    ]
    assert len(captured_turns) == 1
    assert captured_turns[0]["content"] == "Let me check these sources."
    assert [tool["tool_call_id"] for tool in captured_turns[0]["tool_calls"]] == [
        "call-1",
        "call-2",
        "call-3",
    ]
    assert [tool["result"] for tool in captured_turns[0]["tool_calls"]] == [
        "tool result: useful context",
        "tool result: useful context",
        "tool result: useful context",
    ]
    assert {
        key: token_usage[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens")
    } == {"prompt_tokens": 17, "completion_tokens": 7, "total_tokens": 24}


@pytest.mark.asyncio
async def test_agent_loop_evaluates_previous_response_outcome_before_openviking_precommit_clear(
    make_loop, temp_dir: Path, monkeypatch
):
    async def fake_run_agent_loop(self, **kwargs):
        return "final answer", None, [], {"prompt_tokens": 1, "completion_tokens": 1}, 1

    fake_langfuse = _FakeLangfuseClient()
    monkeypatch.setattr(AgentLoop, "_run_agent_loop", fake_run_agent_loop)
    monkeypatch.setattr(
        "vikingbot.agent.loop.LangfuseClient.get_instance",
        staticmethod(lambda: fake_langfuse),
    )

    bus = MessageBus()
    config = Config(
        storage_workspace=str(temp_dir),
        agents={
            "session_context_enabled": True,
            "commit_token_threshold": 1,
            "commit_keep_recent_count": 0,
        },
    )
    loop = make_loop(bus=bus, config=config)

    async def fake_precommit(session, msg):
        session.clear()
        await loop.sessions.save(session)

    monkeypatch.setattr(loop, "_maybe_commit_openviking_before_turn", fake_precommit)

    session_key = SessionKey(type="cli", channel_id="default", chat_id="session-precommit-clear")
    session = loop.sessions.get_or_create(session_key, skip_heartbeat=True)
    session.add_message(
        "assistant",
        "hello",
        sender_id="user-1",
        response_id="resp-123",
        timestamp="2026-04-30T00:00:00",
    )
    await loop.sessions.save(session)

    await loop._process_message(
        InboundMessage(
            session_key=session_key,
            sender_id="user-1",
            content="that did not help",
            timestamp=datetime.fromisoformat("2026-04-30T00:05:00"),
        )
    )

    outcome_event = await bus.consume_outbound()
    assert outcome_event.event_type == OutboundEventType.RESPONSE_OUTCOME_EVALUATED
    assert outcome_event.response_id == "resp-123"
    persisted_session = SessionManager(config.bot_data_path).get_or_create(
        session_key, skip_heartbeat=True
    )
    assert persisted_session.metadata["response_outcomes"]["resp-123"]["outcome_label"] == "reasked"


@pytest.mark.asyncio
async def test_agent_loop_build_prompt_history_uses_ov_context_plus_unsynced_tail(
    make_loop, temp_dir: Path, monkeypatch
):
    fake_ov_client = _FakeOVClient(
        context_payload={
            "latest_archive_overview": "Earlier summary",
            "messages": [
                {"role": "user", "content": "OV user turn"},
                {"role": "assistant", "parts": [{"type": "text", "text": "OV assistant turn"}]},
            ],
        }
    )

    monkeypatch.setattr(AgentLoop, "_get_ov_client", AsyncMock(return_value=fake_ov_client))

    bus = MessageBus()
    config = Config(
        storage_workspace=str(temp_dir),
        ov_server={"server_url": "http://127.0.0.1:1933"},
        agents={"session_context_enabled": True, "session_context_token_budget": 321},
    )
    loop = make_loop(bus=bus, config=config)

    session_key = SessionKey(type="cli", channel_id="default", chat_id="session-ov-history")
    session = loop.sessions.get_or_create(session_key, skip_heartbeat=True)
    session.add_message("user", "local synced user")
    session.add_message("assistant", "local synced assistant")
    session.add_message("user", "local unsynced user")
    session.add_message("assistant", "local unsynced assistant")
    session.metadata["openviking"] = {
        "session_id": "ov-session-1",
        "last_synced_local_index": 1,
    }

    history = await loop._build_prompt_history(session)

    assert fake_ov_client.context_calls == [("ov-session-1", 321)]
    assert [message["content"] for message in history] == [
        "[Earlier conversation summary]\nEarlier summary",
        "OV user turn",
        "OV assistant turn",
        "local unsynced user",
        "local unsynced assistant",
    ]


@pytest.mark.asyncio
async def test_agent_loop_build_prompt_history_falls_back_to_loaded_local_history_when_ov_empty(
    make_loop, temp_dir: Path, monkeypatch
):
    fake_ov_client = _FakeOVClient(context_payload={"messages": []})

    monkeypatch.setattr(AgentLoop, "_get_ov_client", AsyncMock(return_value=fake_ov_client))

    config = Config(
        storage_workspace=str(temp_dir),
        ov_server={"server_url": "http://127.0.0.1:1933"},
        agents={"session_context_enabled": True, "session_context_token_budget": 321},
    )
    loop = make_loop(config=config)

    session_key = SessionKey(type="cli", channel_id="default", chat_id="session-local-fallback")
    session = loop.sessions.get_or_create(session_key, skip_heartbeat=True)
    session.add_message("user", "persisted user")
    session.add_message("assistant", "persisted assistant")
    session.add_message("user", "current question")
    session.metadata["openviking"] = {
        "session_id": "ov-session-missing-context",
        "last_synced_local_index": 1,
    }
    await loop.sessions.save(session)

    restarted_sessions = SessionManager(config.bot_data_path)
    loaded_session = restarted_sessions.get_or_create(session_key, skip_heartbeat=True)
    history = await loop._build_prompt_history(loaded_session)

    assert fake_ov_client.context_calls == [("ov-session-missing-context", 321)]
    assert [message["content"] for message in history] == [
        "persisted user",
        "persisted assistant",
        "current question",
    ]
    assert loaded_session.metadata["openviking"]["last_synced_local_index"] == 1


@pytest.mark.asyncio
async def test_agent_loop_build_prompt_history_skips_tail_when_sync_cursor_is_past_local_messages(
    make_loop, temp_dir: Path, monkeypatch
):
    fake_ov_client = _FakeOVClient(
        context_payload={"messages": [{"role": "user", "content": "OV user turn"}]}
    )

    monkeypatch.setattr(AgentLoop, "_get_ov_client", AsyncMock(return_value=fake_ov_client))

    bus = MessageBus()
    config = Config(
        storage_workspace=str(temp_dir),
        ov_server={"server_url": "http://127.0.0.1:1933"},
        agents={"session_context_enabled": True, "session_context_token_budget": 321},
    )
    loop = make_loop(bus=bus, config=config)

    session_key = SessionKey(type="cli", channel_id="default", chat_id="session-ov-cursor-past")
    session = loop.sessions.get_or_create(session_key, skip_heartbeat=True)
    session.add_message("user", "local user")
    session.add_message("assistant", "local assistant")
    session.metadata["openviking"] = {
        "session_id": "ov-session-1",
        "last_synced_local_index": 20,
    }

    history = await loop._build_prompt_history(session)

    assert [message["content"] for message in history] == ["OV user turn"]


@pytest.mark.asyncio
async def test_agent_loop_build_prompt_history_enforces_token_budget_for_live_tool_outputs(
    make_loop, temp_dir: Path, monkeypatch
):
    tool_output = "x" * 10_000
    fake_ov_client = _FakeOVClient(
        context_payload={
            "messages": [
                {"role": "user", "parts": [{"type": "text", "text": "original query"}]},
                *[
                    {
                        "role": "assistant",
                        "parts": [
                            {"type": "text", "text": f"turn {index}"},
                            {"type": "tool", "tool_output": tool_output},
                        ],
                    }
                    for index in range(10)
                ],
                {"role": "assistant", "parts": [{"type": "text", "text": "final answer"}]},
            ]
        }
    )

    monkeypatch.setattr(AgentLoop, "_get_ov_client", AsyncMock(return_value=fake_ov_client))

    loop = make_loop(
        config=Config(
            storage_workspace=str(temp_dir),
            ov_server={"server_url": "http://127.0.0.1:1933"},
            agents={"session_context_enabled": True, "session_context_token_budget": 3000},
        )
    )
    session = loop.sessions.get_or_create(
        SessionKey(type="cli", channel_id="default", chat_id="session-large-tools"),
        skip_heartbeat=True,
    )
    session.metadata["openviking"] = {
        "session_id": "ov-session-large-tools",
        "last_synced_local_index": -1,
    }

    history = await loop._build_prompt_history(session)

    assert fake_ov_client.context_calls == [("ov-session-large-tools", 3000)]
    assert sum(loop._history_message_tokens(message) for message in history) <= 3000
    assert history[-1]["content"] == "final answer"
    assert sum(str(message.get("content", "")).count("x") for message in history) < 100_000
    assert any("History truncated" in str(message.get("content", "")) for message in history)


@pytest.mark.asyncio
async def test_agent_loop_build_prompt_history_preserves_anchor_when_final_needs_truncation(
    make_loop, temp_dir: Path, monkeypatch
):
    fake_ov_client = _FakeOVClient(
        context_payload={
            "messages": [
                {"role": "user", "parts": [{"type": "text", "text": "u" * 400}]},
                {"role": "assistant", "parts": [{"type": "text", "text": "a" * 7600}]},
            ]
        }
    )

    monkeypatch.setattr(AgentLoop, "_get_ov_client", AsyncMock(return_value=fake_ov_client))

    loop = make_loop(
        config=Config(
            storage_workspace=str(temp_dir),
            ov_server={"server_url": "http://127.0.0.1:1933"},
            agents={"session_context_enabled": True, "session_context_token_budget": 3000},
        )
    )
    session = loop.sessions.get_or_create(
        SessionKey(type="cli", channel_id="default", chat_id="session-long-final"),
        skip_heartbeat=True,
    )
    session.metadata["openviking"] = {
        "session_id": "ov-session-long-final",
        "last_synced_local_index": -1,
    }

    history = await loop._build_prompt_history(session)

    assert fake_ov_client.context_calls == [("ov-session-long-final", 3000)]
    assert [message["role"] for message in history] == ["user", "assistant"]
    assert history[0]["content"] == "u" * 400
    assert "History truncated" in history[1]["content"]
    assert sum(loop._history_message_tokens(message) for message in history) <= 3000


@pytest.mark.parametrize(
    "token_threshold, pending_tokens, memory_window",
    [pytest.param(100, 100, 50, id="token-budget"), pytest.param(1000, 0, 3, id="message-window")],
)
async def test_agent_loop_commits_before_model_at_context_limit(
    make_loop, temp_dir: Path, monkeypatch, token_threshold, pending_tokens, memory_window
):
    events = []

    async def fake_execute_hooks(context, **kwargs):
        events.append(
            (
                "hook",
                kwargs["force_commit"],
                kwargs.get("keep_recent_turn_count"),
                kwargs.get("commit_message_threshold"),
            )
        )
        session = kwargs["session"]
        state = session.metadata.setdefault("openviking", {})
        state["last_sync_status"] = "success"
        state["last_pending_tokens"] = 0
        state["last_commit_performed"] = bool(kwargs["force_commit"])
        state["last_commit_local_index"] = len(session.messages) - 1
        if not kwargs["force_commit"]:
            state["last_synced_local_index"] = len(session.messages) - 1
        return kwargs

    async def fake_get_ov_client(self, session_key, openviking_connection=None, actor_peer_id=None):
        del self, session_key, openviking_connection, actor_peer_id

        class _Client:
            async def get_session_context(self, session_id, token_budget):
                events.append(("context", session_id, token_budget))
                return {"messages": []}

            async def close(self):
                return None

        return _Client()

    async def fake_run_agent_loop(self, **kwargs):
        events.append(("model", [message.get("content") for message in kwargs["messages"]]))
        return "final answer", None, [], {"prompt_tokens": 1, "completion_tokens": 1}, 1

    fake_langfuse = _FakeLangfuseClient()
    monkeypatch.setattr(loop_module.hook_manager, "execute_hooks", fake_execute_hooks)
    monkeypatch.setattr(AgentLoop, "_get_ov_client", fake_get_ov_client)
    monkeypatch.setattr(AgentLoop, "_run_agent_loop", fake_run_agent_loop)
    monkeypatch.setattr(
        "vikingbot.agent.loop.LangfuseClient.get_instance",
        staticmethod(lambda: fake_langfuse),
    )

    bus = MessageBus()
    config = Config(
        storage_workspace=str(temp_dir),
        ov_server={"server_url": "http://127.0.0.1:1933"},
        agents={
            "session_context_enabled": True,
            "session_context_token_budget": 321,
            "commit_token_threshold": token_threshold,
            "commit_keep_recent_turn_count": 2,
        },
    )
    loop = make_loop(bus=bus, config=config, memory_window=memory_window)

    session_key = SessionKey(type="cli", channel_id="default", chat_id="session-precommit")
    session = loop.sessions.get_or_create(session_key, skip_heartbeat=True)
    session.add_message("user", "old user", sender_id="user-1")
    session.add_message("assistant", "old assistant", sender_id="user-1")
    session.metadata["openviking"] = {
        "session_id": session_key.safe_name(),
        "last_synced_local_index": 1,
        "last_pending_tokens": pending_tokens,
        "last_commit_local_index": -1,
    }
    await loop.sessions.save(session)

    response = await loop._process_message(
        InboundMessage(
            session_key=session_key,
            sender_id="user-1",
            content="new question",
            timestamp=datetime.fromisoformat("2026-04-30T00:05:00"),
        )
    )

    assert response is not None
    assert response.content == "final answer"
    assert events[0] == ("hook", True, 2, loop.memory_window)
    assert events[1] == ("context", "cli__default__session-precommit", 321)
    assert events[2][0] == "model"
    assert events[-1] == ("hook", False, None, loop.memory_window)
    persisted_session = SessionManager(config.bot_data_path).get_or_create(
        session_key, skip_heartbeat=True
    )
    assert [message["content"] for message in persisted_session.messages] == [
        "new question",
        "final answer",
    ]
    assert persisted_session.metadata["openviking"]["last_synced_local_index"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "last_commit_index, expected_commits",
    [
        pytest.param(-1, [True], id="uncommitted-history"),
        pytest.param(1, [], id="already-committed"),
    ],
)
async def test_agent_loop_precommit_counts_messages_since_last_commit(
    make_loop, temp_dir: Path, monkeypatch, last_commit_index, expected_commits
):
    calls = []

    async def fake_execute_hooks(context, **kwargs):
        calls.append(kwargs)
        session = kwargs["session"]
        session.metadata.setdefault("openviking", {})["last_sync_status"] = "success"
        return kwargs

    monkeypatch.setattr(loop_module.hook_manager, "execute_hooks", fake_execute_hooks)

    bus = MessageBus()
    config = Config(
        storage_workspace=str(temp_dir),
        ov_server={"server_url": "http://127.0.0.1:1933"},
        agents={
            "session_context_enabled": True,
            "commit_token_threshold": 1000,
            "commit_keep_recent_turn_count": 2,
        },
    )
    loop = make_loop(bus=bus, config=config, memory_window=3)

    session_key = SessionKey(type="cli", channel_id="default", chat_id="session-window-no-repeat")
    session = loop.sessions.get_or_create(session_key, skip_heartbeat=True)
    session.add_message("user", "old user", sender_id="user-1")
    session.add_message("assistant", "old assistant", sender_id="user-1")
    session.metadata["openviking"] = {
        "session_id": session_key.safe_name(),
        "last_pending_tokens": 0,
        "last_commit_local_index": last_commit_index,
    }

    await loop._maybe_commit_openviking_before_turn(
        session,
        InboundMessage(
            session_key=session_key,
            sender_id="user-1",
            content="new question",
            timestamp=datetime.fromisoformat("2026-04-30T00:05:00"),
        ),
    )

    assert [call["force_commit"] for call in calls] == expected_commits


@pytest.mark.asyncio
async def test_agent_loop_post_turn_clears_local_session_after_openviking_commit(
    make_loop, temp_dir: Path, monkeypatch
):
    calls = []

    async def fake_execute_hooks(context, **kwargs):
        calls.append(kwargs)
        session = kwargs["session"]
        state = session.metadata.setdefault("openviking", {})
        state["last_sync_status"] = "success"
        state["last_commit_performed"] = True
        state["last_synced_local_index"] = len(session.messages) - 1
        state["last_commit_local_index"] = len(session.messages) - 1
        return kwargs

    async def fake_run_agent_loop(self, **kwargs):
        return "final answer", None, [], {"prompt_tokens": 1, "completion_tokens": 1}, 1

    fake_langfuse = _FakeLangfuseClient()
    monkeypatch.setattr(loop_module.hook_manager, "execute_hooks", fake_execute_hooks)
    monkeypatch.setattr(AgentLoop, "_run_agent_loop", fake_run_agent_loop)
    monkeypatch.setattr(
        "vikingbot.agent.loop.LangfuseClient.get_instance",
        staticmethod(lambda: fake_langfuse),
    )

    bus = MessageBus()
    config = Config(
        storage_workspace=str(temp_dir),
        ov_server={"server_url": "http://127.0.0.1:1933"},
        agents={"session_context_enabled": True},
    )
    loop = make_loop(bus=bus, config=config, memory_window=3)

    session_key = SessionKey(type="cli", channel_id="default", chat_id="session-post-clear")
    await loop._process_message(
        InboundMessage(
            session_key=session_key,
            sender_id="user-1",
            content="new question",
            timestamp=datetime.fromisoformat("2026-04-30T00:05:00"),
        )
    )

    persisted_session = SessionManager(config.bot_data_path).get_or_create(
        session_key, skip_heartbeat=True
    )
    assert calls[-1]["force_commit"] is False
    assert calls[-1]["commit_message_threshold"] == 3
    assert persisted_session.messages == []
    assert persisted_session.metadata["openviking"]["session_id"] == (
        make_openviking_storage_session_id(session_key.safe_name())
    )
    assert persisted_session.metadata["openviking"]["last_synced_local_index"] == -1
    assert persisted_session.metadata["openviking"]["last_commit_local_index"] == -1


@pytest.mark.asyncio
async def test_quiet_model_call_uses_chat_not_stream(make_loop):
    provider = _RecordingProvider()

    async def forbidden_stream(**kwargs):
        raise AssertionError("Quiet calls must not use streaming")
        yield

    provider.chat_stream = forbidden_stream
    loop = make_loop(provider=provider)
    response, content_streamed, reasoning_streamed = await loop._chat_with_stream_events(
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        session_key=SessionKey(type="cli", channel_id="test", chat_id="quiet"),
        publish_events=False,
    )
    assert response.content == "ok"
    assert len(provider.calls) == 1
    assert not content_streamed and not reasoning_streamed


@pytest.mark.asyncio
async def test_visible_model_call_keeps_streaming(make_loop):
    from vikingbot.providers.base import LLMStreamEvent

    provider = _RecordingProvider()
    provider.chat = AsyncMock(side_effect=AssertionError("Visible call must stream"))

    async def stream(**kwargs):
        yield LLMStreamEvent(type="content_delta", content="hello")
        yield LLMStreamEvent(type="response", response=LLMResponse(content="hello"))

    provider.chat_stream = stream
    loop = make_loop(provider=provider)
    loop.bus.publish_outbound = AsyncMock()
    response, streamed, _ = await loop._chat_with_stream_events(
        messages=[],
        tools=[],
        session_key=SessionKey(type="cli", channel_id="test", chat_id="visible"),
        publish_events=True,
    )
    assert response.content == "hello" and streamed
    provider.chat.assert_not_awaited()
    assert loop.bus.publish_outbound.await_args.args[0].content == "hello"


@pytest.mark.asyncio
@pytest.mark.parametrize("publish_events", [False, True])
@pytest.mark.parametrize("max_iterations", [1, 2])
async def test_model_error_never_routes_text_or_executes_its_tools(
    make_loop, publish_events, max_iterations
):
    from vikingbot.agent.loop import AgentLLMError

    provider = _MediaProvider([])
    provider.responses = iter(
        [
            LLMResponse(content=None, tool_calls=[_image_call("first")], usage={"total_tokens": 3}),
            LLMResponse(
                content="Error calling LLM: failed",
                finish_reason="error",
                tool_calls=[_image_call("must-not-run")],
            ),
        ]
    )
    loop = make_loop(provider=provider, max_iterations=max_iterations)
    registry = _ImageRegistry()
    registry.execute_detailed = AsyncMock(wraps=registry.execute_detailed)
    route = AsyncMock()
    messages = [{"role": "user", "content": "read"}]
    with pytest.raises(AgentLLMError) as captured:
        await loop._run_agent_loop(
            messages=messages,
            session_key=SessionKey(type="cli", channel_id="test", chat_id="failure"),
            publish_events=publish_events,
            tool_registry=registry,
            on_plain_text=route,
        )
    route.assert_not_awaited()
    registry.execute_detailed.assert_awaited_once()
    assert len(captured.value.tools_used) == 1
    assert captured.value.usage["total_tokens"] == 3
    assert captured.value.iteration == max_iterations
    assert not any("Error calling LLM" in str(message) for message in messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt_fails", [False, True])
async def test_tool_receipt_runs_after_result_enters_model_context(make_loop, receipt_fails):
    from vikingbot.agent.tools.base import Tool

    events = []

    class AdviceTool(Tool):
        name = "ask_for_advice"
        description = "Review the current task"
        parameters = {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        }

        async def execute(self, tool_context, query):
            assert query == "Review eligibility"
            assert tool_context.messages[0]["content"] == "actual user facts"
            events.append("execute")

            async def receipt(result):
                assert result == "applicable advice"
                assert events[-1] == "appended"
                events.append("ack")
                if receipt_fails:
                    raise RuntimeError("temporary acknowledgement failure")

            tool_context.on_result_added = receipt
            return "applicable advice"

    provider = _MediaProvider(
        [
            [
                ToolCallRequest(
                    id="advice-1",
                    name="ask_for_advice",
                    arguments={"query": "Review eligibility"},
                    tokens=0,
                )
            ]
        ]
    )
    loop = make_loop(provider=provider)
    loop.tools.register(AdviceTool())
    append = loop.context.add_tool_result

    def observed_append(*args, **kwargs):
        result = append(*args, **kwargs)
        events.append("appended")
        return result

    loop.context.add_tool_result = observed_append
    await loop._run_agent_loop(
        messages=[{"role": "user", "content": "actual user facts"}],
        session_key=SessionKey(type="cli", channel_id="default", chat_id="advice-test"),
        publish_events=False,
        ov_tools_enable=False,
        inject_write_experience=False,
    )
    assert events == ["execute", "appended", "ack"]
    assert len(provider.calls) == 2
    assert any(m.get("content") == "applicable advice" for m in provider.calls[1])


@pytest.mark.asyncio
@pytest.mark.parametrize("max_iterations", [0, 1, 2])
async def test_initial_request_callback_observes_only_first_provider_call(
    make_loop, max_iterations
):
    class Provider(_RecordingProvider):
        async def chat(self, messages, tools=None, **kwargs):
            self.calls.append(copy.deepcopy({"messages": messages, "tools": tools}))
            if len(self.calls) == 1 and tools:
                return LLMResponse(content=None, tool_calls=[_image_call("first-call")])
            return LLMResponse(content="done")

    provider = Provider()
    loop = make_loop(provider=provider, max_iterations=max_iterations)
    captured = []
    messages = [{"role": "system", "content": "initial system"}, {"role": "user", "content": "hi"}]

    result = await loop._run_agent_loop(
        messages=messages,
        session_key=SessionKey(type="cli", channel_id="test", chat_id="capture"),
        publish_events=False,
        tool_registry=_ImageRegistry(),
        inject_write_experience=False,
        status_note_provider=AsyncMock(return_value="effective status note"),
        on_initial_llm_request=lambda messages, tools: captured.append(
            copy.deepcopy({"messages": messages, "tools": tools})
        ),
    )

    assert result[0] == "done"
    assert captured == [provider.calls[0]]
    if max_iterations:
        assert captured[0]["messages"][-1]["content"] == "effective status note"
        assert captured[0]["tools"][0]["function"]["name"] == "read_image"
        assert len(provider.calls) == 2
        assert len(captured[0]["messages"]) < len(messages)
    else:
        assert captured[0]["tools"] == []
    messages[0]["content"] = "later mutation"
    assert captured[0]["messages"][0]["content"] == "initial system"


@pytest.mark.asyncio
async def test_automatic_advice_ack_failure_does_not_abort_model_call(make_loop):
    provider = _RecordingProvider()
    loop = make_loop(provider=provider, max_iterations=1)
    reminder = "[Experience Reminder] review current facts"

    async def guidance(messages, *, before_tools=False):
        if before_tools:
            return
        yield reminder

    result = await loop._run_agent_loop(
        messages=[{"role": "user", "content": "request"}],
        session_key=SessionKey(type="cli", channel_id="test", chat_id="ack-error"),
        publish_events=False,
        inject_write_experience=False,
        experience_context_provider=guidance,
        experience_context_injected=AsyncMock(
            side_effect=ValueError("delivery persistence failed")
        ),
    )
    assert result[0] == "ok"
    assert {"role": "user", "content": reminder} in provider.calls[0][1]["messages"]


@pytest.mark.asyncio
async def test_identity_owning_provider_can_deliver_identical_markdown_for_distinct_advice(
    make_loop,
):
    provider = _MediaProvider([[_image_call("call-1")]])
    loop = make_loop(provider=provider, max_iterations=2)
    note = "[Experience Reminder]\n\n### 建议 1\n\n**适用条件**\n- 当前条件\n\n**建议**\n核对范围。"
    pending = ["experience-a", "experience-b"]
    delivered = []

    async def reminders(messages, *, before_tools=False):
        if before_tools:
            return
        if pending:
            yield note

    async def record(text):
        assert text == note
        delivered.append(pending.pop(0))

    captured = []
    final, *_ = await loop._run_agent_loop(
        messages=[{"role": "user", "content": "read it"}],
        session_key=SessionKey(type="cli", channel_id="default", chat_id="different-identities"),
        publish_events=False,
        tool_registry=_ImageRegistry(),
        experience_context_provider=reminders,
        experience_context_injected=record,
        captured_experience_messages=captured,
    )
    assert final == "done"
    assert delivered == ["experience-a", "experience-b"]
    assert len(captured) == 2
    assert sum(message.get("content") == note for message in provider.calls[1]) == 2


class _CheckpointRegistry:
    def __init__(self):
        self.calls = []

    def get_definitions(self, **kwargs):
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": name,
                    "parameters": {"type": "object", "properties": {}},
                },
            }
            for name in ("read_record", "write_record", "transfer_to_human_agents", "done")
        ]

    async def execute_detailed(self, name, params, **kwargs):
        self.calls.append((name, dict(params)))
        return ToolExecutionResult(result="Success", effective_params=dict(params))


@pytest.mark.asyncio
@pytest.mark.parametrize("critical_tool", ["write_record", "transfer_to_human_agents", "done"])
async def test_new_advice_defers_entire_tool_batch_without_orphan_calls(make_loop, critical_tool):
    registry = _CheckpointRegistry()
    provider = _MediaProvider(
        [
            [
                ToolCallRequest("old-read", "read_record", {}, 0),
                ToolCallRequest("old-action", critical_tool, {"version": "old"}, 0),
            ],
            [ToolCallRequest("new-action", critical_tool, {"version": "revised"}, 0)],
        ]
    )
    loop = make_loop(provider=provider, max_iterations=4)
    delivered = []
    proposal_states = []
    note = "[Experience Reminder] Review the latest facts before proceeding."

    async def guidance(messages, *, before_tools=False):
        if before_tools:
            proposal_states.append(copy.deepcopy(messages))
            if not delivered:
                yield note

    async def acknowledge(value):
        delivered.append(value)

    captured = []
    result = await loop._run_agent_loop(
        messages=[{"role": "user", "content": "Handle this record"}],
        session_key=SessionKey(type="cli", channel_id="test", chat_id="checkpoint"),
        publish_events=False,
        tool_registry=registry,
        stop_tool_names=["done"],
        inject_write_experience=False,
        experience_context_provider=guidance,
        experience_context_injected=acknowledge,
        captured_experience_messages=captured,
    )
    assert registry.calls == [(critical_tool, {"version": "revised"})]
    assert delivered == [note] and captured[0]["after_tool_count"] == 0
    assert proposal_states[0][-1]["tool_calls"][1]["id"] == "old-action"
    second_request = provider.calls[1]
    assert any(m.get("content") == note for m in second_request)
    assert not any(m.get("tool_calls") or m.get("role") == "tool" for m in second_request)
    assert all(t["tool_call_id"] != "old-action" for t in result[2])
    # Only the accepted proposal gets tool results and can end the task.
    assert result[2][0]["tool_call_id"] == "new-action"


@pytest.mark.asyncio
async def test_checkpoint_without_new_advice_executes_without_extra_model_round(make_loop):
    registry = _CheckpointRegistry()
    provider = _MediaProvider([[ToolCallRequest("write", "write_record", {}, 0)]])
    loop = make_loop(provider=provider, max_iterations=3)
    stages = []

    async def no_advice(messages, *, before_tools=False):
        stages.append(before_tools)
        yield ""

    await loop._run_agent_loop(
        messages=[{"role": "user", "content": "Update"}],
        session_key=SessionKey(type="cli", channel_id="test", chat_id="no-advice"),
        publish_events=False,
        tool_registry=registry,
        inject_write_experience=False,
        experience_context_provider=no_advice,
    )
    assert registry.calls == [("write_record", {})]
    assert len(provider.calls) == 2
    assert stages == [False, True, False]


@pytest.mark.asyncio
async def test_checkpoint_cancellation_prevents_tools_from_executing(make_loop):
    import asyncio

    registry = _CheckpointRegistry()
    provider = _MediaProvider([[ToolCallRequest("write", "write_record", {}, 0)]])
    loop = make_loop(provider=provider, max_iterations=3)

    async def cancelled(messages, *, before_tools=False):
        if before_tools:
            raise asyncio.CancelledError()
        yield ""

    with pytest.raises(asyncio.CancelledError):
        await loop._run_agent_loop(
            messages=[{"role": "user", "content": "Update"}],
            session_key=SessionKey(type="cli", channel_id="test", chat_id="cancelled-check"),
            publish_events=False,
            tool_registry=registry,
            inject_write_experience=False,
            experience_context_provider=cancelled,
        )
    assert registry.calls == []


@pytest.mark.asyncio
async def test_deferred_write_is_not_executed_when_iteration_budget_is_exhausted(make_loop):
    registry = _CheckpointRegistry()
    provider = _MediaProvider([[ToolCallRequest("write", "write_record", {}, 0)]])
    loop = make_loop(provider=provider, max_iterations=1)

    async def new_advice(messages, *, before_tools=False):
        if before_tools:
            yield "[Experience Reminder] Reconsider first"

    await loop._run_agent_loop(
        messages=[{"role": "user", "content": "Update"}],
        session_key=SessionKey(type="cli", channel_id="test", chat_id="exhausted-check"),
        publish_events=False,
        tool_registry=registry,
        inject_write_experience=False,
        experience_context_provider=new_advice,
    )
    assert registry.calls == []


@pytest.mark.asyncio
async def test_real_tau2_runtime_checks_user_and_write_boundaries_and_dedups_replan(make_loop):
    from types import SimpleNamespace

    from benchmark.tau2.train.dag_experience_runtime import Tau2DagExperienceRuntime

    registry = _CheckpointRegistry()
    provider = _MediaProvider(
        [
            [ToolCallRequest("read", "read_record", {}, 0)],
            [ToolCallRequest("old-write", "write_record", {"version": "old"}, 0)],
            [ToolCallRequest("new-write", "write_record", {"version": "new"}, 0)],
            [ToolCallRequest("done", "done", {}, 0)],
        ]
    )
    loop = make_loop(provider=provider, max_iterations=5)
    runtime = Tau2DagExperienceRuntime(review_tools={"write_record"})
    key = SessionKey(type="cli", channel_id="test", chat_id="real-checkpoint")
    runtime.bind_session(loop.sessions, key)
    runtime.record_user_message("Update the record")
    instruction = {
        "experience_uri": "viking://user/default/memories/experiences/review.md",
        "slot_name": "review",
        "provider": {"type": "TellAgent", "instruction": "Review the new facts"},
        "conditions": [],
    }
    payload = {
        "instructions": [instruction],
        "matched_experience_uris": [instruction["experience_uri"]],
    }
    runtime.client = SimpleNamespace(
        search_advice=AsyncMock(side_effect=[{}, payload, payload, {}]), close=AsyncMock()
    )
    captures = []
    try:
        await loop._run_agent_loop(
            messages=[{"role": "user", "content": "Update the record"}],
            session_key=key,
            publish_events=False,
            tool_registry=registry,
            stop_tool_names=["done"],
            inject_write_experience=False,
            experience_context_provider=runtime.recall,
            experience_context_injected=runtime.record_injected,
            captured_experience_messages=captures,
        )
        assert registry.calls == [
            ("read_record", {}),
            ("write_record", {"version": "new"}),
            ("done", {}),
        ]
        assert len(provider.calls) == 4
        assert [r["trigger"] for r in runtime.recalls] == ["user_message"] + ["before_action"] * 3
        assert [r["status"] for r in runtime.recalls] == [
            "no_match",
            "advice",
            "no_applicable_advice",
            "no_match",
        ]
        assert len(captures) == 1 and captures[0]["after_tool_count"] == 1
        checkpoint = runtime.client.search_advice.call_args_list[1].args[0]
        assert checkpoint[-1]["tool_calls"][0]["id"] == "old-write"
        assert any(m.get("role") == "tool" and m.get("name") == "read_record" for m in checkpoint)
        assert not any(
            call["id"] == "old-write"
            for message in provider.calls[2]
            for call in message.get("tool_calls", [])
        )
        assert len([e for e in runtime.events if e.get("event") == "reminder_delivered"]) == 1
    finally:
        await runtime.close()


@pytest.fixture
def tau2_feedback_environment():
    import json
    from types import SimpleNamespace

    from benchmark.tau2.common.tau2_env.tau2_environment import _GymTau2BenchEnv

    adapter = _GymTau2BenchEnv.__new__(_GymTau2BenchEnv)
    adapter.terminated = False
    writes = []
    deliveries = []
    replies = iter([("Please add two checked bags.", False), ("###STOP###", True)])

    def step(action):
        if action.startswith("{"):
            name = json.loads(action)["name"]
            assert name != "done", "Active conversations must reject done before Gym sees it"
            writes.append(name)
            return "tool: updated", 0.0, False, False, {}
        deliveries.append(action)
        text, terminal = next(replies)
        return f"user: {text}", 0.0, terminal, False, {}

    adapter.env = SimpleNamespace(step=step, _orchestrator=None)
    schemas = [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": name,
                "parameters": {
                    "type": "object",
                    "properties": {"content": {"type": "string"}}
                    if name == "communicate_with_user"
                    else {},
                },
            },
        }
        for name in [
            "update_reservation_flights",
            "update_reservation_baggages",
            "communicate_with_user",
            "done",
        ]
    ]
    provider = SimpleNamespace(call_tool=adapter.tool_call, list_openai_tools=lambda: schemas)
    return adapter, provider, writes, deliveries


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", ["plain", "tool"])
@pytest.mark.parametrize("done_first", [False, True])
@pytest.mark.parametrize("external_stop", [False, True])
async def test_tau2_done_cannot_skip_result_feedback_and_followup(
    make_loop, monkeypatch, tau2_feedback_environment, delivery, done_first, external_stop
):
    from benchmark.tau2.train import rollout_executor_vikingbot as tau2

    adapter, environment, writes, deliveries = tau2_feedback_environment

    def write_and_done(name, index):
        calls = [
            ToolCallRequest(f"write-{index}", name, {}, 0),
            ToolCallRequest(f"done-{index}", "done", {}, 0),
        ]
        return LLMResponse(content=None, tool_calls=list(reversed(calls)) if done_first else calls)

    def report(text, index):
        if delivery == "plain":
            return LLMResponse(content=text)
        return LLMResponse(
            content=None,
            tool_calls=[
                ToolCallRequest(f"report-{index}", "communicate_with_user", {"content": text}, 0)
            ],
        )

    responses = [
        write_and_done("update_reservation_flights", 1),
        report("Your flight changes are complete. Anything else?", 1),
        write_and_done("update_reservation_baggages", 2),
        report("Your two checked bags have been added.", 2),
    ]
    if delivery == "tool" and not external_stop:
        responses.append(
            LLMResponse(content=None, tool_calls=[ToolCallRequest("final-done", "done", {}, 0)])
        )
    provider = _MediaProvider([])
    provider.responses = iter(responses)
    loop = make_loop(provider=provider, max_iterations=10)
    monkeypatch.setattr(
        loop.context,
        "build_messages",
        AsyncMock(return_value=[{"role": "user", "content": "Change my flight"}]),
    )
    tau2._configure_tools(loop, environment, keep_default_tools=False)

    result = await tau2._run_agent(
        agent=loop,
        system_prompt="Airline policy",
        user_prompt="Change my flight",
        session_key=SessionKey(type="cli", channel_id="test", chat_id="done-feedback"),
        sender_id="customer",
        keep_default_tools=False,
        should_stop=(lambda: adapter.terminated) if external_stop else None,
    )

    assert len(provider.calls) == len(responses)
    assert writes == ["update_reservation_flights", "update_reservation_baggages"]
    assert len(deliveries) == 2
    assert adapter.terminated
    assert any(
        "Please add two checked bags." in str(message.get("content"))
        for message in provider.calls[2]
    )
    done_results = [item for item in result[2] if item["tool_name"] == "done"]
    assert len([item for item in done_results if not item["execute_success"]]) == 2
    assert all(item["result"].startswith("Error:") for item in done_results[:2])
    assert sum(bool(item["execute_success"]) for item in done_results) == int(
        delivery == "tool" and not external_stop
    )
    if external_stop:
        assert result[0] is None


@pytest.mark.asyncio
async def test_rejected_tau2_done_preserves_unfinished_fallback_instead_of_success(
    make_loop, monkeypatch, tau2_feedback_environment
):
    from benchmark.tau2.train import rollout_executor_vikingbot as tau2

    adapter, environment, writes, deliveries = tau2_feedback_environment
    provider = _MediaProvider([])
    provider.responses = iter(
        [
            LLMResponse(content=None, tool_calls=[ToolCallRequest("premature", "done", {}, 0)]),
            LLMResponse(content=""),
            LLMResponse(content="The requested work remains unfinished."),
        ]
    )
    loop = make_loop(provider=provider, max_iterations=2)
    monkeypatch.setattr(
        loop.context,
        "build_messages",
        AsyncMock(return_value=[{"role": "user", "content": "Change my flight"}]),
    )
    tau2._configure_tools(loop, environment, keep_default_tools=False)

    result = await tau2._run_agent(
        agent=loop,
        system_prompt="Airline policy",
        user_prompt="Change my flight",
        session_key=SessionKey(type="cli", channel_id="test", chat_id="blocked-done-limit"),
        sender_id="customer",
        keep_default_tools=False,
    )

    assert len(provider.calls) == 3
    assert result[0] == "The requested work remains unfinished."
    assert result[2][-1]["execute_success"] is False
    assert not adapter.terminated and not writes and not deliveries


@pytest.mark.asyncio
@pytest.mark.parametrize("max_iterations", [1, 100])
@pytest.mark.parametrize("tool_result", ["updated", "Task Terminated", "Error: simulator failed"])
async def test_external_stop_records_tool_result_without_next_model_or_fallback(
    make_loop, max_iterations, tool_result
):
    provider = _MediaProvider([[_image_call("call-1")]])
    loop = make_loop(provider=provider, max_iterations=max_iterations)
    state = {"terminated": False}
    registry = _ImageRegistry()
    received = AsyncMock()

    async def execute(name, params, **kwargs):
        state["terminated"] = True
        return ToolExecutionResult(tool_result, params, on_result_added=received)

    registry.execute_detailed = AsyncMock(side_effect=execute)
    recall_calls = []

    async def advice(messages, *, before_tools=False):
        recall_calls.append(before_tools)
        if False:
            yield ""

    captured = []
    final, reasoning, tools, _usage, iterations = await loop._run_agent_loop(
        messages=[{"role": "user", "content": "run task"}],
        session_key=SessionKey(type="cli", channel_id="test", chat_id="external-stop"),
        publish_events=False,
        stop_tool_names=["done"],
        tool_registry=registry,
        inject_write_experience=False,
        experience_context_provider=advice,
        should_stop=lambda: state["terminated"],
        captured_turns=captured,
    )

    assert final is None and reasoning is None
    assert iterations == len(provider.calls) == 1
    assert recall_calls == [False, True]
    assert len(tools) == 1 and tools[0]["tool_name"] == "read_image"
    assert tools[0]["result"] == tool_result
    assert tools[0]["execute_success"] == (not tool_result.startswith("Error:"))
    assert captured[0]["tool_calls"] == tools
    received.assert_awaited_once_with(tool_result)


@pytest.mark.asyncio
async def test_external_stop_before_first_iteration_skips_compaction_advice_and_model(make_loop):
    provider = _RecordingProvider()
    loop = make_loop(provider=provider, max_iterations=100)
    loop._compact_tool_loop = AsyncMock()
    status = AsyncMock()
    initial_request = Mock()
    recalls = []

    async def advice(messages, *, before_tools=False):
        recalls.append(before_tools)
        if False:
            yield ""

    final, reasoning, tools, usage, iterations = await loop._run_agent_loop(
        messages=[{"role": "user", "content": "already ended"}],
        session_key=SessionKey(type="cli", channel_id="test", chat_id="already-ended"),
        publish_events=False,
        should_stop=lambda: True,
        context_compact_budget=1,
        status_note_provider=status,
        experience_context_provider=advice,
        on_initial_llm_request=initial_request,
    )
    assert final is None and reasoning is None
    assert tools == [] and iterations == 0 and usage["total_tokens"] == 0
    assert provider.calls == [] and recalls == []
    loop._compact_tool_loop.assert_not_awaited()
    status.assert_not_awaited()
    initial_request.assert_not_called()


@pytest.mark.asyncio
async def test_terminal_words_without_terminal_state_do_not_stop_agent(make_loop):
    provider = _MediaProvider([[_image_call("call-1")]])
    loop = make_loop(provider=provider, max_iterations=100)
    registry = _ImageRegistry()
    registry.execute_detailed = AsyncMock(return_value=ToolExecutionResult("Task Terminated", {}))
    final, _reasoning, tools, _usage, iterations = await loop._run_agent_loop(
        messages=[{"role": "user", "content": "read the document"}],
        session_key=SessionKey(type="cli", channel_id="test", chat_id="terminal-words"),
        publish_events=False,
        tool_registry=registry,
        should_stop=lambda: False,
        inject_write_experience=False,
    )
    assert final == "done"  # The next normal model response, not a fabricated done tool.
    assert len(provider.calls) == iterations == 2
    assert len(tools) == 1 and tools[0]["result"] == "Task Terminated"


@pytest.mark.asyncio
async def test_external_stop_collects_all_inflight_tool_results(make_loop):
    import asyncio

    provider = _MediaProvider([[_image_call("fast", "fast"), _image_call("slow", "slow")]])
    loop = make_loop(provider=provider, max_iterations=1)
    state = {"terminated": False}
    finished = []
    registry = _ImageRegistry()

    async def execute(name, params, **kwargs):
        if params["label"] == "slow":
            await asyncio.sleep(0.01)
        else:
            state["terminated"] = True
        finished.append(params["label"])
        return ToolExecutionResult(params["label"], params)

    registry.execute_detailed = AsyncMock(side_effect=execute)
    captured = []
    final, _reasoning, tools, _usage, iterations = await loop._run_agent_loop(
        messages=[{"role": "user", "content": "run both"}],
        session_key=SessionKey(type="cli", channel_id="test", chat_id="terminal-batch"),
        publish_events=False,
        tool_registry=registry,
        should_stop=lambda: state["terminated"],
        inject_write_experience=False,
        captured_turns=captured,
    )
    assert final is None and iterations == len(provider.calls) == 1
    assert finished == ["fast", "slow"]
    assert [item["tool_call_id"] for item in tools] == ["fast", "slow"]
    assert [item["result"] for item in captured[0]["tool_calls"]] == ["fast", "slow"]


@pytest.mark.asyncio
async def test_tau2_terminal_tool_batch_records_queued_write_without_executing_it(
    make_loop, monkeypatch, tau2_feedback_environment
):
    import json

    from benchmark.tau2.train import rollout_executor_vikingbot as tau2

    adapter, environment, writes, deliveries = tau2_feedback_environment

    def terminal_step(action):
        writes.append(json.loads(action)["name"])
        return "tool: simulator ended", 0.0, True, False, {}

    adapter.env.step = terminal_step
    provider = _MediaProvider(
        [
            [
                ToolCallRequest("first", "update_reservation_flights", {}, 0),
                ToolCallRequest("queued", "update_reservation_baggages", {}, 0),
            ]
        ]
    )
    loop = make_loop(provider=provider, max_iterations=1)
    monkeypatch.setattr(
        loop.context,
        "build_messages",
        AsyncMock(return_value=[{"role": "user", "content": "Update my booking"}]),
    )
    tau2._configure_tools(loop, environment, keep_default_tools=False)

    result = await tau2._run_agent(
        agent=loop,
        system_prompt="Airline policy",
        user_prompt="Update my booking",
        session_key=SessionKey(type="cli", channel_id="test", chat_id="terminal-write-batch"),
        sender_id="customer",
        keep_default_tools=False,
        should_stop=lambda: adapter.terminated,
    )
    assert result[0] is None
    assert adapter.terminated
    assert len(provider.calls) == 1
    assert writes == ["update_reservation_flights"]
    assert deliveries == []
    assert [tool["tool_name"] for tool in result[2]] == [
        "update_reservation_flights",
        "update_reservation_baggages",
    ]
    assert [tool["result"] for tool in result[2]] == ["simulator ended", "Task Terminated"]
