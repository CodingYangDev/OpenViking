from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from benchmark.tau2.train.rollout_executor_native import NativeTau2RolloutExecutor
from benchmark.tau2.train.rollout_executor_vikingbot import (
    VikingBotTau2RolloutExecutor,
    _build_system_prompt,
)


@pytest.mark.parametrize("keep_default_tools", [False, True])
@pytest.mark.parametrize(
    "policy",
    [
        "Help the customer with their reservation; they communicate in English and French.",
        "The user explicitly requests Chinese replies for this interaction.",
    ],
)
def test_vikingbot_preserves_task_policy_without_forcing_a_response_language(
    policy, keep_default_tools
):
    prompt = _build_system_prompt(policy, keep_default_tools=keep_default_tools)
    assert prompt.startswith(policy)
    assert "Communicate with the user and write the final response in Chinese" not in prompt
    assert "MUST call tool `communicate_with_user`" in prompt
    assert "include the exact original value" in prompt
    assert ("[Experience Reminder]" in prompt) is keep_default_tools


def test_rollout_executors_do_not_expose_a_forced_language_setting():
    for executor in [NativeTau2RolloutExecutor, VikingBotTau2RolloutExecutor]:
        assert "rollout_language" not in {field.name for field in fields(executor)}


@pytest.mark.parametrize("scope_prompt", ["", "Respect the customer's authorized scope."])
def test_native_agent_preserves_source_policy_and_scope_without_forcing_chinese(
    monkeypatch, scope_prompt
):
    import benchmark.tau2.train.rollout_executor_native as native

    policy = "Help the English/French-speaking customer cancel their reservation."

    class FakeAgent:
        def get_init_state(self, message_history=None):
            return SimpleNamespace(system_messages=[SimpleNamespace(role="system", content=policy)])

    agents = {}
    registry = SimpleNamespace(
        get_agents=lambda: list(agents),
        register_agent=lambda cls, name: agents.update({name: cls}),
    )
    generate = Mock(side_effect=AssertionError("Initialization must not call a model"))
    monkeypatch.setitem(
        sys.modules,
        "tau2.agent.llm_agent",
        SimpleNamespace(LLMAgent=FakeAgent, LLMAgentState=SimpleNamespace),
    )
    monkeypatch.setitem(
        sys.modules,
        "tau2.data_model.message",
        SimpleNamespace(
            AssistantMessage=SimpleNamespace,
            MultiToolMessage=SimpleNamespace,
            SystemMessage=SimpleNamespace,
            UserMessage=SimpleNamespace,
        ),
    )
    monkeypatch.setitem(sys.modules, "tau2.registry", SimpleNamespace(registry=registry))
    monkeypatch.setitem(sys.modules, "tau2.utils.llm_utils", SimpleNamespace(generate=generate))
    monkeypatch.setattr(native, "_NATIVE_AGENT_CONFIGS", {})
    name = native._register_native_memory_agent(
        NativeTau2RolloutExecutor(scope_prompt=scope_prompt)
    )
    state = agents[name]().get_init_state()
    assert [message.content for message in state.system_messages] == [policy] + (
        [scope_prompt] if scope_prompt else []
    )
    generate.assert_not_called()


def test_tau2_service_does_not_forward_a_language_override(monkeypatch):
    from benchmark.tau2.train import service_app

    factory = Mock(return_value=object())
    monkeypatch.setattr(service_app, "create_dataset_service_app", lambda **kwargs: kwargs)
    monkeypatch.setattr(service_app, "make_tau2_rollout_executor", factory)
    app = service_app.create_app(config_path="/tmp/ov.conf", rollout_backend="vikingbot")
    app["make_rollout_executor"]({})
    assert "rollout_language" not in factory.call_args.kwargs
    monkeypatch.setattr(sys, "argv", ["service_app.py"])
    assert not hasattr(service_app.parse_args(), "rollout_language")


@pytest.mark.parametrize("script", ["run_service.sh", "restart_vikingbot_train_eval.sh"])
def test_launchers_no_longer_inherit_memory_output_language_or_expose_language_flag(script):
    root = Path(__file__).resolve().parents[3]
    path = root / "benchmark/tau2/train" / script
    # Help exits before starting services or invoking training/auto-commit.
    completed = subprocess.run(
        ["bash", str(path), "--help"],
        cwd=root,
        env={**os.environ, "OPENVIKING_ENV_FILE": "/dev/null"},
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert "--rollout-language" not in completed.stdout
    source = path.read_text()
    assert "TAU2_ROLLOUT_LANGUAGE" not in source
    assert "output_language_override" not in source
