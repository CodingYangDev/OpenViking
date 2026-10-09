# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from openviking.message import Message, TextPart
from openviking.session.train import (
    Case,
    ExperienceSet,
    PipelineContext,
    PipelineEpochResult,
    Rollout,
    Rubric,
    RubricEvaluation,
)
from openviking.session.train.components.experience_learner import RemoteExperienceLearner
from openviking.session.train.components.report_builder import PipelineReportBuilder
from openviking.session.train.components.reporter import ConsolePipelineReporter
from openviking.session.train.components.rollout_artifact_recorder import RolloutArtifactRecorder
from openviking.session.train.components.session_commit import SessionCommitPolicyTrainer


@pytest.fixture
def gym_adapter(monkeypatch):
    from tau2.gym.gym_agent import AgentGymEnv

    from benchmark.tau2.common.tau2_env import tau2_environment as module

    class FakeGym:
        # Exercise the real exception-swallowing thread entry, without any live model calls.
        _run_orchestrator = AgentGymEnv._run_orchestrator
        _get_reward = AgentGymEnv._get_reward

        def __init__(self, **kwargs):
            self._orchestrator = None
            self._simulation_run = None
            self._simulation_done = threading.Event()
            self._log = Mock()
            self.outcome = None

        def _get_orchestrator(self):
            outcome = self.outcome

            def run():
                if isinstance(outcome, BaseException):
                    raise outcome
                return outcome

            return SimpleNamespace(run=run)

        def reset(self):
            self._orchestrator = self._get_orchestrator()
            self._run_orchestrator()
            return "user: Hello", {
                "task": SimpleNamespace(user_scenario={}, evaluation_criteria={}),
                "simulation_run": "{}",
                "policy": "test policy",
                "tools": [],
            }

    monkeypatch.setattr(module, "AgentGymEnv", FakeGym)
    monkeypatch.setattr(module, "_install_tau2_litellm_rate_limit_retry", lambda: None)
    monkeypatch.setattr(module, "_install_tau2_litellm_unknown_cost_suppression", lambda: None)
    return module


def test_gym_captures_original_error_before_it_becomes_zero_reward(gym_adapter):
    env = gym_adapter.Tau2BenchEnv("airline", "0")
    error = ValueError("UserMessage must have either content or tool_calls")
    env._impl.env.outcome = error
    env.reset()

    assert env.execution_error == {
        "source": "tau2_simulator",
        "type": "ValueError",
        "message": str(error),
    }
    assert env._impl.env._simulation_done.is_set()
    assert env._get_reward() == (0.0, {})  # Existing test scoring is unchanged.


def test_gym_error_is_scoped_to_one_orchestrator_and_cleared_by_reset(gym_adapter):
    env = gym_adapter.Tau2BenchEnv("airline", "0")
    other = gym_adapter.Tau2BenchEnv("airline", "1")
    env._impl.env.outcome = ValueError("failed")
    env.reset()
    old_orchestrator = env._impl.env._orchestrator
    other.reset()
    assert env.execution_error is not None
    assert other.execution_error is None

    env._impl.env.outcome = None
    env.reset()
    assert env.execution_error is None
    assert env._get_reward() == (0.0, {})  # No inference from empty evaluation/results.
    with pytest.raises(ValueError):
        old_orchestrator.run()
    assert env.execution_error is None  # A late old-run failure cannot taint the new run.


def test_gym_tracking_does_not_turn_cancellation_into_a_training_skip(gym_adapter):
    env = gym_adapter.Tau2BenchEnv("airline", "0")
    env._impl.env.outcome = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        env.reset()
    assert env.execution_error is None


def make_rollout(index, *, execution_error=None, score=0.0):
    return Rollout(
        case=Case(
            name=f"tau2_airline_train_{index}_t0",
            task_signature=f"tau2:airline:train:{index}:trial:0",
            input={"data_split": "airline_train", "task_no": index, "task_id": str(index)},
            rubric=Rubric("reward", "reward", []),
        ),
        messages=[Message(id="m1", role="user", parts=[TextPart(text="Keep original rollout")])],
        policy_snapshot_id="snapshot",
        evaluation=RubricEvaluation(score > 0, score, [], []),
        metadata={"execution_error": execution_error, "evaluation_result": {}, "iterations": 30},
    )


@pytest.fixture
def training():
    async def commit(session_id, **kwargs):
        return {"task_id": session_id, "archive_uri": f"archive:{session_id}"}

    async def task(task_id):
        result = (
            {"case_count": 1, "trajectory_count": 2, "applied_count": 1}
            if task_id == "learn-1"
            else {"trajectory_uris": [f"traj:{task_id}"]}
        )
        return {"status": "completed", "result": result}

    client = SimpleNamespace(
        create_session=AsyncMock(),
        batch_add_messages=AsyncMock(),
        commit_session=AsyncMock(side_effect=commit),
        get_task=AsyncMock(side_effect=task),
        learn_experiences=AsyncMock(return_value={"task_id": "learn-1"}),
        read=AsyncMock(),
    )
    events = SimpleNamespace(record=AsyncMock())
    return SimpleNamespace(
        client=client,
        policies=ExperienceSet("viking://user/default/memories/experiences", []),
        context=PipelineContext(execution_metadata={"epoch": 0, "training": True}),
        trainer=SessionCommitPolicyTrainer(client, run_id="skip-test", event_recorder=events),
        learner=RemoteExperienceLearner(client),
        events=events,
    )


@pytest.mark.asyncio
async def test_execution_error_skips_commit_but_normal_zero_and_positive_scores_train(training):
    invalid = make_rollout(0, execution_error={"source": "tau2_simulator", "message": "failed"})
    rollouts = [invalid, make_rollout(1), make_rollout(2, score=1.0)]
    batch = await training.trainer.train_rollouts(rollouts, training.policies, training.context)
    metadata = batch.apply_result.metadata
    assert metadata["committed_rollout_count"] == 2
    assert metadata["skipped_rollout_count"] == 1
    assert batch.apply_result.errors == []
    assert len(batch.analyses) == 3  # Keep artifacts and metric denominators intact.
    assert training.client.create_session.await_count == 2
    assert training.client.batch_add_messages.await_count == 2
    assert training.client.commit_session.await_count == 2
    skipped = metadata["commit_results"][0]
    assert skipped["task_status"] == "skipped"
    assert skipped["execution_error"] == invalid.metadata["execution_error"]
    assert not skipped["session_id"] and not skipped["archive_uri"]
    assert skipped["trajectory_uris"] == []
    assert training.events.record.await_args_list[0].args[0] == "train_commit_skipped"

    result = await training.learner.learn([batch], training.policies, training.context)
    expected_uris = metadata["trajectory_uris"]
    assert len(expected_uris) == 2
    training.client.learn_experiences.assert_awaited_once_with(
        trajectory_uris=expected_uris, max_cases=1000, wait=False
    )
    assert result.apply_result.metadata["committed_rollout_count"] == 2
    assert result.apply_result.metadata["skipped_rollout_count"] == 1

    report = PipelineReportBuilder().train_epoch_report(
        PipelineEpochResult(
            epoch=0,
            analyses=result.analyses,
            gradients=[],
            plan=result.plan,
            apply_result=result.apply_result,
        )
    )
    assert report["committed_rollout_count"] == 2
    assert report["skipped_rollout_count"] == 1
    assert report["case_count"] == 3
    assert report["accuracy"] == pytest.approx(1 / 3)
    assert report["errors"] == []


@pytest.mark.asyncio
async def test_all_skipped_creates_no_sessions_or_learning_and_retains_artifacts(
    training, tmp_path
):
    invalid = make_rollout(0, execution_error={"source": "tau2_simulator", "message": "failed"})
    recorder = RolloutArtifactRecorder(run_dir=tmp_path, client=training.client)
    recorder.record_train_rollouts(epoch=0, rollouts=[invalid])
    training.trainer.event_recorder = SimpleNamespace(record=recorder.record_train_commit_result)
    batch = await training.trainer.train_rollouts([invalid], training.policies, training.context)
    await recorder.record_train_epoch(
        epoch=0,
        analyses=batch.analyses,
        commit_results=batch.apply_result.metadata["commit_results"],
    )
    result = await training.learner.learn([batch], training.policies, training.context)
    assert result.apply_result.metadata["skipped_rollout_count"] == 1
    assert result.apply_result.metadata["committed_rollout_count"] == 0
    assert result.apply_result.errors == []
    for method in [
        "create_session",
        "batch_add_messages",
        "commit_session",
        "learn_experiences",
        "read",
    ]:
        getattr(training.client, method).assert_not_awaited()
    status = json.loads(next(tmp_path.rglob("status.json")).read_text())
    assert status["artifact_state"] == "commit_skipped"
    assert status["commit_error"] is None
    raw = json.loads(next(tmp_path.rglob("rollout.json")).read_text())
    assert raw["metadata"]["execution_error"] == invalid.metadata["execution_error"]
    saved_commit = json.loads(next(tmp_path.rglob("commit_result.json")).read_text())
    assert saved_commit["skip_reason"] == "rollout_execution_error"
    assert next(tmp_path.rglob("messages.json")).exists()


def test_console_reporter_separates_skipped_rollouts_from_commits(capsys):
    reporter = ConsolePipelineReporter(use_rich=False)
    reporter.on_train_report(
        report={"epoch": 0, "committed_rollout_count": 2, "skipped_rollout_count": 1, "errors": []},
        context=PipelineContext(eval_each_epoch_case_loader=object()),
    )
    output = capsys.readouterr().out
    assert "commits=2" in output
    assert "skipped=1" in output
    assert "errors=0" in output
