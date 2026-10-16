"""Initial Tau2 failures retain bounded diagnostics without rerunning the simulator."""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from tau2.data_model.message import AssistantMessage, ToolCall, UserMessage
from tau2.data_model.simulation import TerminationReason

from benchmark.tau2.common.tau2_env import tau2_environment as tau2_env
from openviking.session.train import Case, Rubric
from openviking.session.train.components import dataset_service


@pytest.fixture
def ended_env(monkeypatch):
    orchestrator = SimpleNamespace(
        run=Mock(),
        termination_reason=TerminationReason.USER_STOP,
        step_count=1,
        num_errors=0,
        user_state=SimpleNamespace(messages=[UserMessage(role="user", content="###STOP###")]),
        _openviking_execution_error=None,
    )
    gym = SimpleNamespace(
        _orchestrator=orchestrator,
        _get_orchestrator=Mock(return_value=orchestrator),
        _agent=SimpleNamespace(_lock=threading.Lock()),
        _format_observation=Mock(),
        _simulation_done=threading.Event(),
        reset=Mock(return_value=("", {})),
    )
    gym._simulation_done.set()
    monkeypatch.setattr(tau2_env, "AgentGymEnv", lambda **kwargs: gym)
    monkeypatch.setattr(tau2_env, "_install_tau2_litellm_rate_limit_retry", lambda: None)
    monkeypatch.setattr(tau2_env, "_install_tau2_litellm_unknown_cost_suppression", lambda: None)
    return tau2_env._GymTau2BenchEnv("airline", "23")


def _failed_reset(env):
    with pytest.raises(RuntimeError, match="ended before publishing") as caught:
        env.reset()
    assert isinstance(caught.value.__cause__, RuntimeError)
    env.env.reset.assert_called_once_with()
    return caught.value, json.loads(str(caught.value).split("diagnostics=", 1)[1])


@pytest.mark.parametrize("marker", ["###STOP###", "###TRANSFER###", "###OUT-OF-SCOPE###"])
def test_initial_failure_preserves_user_stop_details(ended_env, marker):
    text = f"A quoted marker: {marker}; I still need help."
    ended_env.env._orchestrator.user_state.messages = [UserMessage(role="user", content=text)]

    _, details = _failed_reset(ended_env)

    assert details["termination_reason"] == "user_stop"
    assert details["step_count"] == 1
    assert details["num_errors"] == 0
    assert details["execution_error"] is None
    assert details["last_user_message"]["content"] == text
    assert details["last_user_message"]["content_chars"] == len(text)
    assert details["last_user_message"]["stop_markers"] == [marker]
    assert details["last_user_message"]["tool_call_count"] == 0


def test_initial_failure_preserves_tool_error_details_without_arguments(ended_env):
    orchestrator = ended_env.env._orchestrator
    orchestrator.termination_reason = TerminationReason.TOO_MANY_ERRORS
    orchestrator.step_count = 20
    orchestrator.num_errors = 10
    orchestrator.user_state.messages = [
        UserMessage(
            role="user",
            content=None,
            tool_calls=[
                ToolCall(id="call", name="book_reservation", arguments={"secret": "private"})
            ],
        ),
        AssistantMessage(role="assistant", content="The latest message is not from the user"),
    ]

    error, details = _failed_reset(ended_env)

    assert details["termination_reason"] == "too_many_errors"
    assert details["num_errors"] == 10
    assert details["step_count"] == 20
    assert details["last_user_message"]["tool_calls"] == ["book_reservation"]
    assert details["last_user_message"]["tool_call_count"] == 1
    assert "private" not in str(error)


def test_initial_failure_reads_invalid_reply_from_user_state_before_trajectory_append(ended_env):
    orchestrator = ended_env.env._orchestrator
    orchestrator.termination_reason = None
    orchestrator.trajectory = [AssistantMessage(role="assistant", content="hello")]
    orchestrator.user_state.messages = [UserMessage(role="user", content=None)]
    orchestrator._openviking_execution_error = {
        "source": "tau2_simulator",
        "type": "ValueError",
        "message": "UserMessage must have either content or tool_calls",
    }

    _, details = _failed_reset(ended_env)

    assert details["termination_reason"] is None
    assert details["last_user_message"]["content_chars"] == 0
    assert details["execution_error"] == orchestrator._openviking_execution_error


def test_initial_failure_without_user_reply_still_preserves_original_exception(ended_env):
    orchestrator = ended_env.env._orchestrator
    orchestrator.termination_reason = None
    orchestrator.user_state.messages = [AssistantMessage(role="assistant", content="hello")]
    orchestrator._openviking_execution_error = {
        "source": "tau2_simulator",
        "type": "RuntimeError",
        "message": "request failed",
    }

    _, details = _failed_reset(ended_env)

    assert details["last_user_message"] is None
    assert details["execution_error"]["message"] == "request failed"


def test_initial_failure_redacts_before_clipping_and_preserves_hidden_stop_marker(ended_env):
    secret = "private" * 1000
    text = f'api_key="{secret}" ' + "a" * 5000 + "###STOP###"
    orchestrator = ended_env.env._orchestrator
    orchestrator.user_state.messages = [UserMessage(role="user", content=text)]
    orchestrator._openviking_execution_error = {
        "source": "tau2_simulator",
        "type": "ValueError",
        "message": f"Authorization: Bearer {secret} " + "x" * 10000,
    }

    error, details = _failed_reset(ended_env)

    assert "private" not in str(error)
    assert len(str(error)) < 5000
    assert details["last_user_message"]["content_chars"] == len(text)
    assert details["last_user_message"]["content"].endswith("...[truncated]")
    assert details["last_user_message"]["stop_markers"] == ["###STOP###"]
    assert details["execution_error"]["message"].endswith("...[truncated]")
    assert secret in orchestrator._openviking_execution_error["message"]  # No state mutation.


def test_initial_failure_handles_missing_orchestrator_state(ended_env):
    ended_env.env._orchestrator = None

    _, details = _failed_reset(ended_env)

    assert details["termination_reason"] is None
    assert details["execution_error"] is None
    assert details["last_user_message"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["###STOP###", 'api_key="private-value" ###STOP###'])
async def test_initial_failure_diagnostics_survive_service_serialization_and_persistence(
    ended_env, tmp_path, monkeypatch, content
):
    ended_env.env._orchestrator.user_state.messages = [UserMessage(role="user", content=content)]
    error, details = _failed_reset(ended_env)
    store = dataset_service.RolloutExecutionStore(spool_dir=tmp_path)
    case = Case("case-23", "task-23", {}, Rubric("rubric", "", []))
    execution = await store.create(case_name=case.name)
    app = FastAPI()
    app.state.rollout_executions = store
    app.state.rollout_semaphore = None
    app.state.rollout_tasks = {}
    app.state.rollout_controls = {}
    executor = AsyncMock(side_effect=error)
    monkeypatch.setattr(dataset_service, "_execute_rollout_request_hosted", executor)
    request = dataset_service.RolloutExecuteRequest(
        case=dataset_service.case_to_dict(case), policy_set={}, execution_context={}
    )

    await dataset_service._run_rollout_execution(
        app, execution.execution_id, request, dataset_service._RolloutControl(time.monotonic() + 60)
    )

    failed = await store.get(execution.execution_id)
    assert failed.status == "failed"
    api_result = dataset_service.execution_to_dict(failed)
    assert json.loads(api_result["error"].split("diagnostics=", 1)[1]) == details
    persisted = json.loads((tmp_path / "failed" / f"{execution.execution_id}.json").read_text())
    assert persisted["error"] == api_result["error"]
    assert "private-value" not in persisted["error"]
    assert await store.count_by_status() == {"running": 0, "completed": 0, "failed": 1}
    executor.assert_awaited_once()


def test_initial_failure_bounds_tool_names_and_keeps_total_count(ended_env):
    ended_env.env._orchestrator.user_state.messages = [
        UserMessage(
            role="user",
            content=None,
            tool_calls=[
                ToolCall(id=str(index), name="n" * 10000, arguments={}) for index in range(50)
            ],
        )
    ]

    _, details = _failed_reset(ended_env)

    message = details["last_user_message"]
    assert message["tool_call_count"] == 50
    assert len(message["tool_calls"]) == 10
    assert all(name == "n" * 100 + "...[truncated]" for name in message["tool_calls"])
