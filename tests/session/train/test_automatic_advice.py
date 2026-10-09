# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from benchmark.tau2.train.dag_experience_runtime import (
    EXPERIENCE_REMINDER_MARKER,
    Tau2DagExperienceRuntime,
    _render_advice,
)


def _note(status="advice", index=0):
    return (
        _render_advice(
            [{"instruction": f"Review the relevant constraints {index}", "conditions": []}]
        )
        if status == "advice"
        else None
    )


@pytest.mark.asyncio
async def test_only_recorded_user_turns_trigger_recall_and_short_replies_keep_context():
    runtime = Tau2DagExperienceRuntime()
    runtime.client = SimpleNamespace(search_advice=AsyncMock(return_value={}))
    runtime.record_user_message("Change my booking")
    messages = [{"role": "user", "content": "Change my booking"}]
    assert [note async for note in runtime.recall(messages)] == []
    assert runtime.client.search_advice.await_args.kwargs["query"] == "Change my booking"

    messages.extend(
        [
            {"role": "user", "content": "Reflect on the results and decide next steps."},
            {"role": "tool", "name": "get_details", "content": "New facts"},
            {"role": "user", "content": "[Experience Reminder] old advice"},
        ]
    )
    assert [note async for note in runtime.recall(messages)] == []
    assert runtime.client.search_advice.await_count == 1

    # Identical replies are distinct real user messages, not duplicate work to suppress.
    runtime.record_user_message("Yes", "Would you like economy?")
    runtime.record_user_message("Yes", "Confirm the proposed change?")
    assert [note async for note in runtime.recall(messages)] == []
    assert runtime.client.search_advice.await_count == 3
    first_query = runtime.client.search_advice.await_args_list[1].kwargs["query"]
    second_query = runtime.client.search_advice.await_args_list[2].kwargs["query"]
    assert "Yes" in first_query and "Would you like economy?" in first_query
    assert "Change my booking" in first_query
    assert "Confirm the proposed change?" in second_query
    assert "Reflect on" not in second_query and "old advice" not in second_query
    assert [r["user_message_index"] for r in runtime.recalls] == [0, 1, 2]


@pytest.mark.asyncio
async def test_automatic_query_is_bounded_and_client_setup_failure_is_nonfatal(monkeypatch):
    runtime = Tau2DagExperienceRuntime()
    from vikingbot.openviking_mount.ov_server import VikingClient

    create = AsyncMock(side_effect=httpx.ConnectError("unavailable"))
    monkeypatch.setattr(VikingClient, "create", create)
    runtime.record_user_message("x" * 20000)
    assert [note async for note in runtime.recall([])] == []
    assert len(runtime.recalls[0]["query"]) == 16384
    assert runtime.recalls[0]["status"] == "unavailable"
    assert runtime.events[0]["stage"] == "user_message"
    assert [note async for note in runtime.recall([])] == []
    create.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancellation_is_not_swallowed_by_automatic_recall():
    runtime = Tau2DagExperienceRuntime()
    runtime.client = SimpleNamespace(search_advice=AsyncMock(side_effect=asyncio.CancelledError()))
    runtime.record_user_message("Cancel my booking")
    with pytest.raises(asyncio.CancelledError):
        _ = [note async for note in runtime.recall([])]


@pytest.mark.parametrize(
    "reply",
    [
        None,
        "",
        "###STOP###",
        "###TRANSFER###",
        "Task Terminated",
        "###OUT-OF-SCOPE###",
        "Error: failed",
        "Error executing communicate_with_user: failed",
    ],
)
def test_control_and_error_replies_are_not_user_turns(reply):
    from benchmark.tau2.train.rollout_executor_vikingbot import _is_real_tau2_user_reply

    assert not _is_real_tau2_user_reply(reply)


@pytest.mark.asyncio
@pytest.mark.parametrize("reply_kind", ["tool", "plain", "batch"])
@pytest.mark.parametrize("status", ["advice", "no_match", "no_applicable_advice", "unavailable"])
async def test_auto_advice_reaches_model_for_each_user_turn_without_skills_or_manual_tool(
    tmp_path, monkeypatch, reply_kind, status
):
    from vikingbot.agent.context import ContextBuilder
    from vikingbot.agent.tools.registry import ToolRegistry
    from vikingbot.config.schema import SessionKey
    from vikingbot.providers.base import LLMResponse, ToolCallRequest

    import benchmark.tau2.train.dag_experience_runtime as runtime_module
    import benchmark.tau2.train.rollout_executor_vikingbot as module

    runtime = Tau2DagExperienceRuntime()
    returned_notes = []
    delivery_recording_order = []

    async def search(query, messages, index, trigger):
        if trigger == "before_action":
            return None  # No new guidance at the final done checkpoint.
        if status == "advice" and returned_notes:
            assert returned_notes[-1] in delivery_recording_order
        note = _note(status, len(returned_notes))
        returned_notes.append(note)
        return note

    runtime._search = AsyncMock(side_effect=search)
    runtime.record_injected = AsyncMock(side_effect=delivery_recording_order.append)
    monkeypatch.setattr(runtime_module, "Tau2DagExperienceRuntime", lambda **kwargs: runtime)

    skill = tmp_path / "skills/unwanted/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: unwanted\nalways: true\n---\nUNWANTED_SKILL")
    context = ContextBuilder(tmp_path, remote_skills_summary="UNWANTED_REMOTE_SKILL")
    context._templates_ensured = True
    context._get_identity = AsyncMock(return_value="Test agent")

    class Environment:
        def list_openai_tools(self):
            return [
                {
                    "type": "function",
                    "function": {
                        "name": n,
                        "description": n,
                        "parameters": {
                            "type": "object",
                            "properties": {"content": {"type": "string"}},
                        },
                    },
                }
                for n in ["get_details", "communicate_with_user", "done"]
            ]

        def call_tool(self, name, args):
            return "Yes" if name == "communicate_with_user" else "known facts"

    class Provider:
        def __init__(self):
            self.requests = []

        async def chat(self, messages, tools=None, **kwargs):
            self.requests.append(copy.deepcopy({"messages": messages, "tools": tools}))
            round_no = len(self.requests)
            if round_no == 1:
                assert runtime._search.await_count == 1
                return LLMResponse(
                    content=None, tool_calls=[ToolCallRequest("read", "get_details", {}, 0)]
                )
            if round_no == 2:
                # Read tools and the loop's internal reflection message must not trigger recall.
                assert runtime._search.await_count == 1
            if reply_kind == "batch" and round_no == 2:
                return LLMResponse(
                    content=None,
                    tool_calls=[
                        ToolCallRequest(
                            f"reply-{i}", "communicate_with_user", {"content": f"Question {i}?"}, 0
                        )
                        for i in range(2)
                    ],
                )
            if round_no in (2, 3) and reply_kind != "batch":
                if reply_kind == "plain":
                    return LLMResponse(content=f"Question {round_no}?")
                return LLMResponse(
                    content=None,
                    tool_calls=[
                        ToolCallRequest(
                            f"reply-{round_no}",
                            "communicate_with_user",
                            {"content": f"Question {round_no}?"},
                            0,
                        )
                    ],
                )
            assert runtime._search.await_count == 3
            return LLMResponse(content=None, tool_calls=[ToolCallRequest("end", "done", {}, 0)])

    imports = module._vikingbot_imports()

    class Agent:
        _run_agent_loop = imports["AgentLoop"]._run_agent_loop
        _chat_with_stream_events = imports["AgentLoop"]._chat_with_stream_events

    agent = Agent()
    from vikingbot.session.manager import SessionManager

    agent.sessions = SessionManager(tmp_path)
    agent.context = context
    agent.tools = ToolRegistry()
    # Seed an inherited capability to verify the real loop never exposes it.
    agent.tools.register(
        module._make_tau2_tool(
            {
                "type": "function",
                "function": {
                    "name": "web_search",
                    "description": "must not reach the model",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            Environment(),
        )
    )
    agent.provider = Provider()
    agent.model = "fake"
    agent.temperature = None
    agent.max_iterations = 5
    agent.sandbox_manager = None
    module._configure_tools(agent, Environment(), keep_default_tools=True)
    captured = {}
    result = await module._run_agent(
        agent=agent,
        system_prompt="Task requirements",
        user_prompt="Change my booking",
        session_key=SessionKey(type="cli", channel_id="test", chat_id="auto"),
        sender_id="user",
        keep_default_tools=True,
        initial_llm_request=captured,
    )
    assert runtime._search.await_count == 4
    assert [c.args[3] for c in runtime._search.await_args_list] == ["user_message"] * 3 + [
        "before_action"
    ]
    assert len(result[6]) == (3 if status == "advice" else 0)
    assert runtime.record_injected.await_count == (3 if status == "advice" else 0)
    assert bool(result[5]) == (status == "advice")
    assert captured["messages"] == agent.provider.requests[0]["messages"]
    for request in agent.provider.requests:
        assert "UNWANTED_SKILL" not in str(request)
        assert "UNWANTED_REMOTE_SKILL" not in str(request)
        assert {tool["function"]["name"] for tool in request["tools"]} == {
            "get_details",
            "communicate_with_user",
            "done",
        }
    assert (EXPERIENCE_REMINDER_MARKER in str(captured["messages"])) == (status == "advice")
    if status == "advice":
        note = next(
            m["content"]
            for m in captured["messages"]
            if m["content"].startswith(EXPERIENCE_REMINDER_MARKER)
        )
        assert "### 建议 1" in note and "**适用条件**" in note and "**建议**" in note
        assert '"query"' not in note and '"matched_experiences"' not in note


@pytest.mark.asyncio
async def test_user_reply_is_recorded_only_after_the_actual_tool_result_enters_context():
    from types import SimpleNamespace

    from benchmark.tau2.train.rollout_executor_vikingbot import _make_tau2_tool

    turns = []
    tool = _make_tau2_tool(
        {"function": {"name": "communicate_with_user", "parameters": {}}},
        SimpleNamespace(call_tool=lambda *args: "raw reply"),
        record_user_message=lambda user, assistant: turns.append((user, assistant)),
    )
    context = SimpleNamespace(on_result_added=None)
    assert await tool.execute(context, content="Which option?") == "raw reply"
    assert turns == []
    # A post-call hook may replace the result: only the delivered text is a trigger.
    await context.on_result_added("Error: reply not delivered")
    assert turns == []
    await context.on_result_added("actual delivered reply")
    assert turns == [("actual delivered reply", "Which option?")]


def test_reports_count_automatic_recalls_separately_from_model_tool_calls():
    from types import SimpleNamespace

    from openviking.session.train import Case, Rollout, Rubric
    from openviking.session.train.components.report_builder import PipelineReportBuilder

    rollouts = [
        Rollout(
            case=Case("case", "signature", {}, Rubric("reward", "", [])),
            messages=[],
            policy_snapshot_id="snapshot",
            metadata={
                "advice_recalls": [{"status": "no_match"}, {"status": "advice"}],
                "memory": "actual advice",
                "tools_used": [],
            },
        ),
        Rollout(
            case=Case("other", "signature", {}, Rubric("reward", "", [])),
            messages=[],
            policy_snapshot_id="snapshot",
            metadata={"advice_recalls": [], "tools_used": []},
        ),
    ]
    summary = PipelineReportBuilder().memory_usage_from_analyses(
        [SimpleNamespace(metadata={"rollout": rollout}) for rollout in rollouts]
    )
    assert summary["advice_recall_total"] == 2
    assert summary["advice_recall_rollout_count"] == 1
    assert summary["memory_context_count"] == 1
    assert summary["memory_tool_call_total"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [ValueError("invalid backend response"), "invalid JSON"])
async def test_automatic_recall_processing_errors_do_not_abort_the_rollout(result):
    runtime = Tau2DagExperienceRuntime()
    runtime.client = SimpleNamespace(
        search_advice=(
            AsyncMock(side_effect=result)
            if isinstance(result, Exception)
            else AsyncMock(return_value=result)
        )
    )
    runtime.record_user_message("Help with my request")
    assert [note async for note in runtime.recall([])] == []
    assert runtime.recalls[0]["status"] == "unavailable"
    assert runtime.events[0]["stage"] == "user_message"
