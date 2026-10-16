from __future__ import annotations

from copy import deepcopy

import pytest
import yaml

from openviking.session.train.components.trajectory_observation import render_trajectory_content
from openviking.session.train.domain import Trajectory


@pytest.mark.parametrize(
    ("outcome", "evaluation"),
    [
        ("success", {"passed": True, "score": 1.0}),
        ("failure", {"passed": False, "score": 0.0}),
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
        "case_name": "cancel",
        "source_session_uri": "viking://session/s/archives/1",
        "task_spec": {"name": "任务一", "input": {"booking_id": "ABC123"}},
        "evaluation": evaluation,
        "evaluation_criteria": [{"id": "check:0", "passed": False, "summary": "缺少确认"}],
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

    content = render_trajectory_content(trajectory)
    header, observed = content.split("\n```\n\n", 1)
    evidence = yaml.safe_load(header.split("```yaml\n", 1)[1])

    assert trajectory == before
    assert observed == body
    assert content.startswith("# Trajectory Record\n\n```yaml\n")
    assert evidence == {
        "trajectory_uri": trajectory.uri,
        "source_session_uri": metadata["source_session_uri"],
        "case_name": "cancel",
        "task_spec": metadata["task_spec"],
        "outcome": outcome,
        "evaluation": evaluation,
        "evaluation_criteria": metadata["evaluation_criteria"],
        "experience_execution": metadata["experience_execution"],
        "experience_effects": metadata["experience_effects"],
        "experience_effect_sources": metadata["experience_effect_sources"],
    }
    assert "experience_update" not in content
    assert "gate_evidence" not in content
    assert "created_at" not in content
    assert "unexpected_field" not in content


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
    content = render_trajectory_content(trajectory)
    evidence = yaml.safe_load(content.split("```yaml\n", 1)[1].split("\n```\n\n", 1)[0])
    assert evidence["experience_execution"] == {}
    assert evidence["task_spec"] is None
    assert evidence["experience_effect_sources"] == {}
    assert evidence["evaluation"] == {"passed": False, "score": 0}
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
    rendered = render_trajectory_content(trajectory)
    evidence = yaml.safe_load(rendered.split("```yaml\n", 1)[1].split("\n```\n\n", 1)[0])
    assert evidence["evaluation"] == summary
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
