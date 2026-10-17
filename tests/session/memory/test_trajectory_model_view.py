from __future__ import annotations

import copy
from dataclasses import asdict
from unittest.mock import AsyncMock

import pytest
import yaml

from openviking.session.memory.experience.experience_context import (
    AgentExperienceContextProvider,
    _pack_trajectory_comparison,
    _trajectory_comparison_messages,
    _trajectory_model_content,
)
from openviking.session.train import Case, Rubric, Trajectory
from openviking.session.train.components.trajectory_observation import (
    TRAJECTORY_FORMAT,
)
from tests.session.train.test_fakes import render_trajectory_template

BODY = (
    '# Session Context\n\nKeep "quoted rules" and exact spacing.\n\n'
    "# Trajectory\n\n1. `user` requested help.\n"
    '2. `tool` returned {"task_spec": "this is dialogue, not header metadata"}.\n\n'
)


def make_trajectory(*, outcome="failure", evaluation=None, criteria=None, task_text="TASK_ONLY"):
    trajectory = Trajectory(
        name="run",
        uri="viking://user/u/memories/trajectories/case_a/run.md",
        outcome=outcome,
        content=BODY,
        metadata={
            "trajectory_format": TRAJECTORY_FORMAT,
            "case_name": "case_a",
            "source_session_uri": "viking://session/s/archives/1",
            "task_spec": {"name": "task_a", "input": {"user_query": task_text}},
            "evaluation": evaluation,
            "evaluation_criteria": criteria or [],
            "experience_execution": {"case_a": {"slot_values": {"ready": True}}},
            "action_scores": [{"id": "A1", "score": -1, "evidence": "Already completed."}],
            "action_score_sources": {"A1": {"slot_name": "hint", "instruction": "Check"}},
            "experience_effects": {"positive_ids": [], "negative_ids": ["E1"], "weak_ids": []},
            "experience_effect_sources": {"E1": "viking://user/u/memories/experiences/case_a.md"},
        },
    )
    trajectory.content = render_trajectory_template(trajectory)
    return trajectory


def header(content):
    return yaml.safe_load(content.split("```yaml\n", 1)[1].split("\n```\n", 1)[0])


def test_model_view_removes_identity_and_task_spec_and_keeps_failed_criteria_only():
    failed = {"id": "failed", "kind": "criterion", "passed": False, "summary": '{"met":false}'}
    trajectory = make_trajectory(
        evaluation={"passed": False, "score": 0.0, "details": {"termination_reason": "user_stop"}},
        criteria=[
            {"id": "passed", "passed": True, "summary": "success-only evidence"},
            failed,
            {"id": "unknown", "passed": None},
            {"id": "missing"},
            {"id": "numeric_zero", "passed": 0},
        ],
    )
    original = copy.deepcopy(trajectory)

    content = _trajectory_model_content(trajectory.content)
    fields = header(content)

    assert set(fields) == {
        "outcome",
        "evaluation",
        "evaluation_criteria",
        "experience_execution",
    }
    assert fields["outcome"] == "failure"
    assert fields["evaluation"] == {"details": trajectory.metadata["evaluation"]["details"]}
    assert fields["evaluation_criteria"] == [failed]
    assert fields["experience_execution"] == trajectory.metadata["experience_execution"]
    for name in [
        "experience_effects",
        "experience_effect_sources",
        "action_scores",
        "action_score_sources",
    ]:
        assert name not in fields
        assert name not in header(trajectory.content)
        assert trajectory.metadata[name] == original.metadata[name]
    assert content.endswith(BODY)
    assert "TASK_ONLY" not in content and "success-only evidence" not in content
    assert trajectory == original
    assert "task_spec" not in header(trajectory.content)
    assert trajectory.metadata["task_spec"]["input"] == {"user_query": "TASK_ONLY"}
    assert _trajectory_model_content(content) == content


def test_model_view_omits_internal_annotations_from_previously_rendered_headers():
    trajectory = make_trajectory()
    fields = header(trajectory.content)
    fields["trajectory_uri"] = trajectory.uri
    fields["source_session_uri"] = trajectory.metadata["source_session_uri"]
    fields["experience_effects"] = trajectory.metadata["experience_effects"]
    fields["experience_effect_sources"] = trajectory.metadata["experience_effect_sources"]
    body = BODY + "The user mentioned experience_effects and experience_effect_sources.\n"
    persisted = (
        "# Trajectory Record\n\n```yaml\n"
        + yaml.safe_dump(fields, allow_unicode=True, sort_keys=False).rstrip()
        + "\n```\n\n"
        + body
    )

    compact = _trajectory_model_content(persisted)

    assert "trajectory_uri" not in header(compact)
    assert "source_session_uri" not in header(compact)
    assert "experience_effects" not in header(compact)
    assert "experience_effect_sources" not in header(compact)
    assert header(compact)["experience_execution"] == fields["experience_execution"]
    assert compact.endswith(body)
    assert header(persisted)["experience_effects"] == trajectory.metadata["experience_effects"]
    assert _trajectory_model_content(compact) == compact


def test_success_retains_failed_diagnostic_without_changing_overall_result():
    failed = {"id": "non_scoring_action", "passed": False, "summary": '{"action_match":false}'}
    trajectory = make_trajectory(
        outcome="success", evaluation={"passed": True, "score": 1.0}, criteria=[failed]
    )
    fields = header(_trajectory_model_content(trajectory.content))
    assert fields["outcome"] == "success" and "evaluation" not in fields
    assert trajectory.metadata["evaluation"] == {"passed": True, "score": 1.0}
    assert fields["evaluation_criteria"] == [failed]


@pytest.mark.parametrize(
    "outcome,evaluation", [("success", {"passed": True, "score": 1.0}), ("unknown", None)]
)
def test_no_failed_criteria_does_not_invent_failures(outcome, evaluation):
    trajectory = make_trajectory(
        outcome=outcome, evaluation=evaluation, criteria=[{"passed": True}]
    )
    fields = header(_trajectory_model_content(trajectory.content))
    assert fields["outcome"] == outcome and "evaluation" not in fields
    assert trajectory.metadata["evaluation"] == evaluation
    assert fields["evaluation_criteria"] == []


@pytest.mark.parametrize(
    "content",
    [
        BODY,
        "Unstructured historical evidence",
        "# Trajectory Record\n\n```yaml\nnot a complete header",
    ],
)
def test_text_without_a_complete_generated_header_is_unchanged(content):
    assert _trajectory_model_content(content) == content


@pytest.mark.asyncio
async def test_prefetch_budgets_the_compact_view_but_keeps_full_internal_trajectory():
    trajectory = make_trajectory(
        task_text="INTERNAL_TASK_DETAILS " * 2000,
        criteria=[{"id": "passed", "passed": True, "evidence": "SUCCESS_ONLY_DETAILS " * 2000}],
    )
    case = Case("case_a", "service domain", {}, Rubric("r", "", []))
    target = "viking://user/u/memories/experiences/case_a.md"
    compact = _trajectory_model_content(trajectory.content)
    payload = _pack_trajectory_comparison(
        case=asdict(case),
        target_uri=target,
        records=[
            {"trajectory_uri": trajectory.uri, "outcome": trajectory.outcome, "content": compact}
        ],
        current_trajectory_uris={trajectory.uri},
        char_limit=100_000,
    )
    budget = sum(len(message["content"]) for message in _trajectory_comparison_messages(payload))
    assert len(trajectory.content) > len(compact)
    assert "INTERNAL_TASK_DETAILS" not in trajectory.content
    assert (
        trajectory.metadata["task_spec"]["input"]["user_query"] == "INTERNAL_TASK_DETAILS " * 2000
    )
    assert "SUCCESS_ONLY_DETAILS" not in trajectory.content
    assert "SUCCESS_ONLY_DETAILS" not in compact
    original = copy.deepcopy(trajectory)
    provider = AgentExperienceContextProvider(
        messages=[],
        case=case,
        target_uri=target,
        source_session_uri=trajectory.metadata["source_session_uri"],
        trajectories=[trajectory],
        current_trajectory_uris={trajectory.uri},
        comparison_char_limit=budget,
    )
    provider._eval_memory = AsyncMock(return_value={"experience": {"error": "File not found"}})

    messages = provider.format_prefetch_messages(await provider.prefetch())

    records = [m for m in messages if m["content"].startswith("# Trajectory Record\n")]
    assert records == [{"role": "user", "content": compact}]
    assert trajectory == original
    assert provider.current_trajectory_uris == {trajectory.uri}
    provider._eval_memory.assert_awaited_once_with(f"experience = sdk.read(uri={target!r})")
