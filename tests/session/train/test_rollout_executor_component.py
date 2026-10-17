# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from openviking.session.train import (
    Case,
    ExecutionContext,
    Experience,
    ExperienceSet,
    Rubric,
    RubricCriterion,
    SingleTurnLLMRolloutExecutor,
    default_single_turn_prompt,
)


class FakeVLM:
    def __init__(self, response="assistant answer"):
        self.response = response
        self.calls = []

    async def get_completion_async(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def _case() -> Case:
    return Case(
        name="case-1",
        task_signature="booking_duplicate",
        input={"user_request": "cancel duplicate booking"},
        rubric=Rubric(
            name="booking_rubric",
            description="Cancel only the verified duplicate booking.",
            criteria=[
                RubricCriterion(
                    name="verify_duplicate",
                    description="Verify duplicate status first.",
                    required=True,
                    weight=1.0,
                )
            ],
        ),
    )


def _policy_set() -> ExperienceSet:
    return ExperienceSet(
        root_uri="viking://user/u/memories/experiences",
        policies=[
            Experience(
                name="booking_policy",
                uri="viking://user/u/memories/experiences/booking_policy.md",
                version=2,
                status="production",
                content="Always verify duplicates before cancellation.",
            )
        ],
    )


@pytest.mark.asyncio
async def test_single_turn_llm_rollout_executor_produces_rollout_messages():
    vlm = FakeVLM()
    executor = SingleTurnLLMRolloutExecutor(vlm=vlm, thinking=False)
    context = ExecutionContext(policy_snapshot_id="snapshot-1")

    rollouts = await executor.execute([_case()], _policy_set(), context)

    assert len(rollouts) == 1
    rollout = rollouts[0]
    assert rollout.case.name == "case-1"
    assert rollout.policy_snapshot_id == "snapshot-1"
    assert [message.role for message in rollout.messages] == ["user", "assistant"]
    assert "Always verify duplicates" in rollout.messages[0].content
    assert "cancel duplicate booking" in rollout.messages[0].content
    assert rollout.messages[1].content == "assistant answer"
    assert vlm.calls[0]["thinking"] is False
    assert vlm.calls[0]["prompt"] == rollout.messages[0].content


@pytest.mark.asyncio
async def test_single_turn_llm_rollout_executor_accepts_custom_prompt_builder():
    vlm = FakeVLM(response=type("Resp", (), {"content": "structured answer"})())

    def build_prompt(case, policy_set, context):
        return f"custom:{case.name}:{len(policy_set.policies)}:{context.policy_snapshot_id}"

    executor = SingleTurnLLMRolloutExecutor(vlm=vlm, prompt_builder=build_prompt)

    rollouts = await executor.execute(
        [_case()],
        _policy_set(),
        ExecutionContext(policy_snapshot_id="snapshot-2"),
    )

    assert rollouts[0].messages[0].content == "custom:case-1:1:snapshot-2"
    assert rollouts[0].messages[1].content == "structured answer"


def test_default_single_turn_prompt_contains_case_policy_and_rubric():
    prompt = default_single_turn_prompt(
        _case(),
        _policy_set(),
        ExecutionContext(policy_snapshot_id="snapshot-3"),
    )

    assert "Policy snapshot: snapshot-3" in prompt
    assert "booking_policy v2 [production]" in prompt
    assert "cancel duplicate booking" in prompt
    assert "verify_duplicate" in prompt


def test_dataset_service_policy_set_from_dict_preserves_policies():
    from openviking.session.train.components.dataset_service import policy_set_from_dict

    policy_set = policy_set_from_dict(
        {
            "root_uri": "viking://user/u/memories/experiences",
            "policies": [
                {
                    "name": "booking_policy",
                    "uri": "viking://user/u/memories/experiences/booking_policy.md",
                    "version": 2,
                    "status": "production",
                    "content": "Always verify duplicates before cancellation.",
                    "metadata": {"domain": "booking"},
                }
            ],
            "metadata": {"snapshot": "remote"},
        }
    )

    assert policy_set.root_uri == "viking://user/u/memories/experiences"
    assert policy_set.metadata == {"snapshot": "remote"}
    assert len(policy_set.policies) == 1
    policy = policy_set.policies[0]
    assert policy.name == "booking_policy"
    assert policy.uri == "viking://user/u/memories/experiences/booking_policy.md"
    assert policy.version == 2
    assert policy.status == "production"
    assert policy.content == "Always verify duplicates before cancellation."
    assert policy.metadata == {"domain": "booking"}


def test_tau2_rollout_messages_use_completed_structured_tool_parts():
    from benchmark.tau2.train.rollout_executor import _build_rollout_messages
    from openviking.message import TextPart, ToolPart

    rollout_messages = _build_rollout_messages(
        system_prompt="policy",
        user_prompt="user request",
        tools_used=[
            {
                "tool_name": "get_user_details",
                "args": '{"user_id": "emma_kim_9957"}',
                "result": '{"membership": "gold"}',
            }
        ],
        final_content="done",
    )

    assert isinstance(rollout_messages[0].parts[0], TextPart)
    assert rollout_messages[0].parts[0].text.startswith("system:\npolicy")

    tool_message = rollout_messages[2]
    assert tool_message.role == "user"
    assert isinstance(tool_message.parts[0], ToolPart)
    assert tool_message.parts[0].tool_status == "completed"
    assert tool_message.parts[0].tool_input == {"user_id": "emma_kim_9957"}
    assert tool_message.parts[0].tool_output == '{"membership": "gold"}'
    assert not any(
        isinstance(part, TextPart) and "tool-call:" in part.text
        for message in rollout_messages
        for part in message.parts
    )
    assert not any(
        isinstance(part, ToolPart) and part.tool_status == "running"
        for message in rollout_messages
        for part in message.parts
    )


@pytest.mark.parametrize("auto_forwarded", [False, True])
def test_tau2_communicate_with_user_renders_as_dialogue(auto_forwarded):
    from benchmark.tau2.train.rollout_executor import _build_rollout_messages
    from openviking.message import TextPart, ToolPart

    rollout_messages = _build_rollout_messages(
        system_prompt="policy",
        user_prompt="user request",
        tools_used=[
            {
                "tool_name": "communicate_with_user",
                "args": {"content": "Could you provide your user ID?"},
                "result": "Sure, it is emma_kim_9957.",
                "auto": auto_forwarded,
            }
        ],
        final_content=None,
    )

    assert rollout_messages[2].role == "assistant"
    assert isinstance(rollout_messages[2].parts[0], TextPart)
    prefix = "auto-forwarded via" if auto_forwarded else "via"
    assert rollout_messages[2].parts[0].text == (
        f"[{prefix} communicate_with_user]\nCould you provide your user ID?"
    )
    assert len(rollout_messages) == 4
    assert rollout_messages[3].role == "user"
    assert isinstance(rollout_messages[3].parts[0], TextPart)
    assert rollout_messages[3].parts[0].text == "Sure, it is emma_kim_9957."
    assert not any(
        isinstance(part, ToolPart) and part.tool_name == "communicate_with_user"
        for message in rollout_messages
        for part in message.parts
    )


def test_native_communication_call_provenance_does_not_label_plain_assistant_text():
    from benchmark.tau2.train.rollout_executor_native import _build_rollout_messages_from_simulation
    from openviking.message import ToolPart

    tool_input = {"content": "Could you provide your user ID?"}
    simulation = SimpleNamespace(
        messages=[
            SimpleNamespace(
                role="assistant",
                content=None,
                tool_calls=[
                    SimpleNamespace(id="comm1", name="communicate_with_user", arguments=tool_input)
                ],
            ),
            SimpleNamespace(role="tool", id="comm1", content="My user ID is U1."),
            SimpleNamespace(role="assistant", content="A plain response."),
        ]
    )
    messages = _build_rollout_messages_from_simulation(simulation=simulation)
    assert [m.role for m in messages] == ["assistant", "user", "assistant"]
    assert [m.content for m in messages] == [
        "[via communicate_with_user]\nCould you provide your user ID?",
        "My user ID is U1.",
        "A plain response.",
    ]
    assert not any(isinstance(p, ToolPart) for m in messages for p in m.parts)
    assert tool_input == {"content": "Could you provide your user ID?"}


@pytest.mark.parametrize(
    "tool_input", [None, {}, {"content": None}, {"content": ""}, {"content": " \n"}]
)
def test_empty_communication_does_not_create_a_provenance_only_step(tool_input):
    from benchmark.tau2.train._rollout_helpers import _communicate_text_from_tool_input

    assert _communicate_text_from_tool_input(tool_input) == ""


def test_communication_provenance_preserves_original_text_whitespace():
    from benchmark.tau2.train._rollout_helpers import _communicate_text_from_tool_input

    text = "  Please confirm.\n"
    assert _communicate_text_from_tool_input({"content": text}) == (
        "[via communicate_with_user]\n" + text
    )


def test_tau2_rollout_messages_omit_empty_final_after_done():
    from benchmark.tau2.train.rollout_executor import _build_rollout_messages

    rollout_messages = _build_rollout_messages(
        system_prompt="policy",
        user_prompt="user request",
        tools_used=[{"tool_name": "done", "args": "{}", "result": "Task Terminated"}],
        final_content=None,
    )

    assert "tau2-final" not in {message.id for message in rollout_messages}
    assert rollout_messages[-1].parts[0].tool_name == "done"
    assert rollout_messages[-1].parts[0].tool_output == "Task Terminated"
    assert "tau2-reward" not in {message.id for message in rollout_messages}


def test_tau2_task_terminated_reply_stops_plain_text_loop():
    from benchmark.tau2.train.rollout_executor_vikingbot import _tau2_user_reply_terminates

    assert _tau2_user_reply_terminates("Task Terminated") is True


@pytest.mark.asyncio
async def test_tau2_plain_text_router_records_delivered_assistant_message():
    from benchmark.tau2.train.rollout_executor_vikingbot import _make_tau2_plain_text_router

    delivered = []
    user_messages = []

    class FakeTools:
        @staticmethod
        def has(name):
            return name == "communicate_with_user"

        @staticmethod
        async def execute(name, arguments, **kwargs):
            assert name == "communicate_with_user"
            assert arguments == {"content": "visible answer"}
            return "user reply"

    route = _make_tau2_plain_text_router(
        publish_events=False,
        bus=None,
        session_key=SimpleNamespace(),
        record_delivered_assistant_message=delivered.append,
        record_user_message=lambda user, assistant: user_messages.append((user, assistant)),
    )
    result = await route(
        SimpleNamespace(
            text="visible answer",
            reasoning_content=None,
            messages=[],
            tools=FakeTools(),
            session_key=SimpleNamespace(),
            sandbox_manager=None,
            sender_id="user",
            memory_peer_ids=None,
            memory_owner_user_ids=None,
            openviking_connection=None,
        )
    )

    assert delivered == ["visible answer"]
    assert result.messages == [
        {"role": "assistant", "content": "visible answer"},
        {"role": "user", "content": "user reply"},
    ]
    assert user_messages == [("user reply", "visible answer")]


def test_tau2_reward_info_stays_json_safe_in_evaluation_not_dialogue():
    import json

    from tau2.data_model.simulation import RewardInfo, RewardType

    from benchmark.tau2.train.rollout_executor import _build_rollout_messages, _tau2_evaluation

    reward_info = RewardInfo(
        reward=1.0,
        reward_basis=[RewardType.DB],
        reward_breakdown={RewardType.DB: 1.0},
    )

    rollout_messages = _build_rollout_messages(
        system_prompt="policy",
        user_prompt="user request",
        tools_used=[],
        final_content="done",
    )
    evaluation = _tau2_evaluation(reward=1.0, evaluation_result=reward_info)

    assert rollout_messages[-1].id == "tau2-final"
    assert [message.id for message in rollout_messages] == [
        "tau2-system",
        "tau2-user",
        "tau2-final",
    ]
    assert '"reward": 1.0' in evaluation.feedback[0]
    assert '"reward_basis": ["DB"]' in evaluation.feedback[0]
    json.dumps(evaluation.metadata, sort_keys=True)


def test_tau2_litellm_generate_rate_limit_retry_patch(monkeypatch):
    import benchmark.tau2.common.tau2_env.tau2_environment as tau2_environment

    calls = {"count": 0}
    sleeps = []

    def fake_generate():
        calls["count"] += 1
        if calls["count"] < 5:
            raise RuntimeError("TPM (Tokens Per Minute) limit of the model is exceeded")
        return "ok"

    class FakeLLMUtils:
        generate = staticmethod(fake_generate)

    class FakeUserSimulator:
        generate = staticmethod(fake_generate)
        UserSimulator = type("UserSimulator", (), {})

    modules = {
        "tau2.utils.llm_utils": FakeLLMUtils,
        "tau2.user.user_simulator": FakeUserSimulator,
    }

    def fake_import_module(name):
        if name in modules:
            return modules[name]
        raise ImportError(name)

    monkeypatch.setattr(tau2_environment.importlib, "import_module", fake_import_module)
    monkeypatch.setattr(tau2_environment, "_tau2_rate_limit_retry_delay", lambda attempt: attempt)
    monkeypatch.setattr(tau2_environment.time, "sleep", lambda delay: sleeps.append(delay))

    tau2_environment._install_tau2_litellm_rate_limit_retry()

    assert FakeLLMUtils.generate() == "ok"
    assert calls["count"] == 5
    assert sleeps == [1, 2, 3, 4]
    assert FakeUserSimulator.generate is FakeLLMUtils.generate


def test_tau2_litellm_generate_retry_patch_does_not_retry_non_rate_limit(monkeypatch):
    import benchmark.tau2.common.tau2_env.tau2_environment as tau2_environment

    calls = {"count": 0}

    def fake_generate():
        calls["count"] += 1
        raise RuntimeError("AuthenticationError Unauthorized")

    class FakeLLMUtils:
        generate = staticmethod(fake_generate)

    def fake_import_module(name):
        if name == "tau2.utils.llm_utils":
            return FakeLLMUtils
        raise ImportError(name)

    monkeypatch.setattr(tau2_environment.importlib, "import_module", fake_import_module)

    def fail_on_sleep(_delay):
        raise AssertionError("unexpected sleep")

    monkeypatch.setattr(tau2_environment.time, "sleep", fail_on_sleep)

    tau2_environment._install_tau2_litellm_rate_limit_retry()

    with pytest.raises(RuntimeError, match="AuthenticationError"):
        FakeLLMUtils.generate()
    assert calls["count"] == 1


def _tau2_model_reply(content=None, tool_calls=None, **raw_fields):
    return SimpleNamespace(
        content=content,
        tool_calls=tool_calls,
        raw_data={
            "id": "response-for-diagnostics",
            "model": "test-model",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": content, "tool_calls": tool_calls, **raw_fields},
                }
            ],
            "usage": {"completion_tokens": 12},
        },
    )


@pytest.mark.parametrize("content", [None, "", " \n"])
@pytest.mark.parametrize(
    "reasoning",
    [
        {"reasoning_content": "private thinking"},
        {"thinking_blocks": [{"type": "thinking", "thinking": "private thinking"}]},
    ],
)
def test_tau2_generate_retries_reasoning_only_reply(monkeypatch, content, reasoning):
    import benchmark.tau2.common.tau2_env.tau2_environment as module

    reply = _tau2_model_reply("Yes, please proceed.")
    generate = Mock(spec=[], side_effect=[_tau2_model_reply(content, **reasoning), reply])
    sleeps = []
    diagnostics = Mock()
    monkeypatch.setattr(module, "_tau2_rate_limit_retry_delay", lambda attempt: attempt)
    monkeypatch.setattr(module.time, "sleep", sleeps.append)
    monkeypatch.setattr(module.tracer, "info", diagnostics)
    warning = Mock()
    monkeypatch.setattr(module.logger, "warning", warning)
    messages = [{"role": "user", "content": "Please confirm."}]

    wrapped = module._wrap_tau2_generate_with_rate_limit_retry(generate)
    assert wrapped(model="test-model", messages=messages) is reply
    assert generate.call_count == 2
    assert sleeps == [1]
    assert all(call.kwargs["messages"] is messages for call in generate.call_args_list)
    assert generate.call_args_list[0] == generate.call_args_list[1]
    assert messages == [{"role": "user", "content": "Please confirm."}]
    event = diagnostics.call_args.args[0]
    assert "response-for-diagnostics" in event
    assert "finish_reason" in event
    assert "completion_tokens" in event
    assert "private thinking" not in event
    warning.assert_not_called()


def test_tau2_generate_reasoning_only_retry_is_bounded(monkeypatch):
    import benchmark.tau2.common.tau2_env.tau2_environment as module

    generate = Mock(spec=[], return_value=_tau2_model_reply(reasoning_content="private thinking"))
    sleeps = []
    trace_error = Mock()
    monkeypatch.setattr(module, "_tau2_rate_limit_retry_delay", lambda attempt: attempt)
    monkeypatch.setattr(module.time, "sleep", sleeps.append)
    monkeypatch.setattr(module.tracer, "error", trace_error)

    wrapped = module._wrap_tau2_generate_with_rate_limit_retry(generate)
    with pytest.raises(ValueError, match="reasoning-only.*3 attempts") as error:
        wrapped()
    assert generate.call_count == 3
    assert sleeps == [1, 2]
    assert "response-for-diagnostics" in str(error.value)
    assert "private thinking" not in str(error.value)
    assert trace_error.call_args.kwargs == {"e": error.value, "console": False}


@pytest.mark.parametrize(
    "reply",
    [
        _tau2_model_reply("Valid final answer", reasoning_content="thinking"),
        _tau2_model_reply("###STOP###", reasoning_content="thinking"),
        _tau2_model_reply(tool_calls=[{"name": "tool"}], reasoning_content="thinking"),
        _tau2_model_reply(),
        _tau2_model_reply("", reasoning_content=""),
        _tau2_model_reply("", reasoning_content=" \n"),
        _tau2_model_reply(reasoning_content="thinking", refusal="Request refused"),
        SimpleNamespace(content=None, tool_calls=None, raw_data=None),
        SimpleNamespace(
            content=None,
            tool_calls=None,
            audio_content="audio-bytes",
            raw_data=_tau2_model_reply(reasoning_content="thinking").raw_data,
        ),
    ],
)
def test_tau2_generate_does_not_retry_other_replies(monkeypatch, reply):
    import benchmark.tau2.common.tau2_env.tau2_environment as module

    generate = Mock(spec=[], return_value=reply)
    sleep = Mock(side_effect=AssertionError("unexpected retry"))
    monkeypatch.setattr(module.time, "sleep", sleep)
    assert module._wrap_tau2_generate_with_rate_limit_retry(generate)() is reply
    generate.assert_called_once_with()
    sleep.assert_not_called()


def test_tau2_generate_reasoning_only_budget_survives_rate_limits(monkeypatch):
    import benchmark.tau2.common.tau2_env.tau2_environment as module

    empty = _tau2_model_reply(reasoning_content="thinking")
    rate_limit = RuntimeError("TPM (Tokens Per Minute) limit of the model is exceeded")
    generate = Mock(spec=[], side_effect=[empty, rate_limit, empty, rate_limit, empty])
    sleeps = []
    monkeypatch.setattr(module, "_tau2_rate_limit_retry_delay", lambda attempt: attempt)
    monkeypatch.setattr(module.time, "sleep", sleeps.append)

    with pytest.raises(ValueError, match="reasoning-only.*3 attempts"):
        module._wrap_tau2_generate_with_rate_limit_retry(generate)()
    assert generate.call_count == 5
    assert sleeps == [1, 1, 2, 2]


def test_tau2_reasoning_only_retry_does_not_duplicate_user_history(monkeypatch):
    import tau2.user.user_simulator as simulator_module
    from tau2.data_model.message import AssistantMessage, SystemMessage
    from tau2.user.user_simulator import UserSimulator, UserState

    import benchmark.tau2.common.tau2_env.tau2_environment as module

    empty = AssistantMessage(
        role="assistant",
        content=None,
        raw_data=_tau2_model_reply(reasoning_content="thinking").raw_data,
    )
    valid = AssistantMessage(role="assistant", content="Yes, please proceed.")
    generate = Mock(spec=[], side_effect=[empty, valid])
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        simulator_module, "generate", module._wrap_tau2_generate_with_rate_limit_retry(generate)
    )
    simulator = UserSimulator(llm="offline-mock", instructions="Offline test", tools=[])
    state = UserState(
        system_messages=[SystemMessage(role="system", content="Offline test")], messages=[]
    )
    incoming = AssistantMessage(role="assistant", content="Please confirm.")

    user_reply, state = simulator.generate_next_message(incoming, state)
    user_reply.validate()
    assert user_reply.content == valid.content
    assert state.messages == [incoming, user_reply]
    assert generate.call_count == 2


@pytest.mark.parametrize("exception", [ValueError("bad request"), asyncio.CancelledError()])
def test_tau2_reasoning_only_retry_preserves_other_errors(monkeypatch, exception):
    import benchmark.tau2.common.tau2_env.tau2_environment as module

    generate = Mock(
        spec=[],
        side_effect=[_tau2_model_reply(reasoning_content="thinking"), exception],
    )
    sleeps = []
    monkeypatch.setattr(module, "_tau2_rate_limit_retry_delay", lambda attempt: attempt)
    monkeypatch.setattr(module.time, "sleep", sleeps.append)
    with pytest.raises(type(exception)) as error:
        module._wrap_tau2_generate_with_rate_limit_retry(generate)()
    assert error.value is exception
    assert generate.call_count == 2
    assert sleeps == [1]


def test_tau2_reasoning_only_retry_preserves_content_filter(monkeypatch):
    import benchmark.tau2.common.tau2_env.tau2_environment as module

    reply = _tau2_model_reply(reasoning_content="thinking")
    reply.raw_data["choices"][0]["finish_reason"] = "content_filter"
    generate = Mock(spec=[], return_value=reply)
    sleep = Mock(side_effect=AssertionError("unexpected retry"))
    monkeypatch.setattr(module.time, "sleep", sleep)
    assert module._wrap_tau2_generate_with_rate_limit_retry(generate)() is reply
    generate.assert_called_once_with()
    sleep.assert_not_called()


def test_tau2_native_env_reward_handles_required_id_and_tool_call_ids(monkeypatch):
    import benchmark.tau2.common.tau2_env.tau2_environment as tau2_environment
    from benchmark.tau2.common.tau2_env.tau2_environment import Tau2BenchEnv

    monkeypatch.setattr(tau2_environment, "AgentGymEnv", None)
    env = Tau2BenchEnv("airline", "1")
    env.reset()
    env.tool_call("get_user_details", {"user_id": "raj_sanchez_7340"})
    env.tool_call("get_reservation_details", {"reservation_id": "Q69X3R"})

    reward, evaluation = env._impl._get_reward()

    assert reward == 1.0
    assert evaluation.reward == 1.0


def test_tau2_native_env_records_communication_as_assistant_text(monkeypatch):
    import benchmark.tau2.common.tau2_env.tau2_environment as tau2_environment
    from benchmark.tau2.common.tau2_env.tau2_environment import Tau2BenchEnv

    monkeypatch.setattr(tau2_environment, "AgentGymEnv", None)
    env = Tau2BenchEnv("airline", "3")
    env.reset()
    env.tool_call("communicate_with_user", {"content": "You may bring 4 suitcases."})

    reward, evaluation = env._impl._get_reward()

    assert reward == 1.0
    assert evaluation.communicate_checks[0].met is True


def test_tau2_final_answer_is_appended_for_native_evaluation(monkeypatch):
    import benchmark.tau2.common.tau2_env.tau2_environment as tau2_environment
    from benchmark.tau2.common.tau2_env.tau2_environment import Tau2BenchEnv
    from benchmark.tau2.train.rollout_executor import _append_final_answer_for_tau2_evaluation

    monkeypatch.setattr(tau2_environment, "AgentGymEnv", None)
    env = Tau2BenchEnv("airline", "3")
    env.reset()
    _append_final_answer_for_tau2_evaluation(env, "You may bring 4 suitcases.")

    reward, evaluation = env._impl._get_reward()

    assert reward == 1.0
    assert evaluation.communicate_checks[0].met is True


@pytest.mark.asyncio
@pytest.mark.parametrize("data_split", ["airline_train", "airline_test"])
@pytest.mark.parametrize("keep_default_tools", [False, True])
@pytest.mark.parametrize("empty_provider", [False, True])
async def test_tau2_configure_tools_uses_only_provider_whitelist(
    data_split, keep_default_tools, empty_provider
):
    from vikingbot.agent.tools.registry import ToolRegistry
    from vikingbot.config.schema import SessionKey

    from benchmark.tau2.train.rollout_executor_vikingbot import _configure_tools, _make_tau2_tool

    def schema(name):
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": name,
                "parameters": {"type": "object", "properties": {}},
            },
        }

    inherited_names = [
        "read_file",
        "write_file",
        "edit_file",
        "list_dir",
        "exec",
        "web_search",
        "web_fetch",
        "generate_image",
        "message",
        "spawn",
        "openviking_search",
        "openviking_memory_commit",
        "ask_for_advice",
        "future_builtin",
        "calculate",
    ]
    inherited_provider = SimpleNamespace(call_tool=Mock(side_effect=AssertionError("builtin ran")))
    agent = SimpleNamespace(tools=ToolRegistry())
    for name in inherited_names:
        agent.tools.register(_make_tau2_tool(schema(name), inherited_provider))
    # A different, ordinary bot registry must remain untouched.
    ordinary_tools = ToolRegistry()
    ordinary_tools.register(agent.tools.get("web_search"))

    provider_names = (
        []
        if empty_provider
        else [
            "get_user_details",
            "calculate",
            "communicate_with_user",
            "done",
        ]
    )
    provider = SimpleNamespace(
        list_openai_tools=lambda: [schema(name) for name in provider_names],
        call_tool=Mock(return_value="ok"),
    )
    _configure_tools(
        agent,
        provider,
        keep_default_tools=keep_default_tools,
        data_split=data_split,
        task_no=5,
    )

    assert agent.tools.tool_names == provider_names
    assert agent.tools.get_definitions() == [schema(name) for name in provider_names]
    assert agent.tools.get_definitions(ov_tools_enable=False) == agent.tools.get_definitions()
    assert (agent._tau2_dag_runtime is not None) == keep_default_tools
    if keep_default_tools:
        assert not hasattr(agent._tau2_dag_runtime, "experience_names")
    key = SessionKey(type="cli", channel_id="test", chat_id=data_split)
    for name in set(inherited_names) - set(provider_names):
        outcome = await agent.tools.execute_detailed(name, {}, session_key=key)
        assert outcome.result == f"Error: Tool '{name}' not found"
    if not empty_provider:
        # An overlapping name must use Tau2's implementation, not the old builtin.
        assert await agent.tools.get("calculate").execute(None) == "ok"
        provider.call_tool.assert_called_once_with("calculate", {})
    inherited_provider.call_tool.assert_not_called()
    assert ordinary_tools.tool_names == ["web_search"]


def test_tau2_rollout_backend_factory_defaults_to_native():
    from benchmark.tau2.train.rollout_executor import (
        NativeTau2RolloutExecutor,
        make_tau2_rollout_executor,
        normalize_tau2_rollout_backend,
    )

    executor = make_tau2_rollout_executor(
        options={"keep_default_tools": False, "max_iterations": 7},
        concurrency=3,
    )

    assert normalize_tau2_rollout_backend(None) == "native"
    assert isinstance(executor, NativeTau2RolloutExecutor)
    assert executor.concurrency == 3
    assert executor.memory_enabled is False
    assert executor.max_steps == 7
    assert executor.show_progress is False


def test_tau2_native_rollout_resolves_non_empty_llms(monkeypatch):
    from benchmark.tau2.train.rollout_executor_native import (
        NativeTau2RolloutExecutor,
        _resolve_llm_runtime_config,
    )

    monkeypatch.delenv("TAU2_AGENT_LLM", raising=False)
    monkeypatch.delenv("TAU2_USER_LLM", raising=False)

    agent_llm, agent_args, user_llm, user_args = _resolve_llm_runtime_config(
        NativeTau2RolloutExecutor(
            agent_llm_args={"temperature": 0.2},
            user_llm_args={"top_p": 0.9},
        )
    )

    assert agent_llm
    assert user_llm
    assert agent_args["temperature"] == 0.2
    assert user_args["temperature"] == 0.0
    assert user_args["top_p"] == 0.9


def test_tau2_native_rollout_uses_env_llm_when_options_omit_model(monkeypatch):
    from benchmark.tau2.train.rollout_executor_native import (
        NativeTau2RolloutExecutor,
        _resolve_llm_runtime_config,
    )

    monkeypatch.setenv("TAU2_AGENT_LLM", "openai/test-agent")
    monkeypatch.setenv("TAU2_USER_LLM", "openai/test-user")

    agent_llm, _agent_args, user_llm, _user_args = _resolve_llm_runtime_config(
        NativeTau2RolloutExecutor()
    )

    assert agent_llm == "openai/test-agent"
    assert user_llm == "openai/test-user"


def test_tau2_rollout_backend_factory_selects_vikingbot(monkeypatch):
    import benchmark.tau2.train.rollout_executor as module

    created = {}

    class FakeVikingBotExecutor:
        def __init__(self, **kwargs):
            created.update(kwargs)

    monkeypatch.setattr(module, "VikingBotTau2RolloutExecutor", FakeVikingBotExecutor)

    executor = module.make_tau2_rollout_executor(
        backend="vikingbot",
        options={
            "config_path": "/tmp/ov.conf",
            "max_iterations": 9,
        },
        concurrency=2,
    )

    assert isinstance(executor, FakeVikingBotExecutor)
    assert created == {
        "config_path": "/tmp/ov.conf",
        "concurrency": 2,
        "keep_default_tools": True,
        "max_iterations": 9,
    }


def test_tau2_service_rollout_backend_option_overrides_default(monkeypatch):
    import benchmark.tau2.train.service_app as service_app

    calls = []

    def fake_create_dataset_service_app(**kwargs):
        calls.append(kwargs)
        return kwargs

    class FakeExecutor:
        pass

    def fake_make_tau2_rollout_executor(**kwargs):
        calls.append({"factory": kwargs})
        return FakeExecutor()

    monkeypatch.setattr(service_app, "create_dataset_service_app", fake_create_dataset_service_app)
    monkeypatch.setattr(service_app, "make_tau2_rollout_executor", fake_make_tau2_rollout_executor)

    app = service_app.create_app(rollout_backend="native")
    executor = app["make_rollout_executor"]({"rollout_backend": "vikingbot", "max_iterations": 5})

    assert isinstance(executor, FakeExecutor)
    assert calls[-1]["factory"]["backend"] == "vikingbot"
    assert calls[-1]["factory"]["options"]["max_iterations"] == 5
    assert calls[-1]["factory"]["options"]["show_progress"] is False

    app["make_rollout_executor"]({"rollout_backend": "native", "show_progress": True})
    assert calls[-1]["factory"]["options"]["show_progress"] is True


@pytest.mark.asyncio
async def test_tau2_vikingbot_rollout_runs_on_current_event_loop():
    from benchmark.tau2.train.rollout_executor_vikingbot import VikingBotTau2RolloutExecutor

    expected_loop = asyncio.get_running_loop()
    expected_thread = threading.get_ident()
    observed = {}

    class FakeVikingBotExecutor(VikingBotTau2RolloutExecutor):
        async def _execute_one_async(self, case, context):
            del context
            observed["loop"] = asyncio.get_running_loop()
            observed["thread"] = threading.get_ident()
            await asyncio.sleep(0)
            return case.name

    executor = FakeVikingBotExecutor()

    result = await executor._execute_one(
        _case(),
        ExecutionContext(policy_snapshot_id="snapshot", metadata={}),
    )

    assert result == "case-1"
    assert observed["loop"] is expected_loop
    assert observed["thread"] == expected_thread


@pytest.mark.asyncio
@pytest.mark.parametrize("execution_failure", [None, "simulator", "evaluation"])
async def test_tau2_vikingbot_blocking_setup_and_reward_are_offloaded(
    monkeypatch, execution_failure
):
    import benchmark.tau2.train.rollout_executor_vikingbot as module
    from benchmark.tau2.train.rollout_executor_vikingbot import VikingBotTau2RolloutExecutor

    event_loop_thread = threading.get_ident()
    calls = []
    environments = []

    class FakeEnv:
        execution_error = None
        terminated = False

        def _get_reward(self):
            calls.append(("reward", threading.get_ident()))
            if execution_failure == "simulator":
                self.execution_error = {
                    "source": "tau2_simulator",
                    "type": "ValueError",
                    "message": "empty user reply",
                }
                return 0.0, {}
            if execution_failure == "evaluation":
                raise ValueError("evaluation failed")
            return 1.0, {"ok": True}

    class FakeTau2BenchToolProvider:
        def __init__(self, domain, task_id, data_root=None):
            self.domain = domain
            self.task_id = task_id
            self.data_root = data_root
            self.env = FakeEnv()
            environments.append(self.env)
            self.policy = "policy"
            self.user_query = "user query"

        def reset(self):
            calls.append(("reset", threading.get_ident()))

        def list_openai_tools(self):
            return []

    class FakeAgent:
        def __init__(self):
            calls.append(("build_agent", threading.get_ident()))

    async def fake_run_agent(**kwargs):
        calls.append(("run_agent", threading.get_ident()))
        calls.append(("case_lookup", kwargs.get("case_lookup")))
        assert kwargs["should_stop"]() is False
        environments[-1].terminated = True
        assert kwargs["should_stop"]() is True
        kwargs["initial_llm_request"].update(
            {"messages": [{"role": "user", "content": "initial"}], "tools": []}
        )
        return "final", None, [], {}, 1, None, [], None

    monkeypatch.setattr(module, "_tool_provider_cls", lambda: FakeTau2BenchToolProvider)
    monkeypatch.setattr(module, "_build_agent", lambda *args, **kwargs: FakeAgent())
    monkeypatch.setattr(module, "_configure_tools", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "_run_agent", fake_run_agent)

    case = Case(
        name="tau2_case",
        task_signature="tau2:airline:train:0",
        input={
            "domain": "airline",
            "split": "train",
            "task_id": "0",
            "task_no": 0,
            "data_split": "airline_train",
        },
        rubric=Rubric(name="rubric", description="", criteria=[]),
    )
    executor = VikingBotTau2RolloutExecutor()

    rollout = await executor._execute_one(
        case,
        ExecutionContext(policy_snapshot_id="snapshot", metadata={}),
    )

    if execution_failure is None:
        assert rollout.metadata["reward"] == 1.0
        assert rollout.metadata["execution_error"] is None
    else:
        assert rollout.evaluation.score == 0.0
        assert rollout.metadata["execution_error"]["source"] == f"tau2_{execution_failure}"
    call_values = dict(calls)
    assert call_values["case_lookup"] == {
        "benchmark": "tau2",
        "strict": True,
        "case_names": ["tau2_case", "tau2_airline_train_0"],
        "domain": "airline",
        "split": "train",
        "data_split": "airline_train",
        "task_no": 0,
        "task_id": "0",
        "case_name": "tau2_case",
        "task_signature": "tau2:airline:train:0",
        "original_case_name": None,
        "expected_fields": {
            "input.domain": "airline",
            "input.split": "train",
            "input.data_split": "airline_train",
            "input.task_no": 0,
            "input.task_id": "0",
        },
    }
    assert rollout.metadata["initial_llm_request"] == {
        "messages": [{"role": "user", "content": "initial"}],
        "tools": [],
    }
    call_threads = call_values
    assert call_threads["reset"] != event_loop_thread
    assert call_threads["build_agent"] != event_loop_thread
    assert call_threads["reward"] != event_loop_thread
    assert call_threads["run_agent"] == event_loop_thread


@pytest.mark.asyncio
@pytest.mark.parametrize("split", ["train", "test"])
@pytest.mark.parametrize("keep_default_tools", [False, True])
async def test_tau2_run_agent_loads_no_skills(tmp_path, split, keep_default_tools):
    from vikingbot.agent.context import ContextBuilder
    from vikingbot.agent.tools.registry import ToolRegistry
    from vikingbot.config.schema import SessionKey

    import benchmark.tau2.train.rollout_executor_vikingbot as module

    for name in ("experience_loader", "task_case_experience", "other_skill"):
        folder = tmp_path / "skills" / name
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(
            f"---\nname: {name}\nalways: true\n---\n{name} instructions", encoding="utf-8"
        )
    context = ContextBuilder(tmp_path, remote_skills_summary="REMOTE_SKILL_SENTINEL")
    context._templates_ensured = True
    context._get_identity = AsyncMock(return_value="Test agent")
    from vikingbot.session.manager import SessionManager

    agent = SimpleNamespace(
        sessions=SessionManager(tmp_path),
        context=context,
        tools=ToolRegistry(),
        _run_agent_loop=AsyncMock(return_value=(None, None, [], {}, 1)),
    )

    async def run_loop(**kwargs):
        kwargs["on_initial_llm_request"](
            kwargs["messages"], agent.tools.get_definitions(ov_tools_enable=False)
        )
        return None, None, [], {}, 1

    agent._run_agent_loop.side_effect = run_loop
    initial_request = {}
    module._configure_tools(
        agent,
        SimpleNamespace(list_openai_tools=lambda: []),
        keep_default_tools=keep_default_tools,
        data_split=f"airline_{split}",
    )

    await module._run_agent(
        agent=agent,
        system_prompt="Business policy remains in context",
        user_prompt="Please help with my task",
        session_key=SessionKey(type="cli", channel_id="test", chat_id=f"advice-{split}"),
        sender_id="test_user",
        keep_default_tools=keep_default_tools,
        initial_llm_request=initial_request,
    )

    kwargs = agent._run_agent_loop.await_args.kwargs
    if keep_default_tools:
        assert "done" in agent._tau2_dag_runtime.review_tools
        assert "transfer_to_human_agents" in agent._tau2_dag_runtime.review_tools
        assert "communicate_with_user" not in agent._tau2_dag_runtime.review_tools
    prompt = kwargs["messages"][0]["content"]
    assert initial_request["messages"] == kwargs["messages"]
    assert initial_request["tools"] == []
    assert "### Skill:" not in prompt
    assert "ask_for_advice" not in agent.tools.tool_names
    assert "You MUST call `ask_for_advice`" not in prompt
    for excluded in (
        "experience_loader",
        "task_case_experience",
        "other_skill",
        "REMOTE_SKILL_SENTINEL",
        "Required skill:",
    ):
        assert excluded not in prompt
    assert kwargs["messages"][1]["content"] == "Business policy remains in context"
    assert kwargs["inject_write_experience"] is False
    assert kwargs["ov_tools_enable"] is False
    assert (kwargs["experience_context_provider"] is not None) == keep_default_tools


@pytest.mark.asyncio
async def test_tau2_run_agent_retains_partial_work_on_model_error(tmp_path):
    from vikingbot.agent.loop import AgentLLMError

    import benchmark.tau2.train.rollout_executor_vikingbot as module

    completed_tools = [{"tool_name": "get_user_details", "args": "{}", "result": "known"}]
    usage = {"total_tokens": 12}
    from benchmark.tau2.train.dag_experience_runtime import Tau2DagExperienceRuntime

    runtime = Tau2DagExperienceRuntime()
    runtime.close = AsyncMock()
    from vikingbot.session.manager import SessionManager

    agent = SimpleNamespace(
        sessions=SessionManager(tmp_path),
        context=SimpleNamespace(build_messages=AsyncMock(return_value=[])),
        _tau2_dag_runtime=runtime,
        _run_agent_loop=AsyncMock(
            side_effect=AgentLLMError(
                "model failed", tools_used=completed_tools, usage=usage, iteration=2
            )
        ),
    )
    initial_request = {}
    model_error = agent._run_agent_loop.side_effect

    async def fail_model(**kwargs):
        kwargs["on_initial_llm_request"](kwargs["messages"], [])
        raise model_error

    agent._run_agent_loop.side_effect = fail_model
    result = await module._run_agent(
        agent=agent,
        system_prompt="policy",
        user_prompt="question",
        session_key=module._vikingbot_imports()["SessionKey"](
            type="cli", channel_id="test", chat_id="x"
        ),
        sender_id="user",
        keep_default_tools=True,
        initial_llm_request=initial_request,
    )
    assert result[0] is None and result[1] is None
    assert result[2] == completed_tools
    assert result[3] == usage
    assert result[4] == 2
    assert result[7] == {
        "source": "vikingbot_llm",
        "type": "AgentLLMError",
        "message": "model failed",
    }
    runtime.close.assert_awaited_once()
    assert initial_request == {
        "capture_boundary": "agent_provider",
        "messages": [{"role": "system", "content": "policy"}],
        "tools": [],
    }


@pytest.mark.asyncio
async def test_tau2_batch_returns_failed_model_rollout_without_aborting_others(monkeypatch):
    import benchmark.tau2.train.rollout_executor_vikingbot as module

    rewards = []

    class Provider:
        def __init__(self, domain, task_id, data_root=None):
            self.policy = "policy"
            self.user_query = "question"
            self.env = SimpleNamespace(_get_reward=self.reward, execution_error=None)
            self.task_id = task_id

        def reward(self):
            assert self.task_id != "bad", "Do not evaluate a fatal model failure"
            rewards.append(self.task_id)
            return 1.0, {"ok": True}

        def reset(self):
            pass

        def list_openai_tools(self):
            return []

    async def run_agent(**kwargs):
        error = None
        if kwargs["case_lookup"]["task_id"] == "bad":
            error = {"source": "vikingbot_llm", "type": "AgentLLMError", "message": "model failed"}
        return (None if error else "done", None, [], {}, 1, None, [], error)

    monkeypatch.setattr(module, "_tool_provider_cls", lambda: Provider)
    monkeypatch.setattr(module, "_build_agent", lambda *a, **kw: SimpleNamespace())
    monkeypatch.setattr(module, "_configure_tools", lambda *a, **kw: None)
    monkeypatch.setattr(module, "_run_agent", run_agent)
    cases = [
        Case(
            name=task,
            task_signature=f"tau2:{task}",
            input={
                "domain": "airline",
                "split": "train",
                "task_id": task,
                "task_no": i,
                "data_split": "airline_train",
            },
            rubric=Rubric("reward", "", []),
        )
        for i, task in enumerate(("bad", "good"))
    ]
    executor = module.VikingBotTau2RolloutExecutor()
    results = await executor.execute(cases, _policy_set(), ExecutionContext(policy_snapshot_id="p"))
    assert len(results) == 2
    assert results[0].evaluation.passed is False
    assert results[0].metadata["execution_error"]["source"] == "vikingbot_llm"
    assert results[1].evaluation.passed is True
    assert results[1].metadata["execution_error"] is None
    assert rewards == ["good"]


@pytest.fixture
def initial_observation_env(monkeypatch):
    from tau2.gym.gym_agent import GymAgent

    from benchmark.tau2.common.tau2_env import tau2_environment as module

    # Use the real observation property, without starting a simulator or model call.
    agent = GymAgent.__new__(GymAgent)
    agent._lock = threading.Lock()
    agent._observation = []
    clock = SimpleNamespace(now=0.0)

    def sleep(seconds):
        clock.now += seconds

    def format_observation(messages):
        assert agent._lock.locked()
        assert messages is not agent._observation  # Read a snapshot while holding the lock.
        return "\n".join(messages)

    env = SimpleNamespace(
        _agent=agent,
        _simulation_done=threading.Event(),
        _format_observation=Mock(side_effect=format_observation),
    )
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep))
    monkeypatch.setattr(module, "_GYM_INITIAL_OBSERVATION_TIMEOUT_SECONDS", 0.03, raising=False)
    monkeypatch.setattr(module, "_GYM_INITIAL_OBSERVATION_POLL_SECONDS", 0.01, raising=False)
    return module, env, clock


def test_tau2_initial_observation_default_wait_matches_ported_branch():
    from benchmark.tau2.common.tau2_env import tau2_environment as module

    assert module._GYM_INITIAL_OBSERVATION_TIMEOUT_SECONDS == 120.0


def test_tau2_initial_observation_preserves_nonempty_reply(initial_observation_env):
    module, env, clock = initial_observation_env
    assert (
        module._wait_for_gym_initial_observation(env, "user: already ready")
        == "user: already ready"
    )
    env._format_observation.assert_not_called()
    assert clock.now == 0.0


@pytest.mark.parametrize("initial", ["", "  \n", None])
def test_tau2_initial_observation_waits_for_publication(initial_observation_env, initial):
    module, env, clock = initial_observation_env

    def publish(seconds):
        clock.now += seconds
        with env._agent._lock:
            env._agent._observation = ["user: published after reset"]

    module.time.sleep = publish
    assert module._wait_for_gym_initial_observation(env, initial) == "user: published after reset"
    assert clock.now == 0.01
    assert not env._agent._lock.locked()


def test_tau2_initial_observation_missing_reply_times_out(initial_observation_env):
    module, env, clock = initial_observation_env
    with pytest.raises(RuntimeError, match="timed out waiting for its initial observation"):
        module._wait_for_gym_initial_observation(env, "")
    assert clock.now == pytest.approx(0.03)
    assert not env._agent._lock.locked()


def test_tau2_initial_observation_lock_acquisition_is_bounded(initial_observation_env):
    module, env, _ = initial_observation_env
    lock = SimpleNamespace(acquire=Mock(return_value=False), release=Mock())
    env._agent._lock = lock
    with pytest.raises(RuntimeError, match="timed out waiting"):
        module._wait_for_gym_initial_observation(env, "")
    lock.acquire.assert_called_once_with(timeout=0.03)
    lock.release.assert_not_called()
    env._format_observation.assert_not_called()


def test_tau2_initial_observation_releases_lock_on_formatter_error(initial_observation_env):
    module, env, _ = initial_observation_env
    error = ValueError("cannot format observation")
    env._format_observation.side_effect = error
    with pytest.raises(ValueError) as raised:
        module._wait_for_gym_initial_observation(env, "")
    assert raised.value is error
    assert not env._agent._lock.locked()


def test_tau2_initial_observation_stops_when_simulation_ends(initial_observation_env):
    module, env, clock = initial_observation_env
    env._simulation_done.set()
    with pytest.raises(RuntimeError, match="ended before publishing"):
        module._wait_for_gym_initial_observation(env, "")
    env._format_observation.assert_not_called()
    assert clock.now == 0.0


@pytest.mark.parametrize("missing", ["agent", "lock", "formatter"])
def test_tau2_initial_observation_requires_gym_interface(initial_observation_env, missing):
    module, env, clock = initial_observation_env
    if missing == "agent":
        env._agent = None
    elif missing == "lock":
        env._agent._lock = None
    else:
        env._format_observation = None
    with pytest.raises(RuntimeError, match="returned an empty initial observation"):
        module._wait_for_gym_initial_observation(env, "")
    assert clock.now == 0.0


def test_tau2_gym_reset_uses_late_published_first_message(initial_observation_env, monkeypatch):
    module, gym, clock = initial_observation_env
    task = SimpleNamespace(evaluation_criteria=[], user_scenario="scenario")
    gym._orchestrator = None
    gym._get_orchestrator = Mock(return_value=SimpleNamespace(run=lambda: None))
    gym.reset = Mock(
        return_value=("", {"task": task, "simulation_run": "{}", "policy": "policy", "tools": []})
    )
    monkeypatch.setattr(module, "AgentGymEnv", lambda **kwargs: gym)
    monkeypatch.setattr(module, "_install_tau2_litellm_rate_limit_retry", lambda: None)
    monkeypatch.setattr(module, "_install_tau2_litellm_unknown_cost_suppression", lambda: None)

    def publish(seconds):
        clock.now += seconds
        with gym._agent._lock:
            gym._agent._observation = ["user: hello"]

    module.time.sleep = publish
    env = module.Tau2BenchEnv("airline", "offline-case")
    env.reset()
    assert env.user_query == "hello"
    assert env.task is task and env.policy == "policy"
    assert env.execution_error is None
    gym.reset.assert_called_once_with()


@pytest.mark.parametrize("backend", ["vikingbot", "native"])
def test_tau2_message_builders_preserve_real_reward_text_policy_and_stop(backend):
    from benchmark.tau2.train.rollout_executor_native import _build_rollout_messages_from_simulation
    from benchmark.tau2.train.rollout_executor_vikingbot import _build_rollout_messages

    user_text = "task_success: what does this mean? The policy says to confirm first."
    if backend == "vikingbot":
        messages = _build_rollout_messages(
            system_prompt="Obtain confirmation before cancellation.",
            user_prompt=user_text,
            tools_used=[
                {
                    "tool_name": "communicate_with_user",
                    "args": {"content": "That is an evaluation field; do you confirm?"},
                    "result": "###STOP###",
                }
            ],
            final_content=None,
        )
    else:
        messages = _build_rollout_messages_from_simulation(
            simulation=SimpleNamespace(
                messages=[
                    SimpleNamespace(
                        role="system", content="Obtain confirmation before cancellation."
                    ),
                    SimpleNamespace(role="user", content=user_text),
                    SimpleNamespace(
                        role="assistant", content="That is an evaluation field; do you confirm?"
                    ),
                    SimpleNamespace(role="user", content="###STOP###"),
                ]
            )
        )
    assert [m.role for m in messages] == ["user", "user", "assistant", "user"]
    assert messages[0].content == "system:\nObtain confirmation before cancellation."
    assert messages[1].content == user_text
    assert messages[-1].content == "###STOP###"
    assert "tau2-reward" not in {m.id for m in messages}


def test_native_executor_keeps_termination_in_structured_evaluation(monkeypatch):
    import tau2.run
    from tau2.registry import registry

    from benchmark.tau2.train import rollout_executor_native as native
    from openviking.session.train.components.trajectory_observation import (
        trajectory_evaluation_summary,
    )

    simulation = SimpleNamespace(
        reward_info=SimpleNamespace(reward=0.0, info={"note": "Simulation stopped at step budget"}),
        termination_reason="max_steps",
        messages=[SimpleNamespace(role="user", content="Please update my booking")],
    )
    monkeypatch.setattr(native, "_ensure_tau2_llm_api_bases", lambda: None)
    monkeypatch.setattr(native, "_install_tau2_litellm_rate_limit_retry", lambda: None)
    monkeypatch.setattr(native, "_resolve_llm_runtime_config", lambda _: ("test", {}, "test", {}))
    monkeypatch.setattr(registry, "get_tasks_loader", lambda _: lambda: [SimpleNamespace(id="1")])
    monkeypatch.setattr(tau2.run, "run_task", lambda **kwargs: simulation)
    case = Case(
        "case",
        "update",
        {"domain": "airline", "task_id": "1", "task_no": 0, "data_split": "train"},
        Rubric("r", "", []),
    )
    rollout = native.NativeTau2RolloutExecutor(memory_enabled=False)._execute_one_sync(
        case,
        ExecutionContext("snapshot"),
        0,
    )
    assert [m.content for m in rollout.messages] == ["Please update my booking"]
    assert rollout.evaluation.metadata["termination_reason"] == "max_steps"
    assert trajectory_evaluation_summary(rollout.evaluation)["details"] == {
        "termination_reason": "max_steps",
        "info": {"note": "Simulation stopped at step budget"},
    }


def test_named_experience_messages_keep_capture_text_and_tool_positions():
    from copy import deepcopy

    from benchmark.tau2.train.rollout_executor_vikingbot import _build_rollout_messages
    from openviking.session.memory.experience.trajectory_context import trajectory_source_steps

    note = "[Experience Reminder]\n\n### 建议 1\n\n**建议**\nKeep the original guidance."
    captures = [
        {"content": note, "after_tool_count": 0, "advice_names": ["require_chinese", "ask_reason"]},
        {"content": note, "after_tool_count": 1, "advice_names": ["check_total"]},
    ]
    before = deepcopy(captures)
    messages = _build_rollout_messages(
        system_prompt="policy",
        user_prompt="request",
        final_content=None,
        tools_used=[{"tool_name": "get_booking", "args": {"id": "A"}, "result": "found"}],
        experience_messages=captures,
    )
    steps = trajectory_source_steps(messages)
    assert [x["type"] for x in steps] == [
        "user",
        "user",
        "experience_reminder",
        "tool:get_booking",
        "experience_reminder",
    ]
    assert steps[2]["content"] == note.replace(
        "[Experience Reminder]", "[Experience Reminder] require_chinese, ask_reason", 1
    )
    assert steps[4]["content"] == note.replace(
        "[Experience Reminder]", "[Experience Reminder] check_total", 1
    )
    assert captures == before


@pytest.mark.asyncio
async def test_run_agent_captures_names_without_changing_online_advice(tmp_path):
    from copy import deepcopy

    from test_dag_experience_runtime import advice, client
    from vikingbot.config.schema import SessionKey
    from vikingbot.session.manager import SessionManager

    from benchmark.tau2.train.dag_experience_runtime import Tau2DagExperienceRuntime
    from benchmark.tau2.train.rollout_executor_vikingbot import _run_agent

    runtime = Tau2DagExperienceRuntime()
    runtime.client = client(advice(slot="require_chinese", text="Communicate clearly."))
    inserted = []

    async def run_loop(**kwargs):
        async for note in kwargs["experience_context_provider"](
            kwargs["messages"], before_tools=False
        ):
            message = {"role": "user", "content": note}
            kwargs["messages"].append(message)
            before = deepcopy(message)
            kwargs["captured_experience_messages"].append({**message, "after_tool_count": 0})
            await kwargs["experience_context_injected"](note)
            assert message == before
            inserted.append(note)
        return None, None, [], {}, 1

    agent = SimpleNamespace(
        sessions=SessionManager(tmp_path),
        context=SimpleNamespace(build_messages=AsyncMock(return_value=[])),
        _tau2_dag_runtime=runtime,
        _run_agent_loop=AsyncMock(side_effect=run_loop),
    )
    result = await _run_agent(
        agent=agent,
        system_prompt="policy",
        user_prompt="request",
        session_key=SessionKey(type="cli", channel_id="test", chat_id="names"),
        sender_id="user",
        keep_default_tools=True,
    )
    assert len(inserted) == 1 and "require_chinese" not in inserted[0]
    assert result[5] == inserted[0]
    assert result[6] == [
        {
            "role": "user",
            "content": inserted[0],
            "after_tool_count": 0,
            "advice_names": ["require_chinese"],
        }
    ]
