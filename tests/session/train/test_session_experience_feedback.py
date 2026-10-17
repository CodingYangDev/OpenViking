# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml
from test_case_experience_pipeline import observe, trajectory_program
from test_case_experience_pipeline import setup_observation as setup_observation
from test_gradient_estimator_component import ROOT, analysis

from openviking.session.memory.dataclass import MemoryFile
from openviking.session.memory.experience.experience_context import AgentExperienceContextProvider
from openviking.session.memory.utils.memory_file_utils import MemoryFileUtils
from openviking.session.train import ExperienceGradientContext, ExperienceGradientEstimator
from openviking_cli.utils.config import get_openviking_config
from openviking_cli.utils.config.memory_config import ActionFeedbackConfig

URI = f"{ROOT}/payment_review.md"
SOURCE = 'dag = workflow("review")\nready = check("Payment needed")\nhint = tell("Review payment scope")\nready.then(hint)'


def prepare(fs, *, second=False):
    content = SOURCE + ('\nother = tell("Repeat an already completed check")' if second else "")
    fs.files[URI] = MemoryFileUtils.write(
        MemoryFile(
            uri=URI,
            memory_type="experiences",
            content=content,
            extra_fields={"experience_name": "payment_review", "version": 1},
        )
    )
    run = analysis(name="payment_review", passed=False)
    actions = [
        {"slot_name": "hint", "instruction": "Review payment scope", "provider_type": "TellAgent"}
    ]
    if second:
        actions.append(
            {
                "slot_name": "other",
                "instruction": "Repeat an already completed check",
                "provider_type": "TellAgent",
            }
        )
    run.metadata["experience_execution"] = {
        URI: {"experience_name": "payment_review", "delivered_advice": actions},
        f"{ROOT}/not_delivered.md": {
            "actions": [{"slot_name": "planned", "description": "Not delivered"}],
            "executed_nodes": ["planned"],
            "slot_values": {"planned": True},
        },
    }
    return run


def program(alias="A1", *, second=False):
    scores = [
        {
            "id": alias,
            "score": 1,
            "evidence": "The agent corrected the payment scope after this reminder.",
        }
    ]
    if second:
        scores.append(
            {
                "id": "A2",
                "score": -1,
                "evidence": "The proposed check was already completed before injection.",
            }
        )
    return trajectory_program().replace(
        "steps=", f"action_scores={yaml.safe_dump(scores)!r}, steps=", 1
    )


def feedback(fs):
    return MemoryFileUtils.read(fs.files[URI], uri=URI).extra_fields["action_feedback"]


@pytest.mark.asyncio
@pytest.mark.parametrize("updated_instruction", [False, True])
@pytest.mark.parametrize("min_samples", [1, 10])
async def test_actions_are_scored_in_one_extraction_and_exposed_with_existing_experience(
    setup_observation,
    updated_instruction,
    min_samples,
    monkeypatch,
):
    fs, ctx, model, analyzer, context, policies = setup_observation
    run = prepare(fs, second=True)
    model.get_completion_async.return_value = program(second=True)
    await observe(analyzer, run, policies, context)
    trajectory = run.trajectories[0]
    assert [x["score"] for x in trajectory.metadata["action_scores"]] == [1, -1]
    assert trajectory.metadata["action_score_sources"] == {
        "A1": {"experience_uri": URI, "slot_name": "hint", "instruction": "Review payment scope"},
        "A2": {
            "experience_uri": URI,
            "slot_name": "other",
            "instruction": "Repeat an already completed check",
        },
    }
    header = yaml.safe_load(trajectory.content.split("```yaml\n", 1)[1].split("\n```", 1)[0])
    assert "action_scores" not in header and "action_score_sources" not in header
    payloads = [
        json.loads(m["content"])
        for m in model.get_completion_async.call_args.kwargs["messages"]
        if isinstance(m.get("content"), str) and m["content"].startswith('{"case":')
    ]
    assert [x["id"] for x in payloads[0]["injected_actions"]] == ["A1", "A2"]
    extraction_prompt = " ".join(
        m["content"]
        for m in model.get_completion_async.call_args.kwargs["messages"]
        if isinstance(m.get("content"), str)
    )
    for rule in (
        "user's current valid goal, not the action's own demands",
        "Following the reminder alone is not evidence of help",
        "Use -1 only for evidenced side effects",
        "Non-adoption or inapplicability alone does not justify a score",
        "If both benefit and side effects are supported, use -1 and describe both",
    ):
        assert rule in extraction_prompt
    persisted = fs.files[URI]
    await observe(analyzer, run, policies, context)
    model.get_completion_async.assert_awaited_once()
    assert fs.files[URI] == persisted
    state = feedback(fs)
    assert state["actions"]["hint"]["useful_count"] == 1
    assert state["actions"]["other"]["not_useful_count"] == 1
    assert trajectory.outcome == "failure"  # Whole-task reward does not label individual actions.

    expected_source = SOURCE
    if updated_instruction:
        experience = MemoryFileUtils.read(fs.files[URI], uri=URI)
        experience.content = experience.content.replace(
            "Review payment scope", "Confirm payment scope"
        )
        fs.files[URI] = MemoryFileUtils.write(experience)
        expected_source = SOURCE.replace("Review payment scope", "Confirm payment scope")

    # Exercise the actual exp request, including configured feedback thresholds.
    monkeypatch.setattr(
        get_openviking_config().memory,
        "action_feedback",
        ActionFeedbackConfig(min_samples=min_samples),
    )
    provider = AgentExperienceContextProvider(
        messages=[],
        case=run.rollout.case,
        target_uri=URI,
        source_session_uri=run.metadata["source_session_uri"],
        trajectories=run.trajectories,
    )
    provider._ctx = ctx
    exp_model = SimpleNamespace(
        model="test", get_completion_async=AsyncMock(return_value="sdk.commit()")
    )
    estimator = ExperienceGradientEstimator(viking_fs=fs, vlm=exp_model)
    await estimator._run_extract_loop(provider, ExperienceGradientContext(ctx, []))
    exp_model.get_completion_async.assert_awaited_once()
    messages = exp_model.get_completion_async.call_args.kwargs["messages"]
    existing = next(
        m["content"] for m in messages if m.get("content", "").startswith("# Existing Experience")
    )
    if min_samples == 1:
        assert "## action_feedback (system-owned, read-only)" in existing
        assert "### unchanged\n\n- hint: positive 100.0% (1/1)" in existing
        assert "### revise\n\n- other: negative 100.0% (1/1)" in existing
        assert "The agent corrected the payment scope" in existing
        assert (
            "proposed check was already completed" in existing
        )  # Historical negatives are reused.
    else:
        assert "action_feedback" not in existing  # Insufficient scored samples.
    for hidden in (
        "useful_count",
        "not_useful_count",
        "observed_trajectories",
        "action_score_sources",
        "instruction:",
    ):
        assert hidden not in existing
    assert expected_source in existing
    assert (
        existing.count("Confirm payment scope" if updated_instruction else "Review payment scope")
        == 1
    )
    assert feedback(fs) == state  # Rendering and action text edits never reset counters.
    update_prompt = " ".join(" ".join(m.get("content", "") for m in messages).split())
    for rule in (
        "accumulates model assessments by slot_name across instruction changes and scoring",
        "unchanged: do not delete, rename or rewrite these actions, or change when they trigger",
        "revise: check negative evidence against user intent and source steps",
        "while preserving supported benefits",
        "Non-adoption or inapplicability alone does not justify deletion",
        "Unlisted actions are not necessarily useless",
        "Ratios exclude unscored observations",
        "Historical counts are not side-effect rates or proof that revised instructions work",
    ):
        assert rule in update_prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["unknown_alias", "malformed", "zero", "null"])
async def test_unusable_scores_do_not_retry_or_block_trajectory(setup_observation, bad):
    fs, ctx, model, analyzer, context, policies = setup_observation
    run = prepare(fs)
    text = program("A999")
    if bad != "unknown_alias":
        value = (
            "bad yaml: ["
            if bad == "malformed"
            else [{"id": "A1", "score": 0 if bad == "zero" else None, "evidence": "Uncertain."}]
        )
        text = trajectory_program().replace(
            "steps=", f"action_scores={yaml.safe_dump(value)!r}, steps=", 1
        )
    model.get_completion_async.return_value = text
    await observe(analyzer, run, policies, context)
    model.get_completion_async.assert_awaited_once()
    assert "score" not in run.trajectories[0].metadata["action_scores"][0]
    item = feedback(fs)["actions"]["hint"]
    assert item["unscored_count"] == 1 and item["not_useful_count"] == item["useful_count"] == 0


@pytest.mark.asyncio
async def test_feedback_retry_reuses_persisted_trajectory_after_failed_write(
    setup_observation, monkeypatch
):
    fs, ctx, model, analyzer, context, policies = setup_observation
    run = prepare(fs)
    model.get_completion_async.return_value = program()
    original = fs.write_file
    failed = False

    async def fail_once(uri, *args, **kwargs):
        nonlocal failed
        if uri == URI and not failed:
            failed = True
            raise OSError("temporary write failure")
        return await original(uri, *args, **kwargs)

    monkeypatch.setattr(fs, "write_file", fail_once)
    with pytest.raises(RuntimeError, match="feedback update failed"):
        await observe(analyzer, run, policies, context)
    assert any("/trajectories/" in uri for uri in fs.files)
    await observe(analyzer, run, policies, context)
    model.get_completion_async.assert_awaited_once()
    assert feedback(fs)["actions"]["hint"]["useful_count"] == 1


def test_action_feedback_examples_cover_benefit_harm_and_no_effect():
    """Validate the prompt's examples, not a live model's classification accuracy."""
    from openviking.session.memory.memory_type_registry import get_default_registry

    schema = get_default_registry().get("trajectories")
    description = next(f.description for f in schema.fields if f.name == "action_scores")
    marker = "Examples (use supplied IDs and actual evidence):"
    assert marker in description
    examples = yaml.safe_load(description.split(marker, 1)[1])
    assert [x.get("score") for x in examples] == [1, -1, None]
    assert "corrected" in examples[0]["evidence"]
    assert "unwanted paid step" in examples[1]["evidence"]
    assert "confirmed free operation" in examples[1]["evidence"]
    assert "no effect" in examples[2]["evidence"]
    assert "score" not in examples[2]  # No zero or null for neutral/uncertain effects.
    assert "Use -1 for evidenced redundancy" not in description
