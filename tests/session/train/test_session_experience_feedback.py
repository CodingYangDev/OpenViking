# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
from __future__ import annotations

import json

import pytest
import yaml
from test_case_experience_pipeline import observe, trajectory_program
from test_case_experience_pipeline import setup_observation as setup_observation
from test_gradient_estimator_component import ROOT, analysis

from openviking.session.memory.dataclass import MemoryFile
from openviking.session.memory.utils.memory_file_utils import MemoryFileUtils

URI = f"{ROOT}/payment_review.md"
SOURCE = 'dag = workflow("review")\nready = check("Payment needed")\nhint = tell("Review payment scope")\nready.then(hint)'


def prepare(fs):
    fs.files[URI] = MemoryFileUtils.write(
        MemoryFile(
            uri=URI,
            memory_type="experiences",
            content=SOURCE,
            extra_fields={"experience_name": "payment_review", "version": 1},
        )
    )
    run = analysis(passed=False)
    run.metadata["experience_execution"] = {
        URI: {
            "experience_name": "payment_review",
            "delivered_advice": [
                {
                    "slot_name": "hint",
                    "instruction": "Review payment scope",
                    "provider_type": "TellAgent",
                }
            ],
        },
        f"{ROOT}/not_delivered.md": {
            "actions": [{"slot_name": "planned", "description": "Not delivered"}],
            "executed_nodes": ["planned"],
            "slot_values": {"planned": True},
        },
    }
    return run


def program(alias="E1"):
    labels = json.dumps({"positive_ids": [alias], "negative_ids": [], "weak_ids": []})
    return trajectory_program().replace("content=", f"experience_effects={labels!r}, content=", 1)


@pytest.mark.asyncio
async def test_effects_are_coextracted_persisted_and_reused_without_another_model_call(
    setup_observation,
):
    fs, ctx, model, analyzer, context, policies = setup_observation
    run = prepare(fs)
    model.get_completion_async.return_value = program()
    await observe(analyzer, run, policies, context)
    trajectory = run.trajectories[0]
    assert trajectory.metadata["experience_effects"] == {
        "positive_ids": ["E1"],
        "negative_ids": [],
        "weak_ids": [],
    }
    assert trajectory.metadata["experience_effect_sources"] == {"E1": URI}
    rendered = yaml.safe_load(trajectory.content.split("```yaml\n", 1)[1].split("\n```", 1)[0])
    assert rendered["experience_effect_sources"] == {"E1": URI}
    assert rendered["experience_effects"] == trajectory.metadata["experience_effects"]
    assert model.get_completion_async.await_count == 1
    payloads = [
        json.loads(m["content"])
        for m in model.get_completion_async.call_args.kwargs["messages"]
        if isinstance(m.get("content"), str) and m["content"].startswith('{"case":')
    ]
    assert len(payloads) == 1
    assert payloads[0]["injected_experiences"] == [
        {
            "id": "E1",
            "experience_uri": URI,
            "instructions": ["Review payment scope"],
        }
    ]
    first = fs.files[URI]
    await observe(analyzer, run, policies, context)
    assert model.get_completion_async.await_count == 1
    assert fs.files[URI] == first
    stats = MemoryFileUtils.read(first, uri=URI).extra_fields["feedback_stats"]
    assert stats["positive_count"] == stats["injected_count"] == 1
    # A helpful reminder can be observed in a rollout whose overall outcome still failed.
    assert trajectory.outcome == "failure"


@pytest.mark.asyncio
async def test_invalid_alias_uses_existing_provider_repair_loop(setup_observation):
    fs, ctx, model, analyzer, context, policies = setup_observation
    run = prepare(fs)
    model.get_completion_async.side_effect = [program("E999"), program()]
    await observe(analyzer, run, policies, context)
    assert model.get_completion_async.await_count == 2
    assert run.trajectories[0].metadata["experience_effects"]["positive_ids"] == ["E1"]
    assert (
        MemoryFileUtils.read(fs.files[URI], uri=URI).extra_fields["feedback_stats"][
            "injected_count"
        ]
        == 1
    )


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
    assert model.get_completion_async.await_count == 1
    assert (
        MemoryFileUtils.read(fs.files[URI], uri=URI).extra_fields["feedback_stats"][
            "injected_count"
        ]
        == 1
    )
