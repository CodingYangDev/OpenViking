"""Tau2 done cannot preempt the customer; rollout iteration defaults stay aligned."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from benchmark.tau2.common.tau2_env import tau2_environment as env_module
from benchmark.tau2.train.rollout_executor import make_tau2_rollout_executor
from benchmark.tau2.train.rollout_executor_vikingbot import (
    VikingBotTau2RolloutExecutor,
    _AsyncRWLock,
    _make_tau2_tool,
)
from openviking.session.train.batch_runner import (
    BatchTrainEvalConfig,
    _train_rollout_cache_key_prefix,
)
from openviking.session.train.domain import DEFAULT_MAX_ROLLOUT_ITERATIONS
from openviking.session.train.run_batch_train_eval import parse_args


@pytest.fixture
def gym(monkeypatch):
    calls = []
    raw = SimpleNamespace(
        _orchestrator=None,
        _get_orchestrator=lambda: SimpleNamespace(run=lambda: None),
        replies=iter([("Please add two checked bags.", False), ("###STOP###", True)]),
    )
    done_schema = {
        "type": "function",
        "function": {"name": "done", "description": "Finish", "parameters": {}},
    }
    raw.reset = Mock(
        return_value=(
            "user: Hello",
            {
                "task": SimpleNamespace(evaluation_criteria=[], user_scenario="scenario"),
                "simulation_run": None,
                "policy": "policy",
                "tools": [SimpleNamespace(openai_schema=done_schema)],
            },
        )
    )

    def step(action):
        calls.append(action)
        if action.startswith("{"):
            name = json.loads(action)["name"]
            assert name != "done", "Premature done must never reach the simulator"
            return "tool: updated", 0.0, name == "transfer_to_human_agents", False, {}
        response, terminal = next(raw.replies)
        return f"user: {response}", 0.0, terminal, False, {}

    raw.step = Mock(side_effect=step)
    monkeypatch.setattr(env_module, "AgentGymEnv", lambda **kwargs: raw)
    monkeypatch.setattr(env_module, "_install_tau2_litellm_rate_limit_retry", lambda: None)
    monkeypatch.setattr(env_module, "_install_tau2_litellm_unknown_cost_suppression", lambda: None)
    adapter = env_module._GymTau2BenchEnv("airline", "offline")
    adapter.reset()
    return adapter, raw, calls


def test_done_requires_customer_termination_not_just_a_successful_write(gym):
    adapter, raw, calls = gym
    assert "communicate_with_user" in adapter.tool_schemas[0]["function"]["description"]
    assert adapter.tool_call("done", {}).startswith("Error: USER_CONVERSATION_ACTIVE")
    raw.step.assert_not_called()

    assert adapter.tool_call("update_reservation_flights", {}) == "updated"
    assert adapter.tool_call("done", {}).startswith("Error:")
    assert not adapter.terminated
    assert len(calls) == 1

    assert adapter.tool_call("communicate_with_user", {"content": "Flights changed."}) == (
        "Please add two checked bags."
    )
    assert adapter.tool_call("done", {}).startswith("Error:")
    assert not adapter.terminated
    adapter.tool_call("update_reservation_baggages", {})
    assert adapter.tool_call("done", {}).startswith("Error:")
    assert adapter.tool_call("communicate_with_user", {"content": "Bags added."}) == "###STOP###"
    assert adapter.terminated
    assert adapter.tool_call("done", {}) == "Task Terminated"
    assert len(calls) == 4


@pytest.mark.parametrize("marker", ["###STOP###", "###TRANSFER###", "###OUT-OF-SCOPE###"])
def test_done_still_allows_existing_terminal_states(gym, marker):
    adapter, raw, calls = gym
    raw.replies = iter([(marker, True)])
    assert adapter.tool_call("communicate_with_user", {"content": "Result"}) == marker
    assert adapter.tool_call("done", {}) == "Task Terminated"
    assert len(calls) == 1


def test_done_after_human_transfer_does_not_require_another_user_message(gym):
    adapter, _, calls = gym
    adapter.tool_call("transfer_to_human_agents", {})
    assert adapter.terminated
    assert adapter.tool_call("done", {}) == "Task Terminated"
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("done_first", [True, False])
async def test_done_cannot_bypass_guard_in_a_parallel_write_batch(gym, done_first):
    adapter, _, calls = gym
    lock = _AsyncRWLock()
    provider = SimpleNamespace(call_tool=adapter.tool_call)

    def tool(name):
        return _make_tau2_tool(
            {"function": {"name": name, "parameters": {"type": "object", "properties": {}}}},
            provider,
            tool_lock=lock,
            is_write_tool=True,
        )

    names = (
        ["done", "update_reservation_flights"]
        if done_first
        else ["update_reservation_flights", "done"]
    )
    results = await asyncio.gather(*(tool(name).execute(None) for name in names))
    assert results[names.index("done")].startswith("Error:")
    assert results[names.index("update_reservation_flights")] == "updated"
    assert not adapter.terminated
    assert len(calls) == 1


def test_all_rollout_entry_defaults_are_100_and_old_30_cache_does_not_match(monkeypatch):
    monkeypatch.setattr(
        "sys.argv", ["run_batch_train_eval", "--dataset", "tau2", "--domain", "airline"]
    )
    config = BatchTrainEvalConfig(dataset="tau2", domain="airline")
    assert DEFAULT_MAX_ROLLOUT_ITERATIONS == 100
    assert parse_args().max_iterations == 100
    assert config.max_iterations == 100
    assert VikingBotTau2RolloutExecutor().max_iterations == 100
    assert make_tau2_rollout_executor(backend="vikingbot").max_iterations == 100
    assert _train_rollout_cache_key_prefix(config) != _train_rollout_cache_key_prefix(
        BatchTrainEvalConfig(dataset="tau2", domain="airline", max_iterations=30)
    )


def test_explicit_iteration_limit_is_preserved(monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_batch_train_eval",
            "--dataset",
            "tau2",
            "--domain",
            "airline",
            "--max-iterations",
            "17",
        ],
    )
    assert parse_args().max_iterations == 17
    assert (
        BatchTrainEvalConfig(dataset="tau2", domain="airline", max_iterations=17).max_iterations
        == 17
    )
    assert (
        make_tau2_rollout_executor(
            backend="vikingbot", options={"max_iterations": 17}
        ).max_iterations
        == 17
    )


def test_standalone_runner_uses_same_default_without_initializing_real_services(
    monkeypatch, tmp_path
):
    from benchmark.tau2.vikingbot.scripts import vikingbot_tau2_runner as runner

    config = SimpleNamespace(
        bot_data_path=tmp_path,
        workspace_path=tmp_path,
        agents=SimpleNamespace(model="offline", memory_window=20, gen_image_model="offline"),
        tools=SimpleNamespace(web=SimpleNamespace(search=SimpleNamespace(api_key="")), exec=None),
    )
    monkeypatch.setattr(runner, "ensure_config", lambda _: config)
    monkeypatch.setattr(runner, "_init_bot_data", lambda _: None)
    for name in (
        "MessageBus",
        "SessionManager",
        "SandboxManager",
        "_make_provider",
        "get_source_workspace_path",
    ):
        monkeypatch.setattr(runner, name, Mock(return_value=None))
    constructor = Mock(return_value=SimpleNamespace())
    monkeypatch.setattr(runner, "AgentLoop", constructor)

    runner._build_agent(None)

    assert constructor.call_args.kwargs["max_iterations"] == 100
