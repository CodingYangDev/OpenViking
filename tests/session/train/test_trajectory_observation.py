from __future__ import annotations

from copy import deepcopy

import pytest
import yaml

from openviking.session.train.domain import Trajectory
from tests.session.train.test_fakes import render_trajectory_template


@pytest.mark.parametrize(
    ("outcome", "evaluation"),
    [
        ("success", {"passed": True, "score": 1.0}),
        ("failure", {"passed": False, "score": 0.0}),
        ("failure", {"passed": False, "score": 0.5}),
        ("failure", {"passed": False, "score": 0.0, "details": {}}),
        ("failure", {"passed": False, "score": 0.0, "details": None}),
        ("unknown", None),
    ],
)
def test_complete_content_preserves_authoritative_evidence_and_exact_observation(
    outcome, evaluation
):
    body = (
        '# Session Context\n\n仅取消经确认的订单。\n\n# Trajectory\n\n1. `tool` {"amount": 123}\n'
    )
    metadata = {
        "context_rules": "仅取消经确认的订单。",
        "steps": '1. `tool` {"amount": 123}\n',
        "case_name": "cancel",
        "source_session_uri": "viking://session/s/archives/1",
        "task_spec": {
            "name": "PRIVATE_TASK_NAME",
            "task_signature": "private:task:signature",
            "input": {"booking_id": "ABC123"},
            "rubric": {"name": "PRIVATE_RUBRIC_NAME", "criteria": [{"name": "task_reward"}]},
            "metadata": {"marker": "PRIVATE_TASK_METADATA"},
        },
        "evaluation": evaluation,
        "evaluation_criteria": [
            {"id": "check:0", "passed": False, "summary": "缺少确认"},
            {"id": "passed", "passed": True},
            {"id": "unknown", "passed": None},
            {"id": "zero", "passed": 0},
            {"id": "text_false", "passed": "false"},
            {"id": "missing"},
            {"id": "check:1", "passed": False, "summary": "遗漏信息"},
        ],
        "experience_execution": {
            "cancel": {
                "slot_values": {"eligible": True, "confirmed": False},
                "executed_nodes": ["eligible"],
                "current_nodes": ["confirmed"],
            }
        },
        "experience_effects": {"positive_ids": [], "negative_ids": [], "weak_ids": ["E1"]},
        "experience_effect_sources": {"E1": "viking://user/u/memories/experiences/cancel.md"},
        "experience_update": {"state": "processing", "attempts": 3},
        "gate_evidence": [{"summary": "not part of the body"}],
        "created_at": "2026-10-12T00:00:00+00:00",
        "unexpected_field": "do not expose",
    }
    trajectory = Trajectory(
        name="run1",
        uri="viking://user/u/memories/trajectories/cancel/run1.md",
        outcome=outcome,
        content=body,
        metadata=metadata,
    )
    before = deepcopy(trajectory)

    content = render_trajectory_template(trajectory)
    header, observed = content.split("\n```\n\n", 1)
    evidence = yaml.safe_load(header.split("```yaml\n", 1)[1])

    assert trajectory == before
    assert observed == body
    assert content.startswith("# Trajectory Record\n\n```yaml\n")
    assert evidence == {
        "case_name": "cancel",
        "outcome": outcome,
        "evaluation_criteria": [
            metadata["evaluation_criteria"][0],
            metadata["evaluation_criteria"][-1],
        ],
        "experience_execution": metadata["experience_execution"],
    }
    assert "trajectory_uri" not in evidence
    assert "source_session_uri" not in evidence
    assert trajectory.uri not in content
    assert metadata["source_session_uri"] not in content
    assert "experience_effects" not in evidence
    assert "experience_effect_sources" not in evidence
    assert "experience_update" not in content
    assert "gate_evidence" not in content
    assert "created_at" not in content
    assert "unexpected_field" not in content
    assert "task_spec" not in evidence
    assert "evaluation" not in evidence
    assert metadata["task_spec"]["input"] == {"booking_id": "ABC123"}
    for marker in [
        "ABC123",
        "PRIVATE_TASK_NAME",
        "private:task:signature",
        "PRIVATE_RUBRIC_NAME",
        "PRIVATE_TASK_METADATA",
    ]:
        assert marker not in content


def test_content_excludes_raw_evaluator_payload_and_keeps_empty_execution_explicit():
    trajectory = Trajectory(
        name="run1",
        uri="trajectory:run1",
        outcome="unknown",
        content="Unstructured observation.",
        metadata={
            "case_name": "case",
            "source_session_uri": "viking://session/s/archives/1",
            "evaluation": {"passed": False, "score": 0, "raw": "private evaluator payload"},
        },
    )
    content = render_trajectory_template(trajectory)
    evidence = yaml.safe_load(content.split("```yaml\n", 1)[1].split("\n```\n\n", 1)[0])
    assert evidence["experience_execution"] == {}
    assert "task_spec" not in evidence
    assert "experience_effects" not in evidence
    assert "experience_effect_sources" not in evidence
    assert "evaluation" not in evidence
    assert trajectory.metadata["evaluation"] == {
        "passed": False,
        "score": 0,
        "raw": "private evaluator payload",
    }
    assert "private evaluator payload" not in content
    assert content.endswith(trajectory.content)


@pytest.mark.parametrize("reason", [None, "max_steps", "user_stop"])
def test_evaluation_diagnostics_preserve_termination_without_copying_full_reward(reason):
    from openviking.session.train.components.trajectory_observation import (
        trajectory_evaluation_summary,
    )
    from openviking.session.train.domain import RubricEvaluation

    note = "Simulation terminated prematurely. Termination reason: max_steps"
    result = {
        "info": {"note": note, "action": None},
        "action_checks": [{"huge": "action payload stays in evaluation criteria"}],
        "reward": 0.0,
    }
    metadata = {"evaluation_result": result}
    if reason is not None:
        metadata["termination_reason"] = reason
    evaluation = RubricEvaluation(False, 0.0, [], [], metadata=metadata)
    before = deepcopy(evaluation)
    summary = trajectory_evaluation_summary(evaluation)
    expected = {"info": {"note": note}}
    if reason is not None:
        expected["termination_reason"] = reason
    assert summary == {"passed": False, "score": 0.0, "details": expected}
    assert evaluation == before
    trajectory = Trajectory(
        name="run",
        uri="trajectory:run",
        outcome="failure",
        content="1. Attempted update.",
        metadata={
            "case_name": "case",
            "source_session_uri": "viking://session/s/archives/1",
            "evaluation": summary,
        },
    )
    rendered = render_trajectory_template(trajectory)
    evidence = yaml.safe_load(rendered.split("```yaml\n", 1)[1].split("\n```\n\n", 1)[0])
    assert evidence["evaluation"] == {"details": expected}
    assert trajectory.metadata["evaluation"] == summary
    assert note in rendered
    assert "action payload" not in rendered


def test_evaluation_summary_handles_unknown_empty_info_and_error_without_changing_score():
    from openviking.session.train.components.trajectory_observation import (
        trajectory_evaluation_summary,
    )
    from openviking.session.train.domain import RubricEvaluation

    assert trajectory_evaluation_summary(None) is None
    evaluation = RubricEvaluation(
        True,
        0.75,
        [],
        [],
        metadata={
            "evaluation_result": {"info": {"action": None, "communicate": None}},
        },
    )
    assert trajectory_evaluation_summary(evaluation) == {"passed": True, "score": 0.75}
    evaluation.metadata["evaluation_result"] = {
        "type": "EvaluatorError",
        "error": "Could not inspect final state",
        "unrelated": "skip",
    }
    assert trajectory_evaluation_summary(evaluation) == {
        "passed": True,
        "score": 0.75,
        "details": {"type": "EvaluatorError", "error": "Could not inspect final state"},
    }


@pytest.mark.parametrize(
    "criteria",
    [None, [], [{"passed": True}], [{"passed": None}], [{"passed": 0}], [{"id": "missing"}]],
)
def test_body_without_explicitly_failed_rubrics_keeps_an_empty_list(criteria):
    trajectory = Trajectory(
        name="run",
        uri="trajectory:run",
        outcome="unknown",
        content="Observed dialogue.",
        metadata={
            "case_name": "case",
            "source_session_uri": "viking://session/s/archives/1",
            "evaluation_criteria": criteria,
        },
    )
    before = deepcopy(trajectory)
    content = render_trajectory_template(trajectory)
    fields = yaml.safe_load(content.split("```yaml\n", 1)[1].split("\n```\n\n", 1)[0])
    assert fields["evaluation_criteria"] == []
    assert trajectory == before
    assert content.endswith(trajectory.content)


@pytest.mark.parametrize("execution", [None, {}, {"rule": {"executed_nodes": ["reminder"]}}])
def test_template_preserves_explicit_execution_value(execution):
    trajectory = Trajectory(
        name="run",
        uri="viking://user/u/memories/trajectories/case/run.md",
        outcome="failure",
        content="observed steps\n",
        metadata={"case_name": "case", "experience_execution": execution},
    )
    rendered = render_trajectory_template(trajectory)
    header = yaml.safe_load(rendered.split("```yaml\n", 1)[1].split("\n```", 1)[0])
    assert header["experience_execution"] == execution
    assert header["evaluation_criteria"] == []
    assert rendered.endswith("observed steps\n")
