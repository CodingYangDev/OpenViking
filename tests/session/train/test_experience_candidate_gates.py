from __future__ import annotations

import json
from dataclasses import fields
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_case_experience_pipeline import setup_observation as setup_observation
from test_gradient_estimator_component import ROOT, analysis, operations

from openviking.session.memory.dataclass import MemoryFile
from openviking.session.memory.utils.memory_file_utils import MemoryFileUtils
from openviking.session.train import ExperienceGradientEstimator, MemoryFilePolicyUpdater
from openviking.session.train.components.policy_optimizer import case_experience_plan_items
from openviking.session.train.domain import PolicyUpdatePlan
from openviking.session.train.gates.experience import ExperienceImprovementGate
from openviking.session.train.gates.identifiers import ExperienceIdentifierGate
from openviking_cli.utils.config.agent_evolution_config import DagDeciderConfig

TARGET = ROOT + "/case_a.md"
BAD = 'dag = workflow("review")\nr = tell("Cancel XEHM4B")'
GOOD = 'dag = workflow("review")\nr = tell("Verify the reservation selected by this user")'


def create(source):
    return f"sdk.create_experiences(experience_name='case_a', content={source!r})\nsdk.commit()"


def harness(setup, monkeypatch, replies, *, gates=(), old=None):
    fs, ctx, model, _analyzer, context, policies = setup
    if old is not None:
        fs.files[TARGET] = MemoryFileUtils.write(
            MemoryFile(
                uri=TARGET,
                memory_type="experiences",
                content=old,
                extra_fields={"experience_name": "case_a", "case_name": "case_a", "version": 3},
            )
        )
    replies = iter(replies)

    async def complete(**kwargs):
        assert not fs.policy_lock.locked(), "model calls must not hold the write lock"
        return next(replies)

    model.get_completion_async = AsyncMock(side_effect=complete)
    config = SimpleNamespace(
        memory=SimpleNamespace(link_enabled=False, extraction_output_format="python")
    )
    monkeypatch.setattr(
        "openviking.session.memory.extract_loop.get_openviking_config", lambda: config
    )
    item = analysis()
    item.rollout.case.input = {"query": "Cancel XEHM4B"}
    estimator = ExperienceGradientEstimator(viking_fs=fs, vlm=model, gates=list(gates))
    return estimator, item, context, policies, model, fs


@pytest.mark.asyncio
@pytest.mark.parametrize("repaired", [True, False])
async def test_identifier_gate_uses_real_extract_loop_bounded_repair(
    setup_observation, monkeypatch, repaired
):
    estimator, item, context, policies, model, fs = harness(
        setup_observation, monkeypatch, [create(BAD), create(GOOD if repaired else BAD)]
    )
    if repaired:
        (proposal,) = await estimator.estimate(item, policies, context)
        assert proposal.after_file.content == GOOD
        assert proposal.metadata["experience_proposal_gate"]["gates"] == [
            "ExperienceIdentifierGate"
        ]
        assert (
            proposal.metadata["proposal_source_sessions"][0]["source_session_uri"]
            == item.metadata["source_session_uri"]
        )
        assert proposal.links[0].to_uri == item.trajectories[0].uri
        plan = PolicyUpdatePlan(
            items=case_experience_plan_items(operations(content=GOOD), [proposal], policies)
        )
        async with policies.lock() as lease:
            applied = await MemoryFilePolicyUpdater(viking_fs=fs).apply(
                plan, policies, context.request_context, transaction_handle=lease
            )
        assert not applied.errors
        assert MemoryFileUtils.read(fs.files[TARGET], uri=TARGET).content == GOOD
    else:
        with pytest.raises(ValueError, match="Provider operation validation failed after repair"):
            await estimator.estimate(item, policies, context)
        assert TARGET not in fs.files
    assert model.get_completion_async.await_count == 2
    messages = model.get_completion_async.await_args_list[1].kwargs["messages"]
    assert {"role": "assistant", "content": create(BAD)} in messages
    feedback = next(m["content"] for m in messages if "Errors: [{" in m["content"])
    assert "ExperienceIdentifierGate" in feedback and "XEHM4B" in feedback
    assert "experience_synthesis_noop" not in item.metadata


@pytest.mark.asyncio
async def test_gate_checks_whole_materialized_patch_not_only_edit(setup_observation, monkeypatch):
    old = BAD + '\nother = tell("old reminder")'
    bad = 'experience.content.edit(search="old reminder", replace="new reminder")\nsdk.commit()'
    good = (
        'experience.content.edit(search="Cancel XEHM4B", replace="Verify the selected reservation")\n'
        'experience.content.edit(search="old reminder", replace="new reminder")\nsdk.commit()'
    )
    estimator, item, context, policies, model, fs = harness(
        setup_observation, monkeypatch, [bad, good], old=old
    )
    (proposal,) = await estimator.estimate(item, policies, context)
    assert "XEHM4B" not in proposal.after_file.content
    assert "new reminder" in proposal.after_file.content
    assert proposal.before_file.content == old and proposal.base_version == 3
    assert MemoryFileUtils.read(fs.files[TARGET], uri=TARGET).content == old
    assert model.get_completion_async.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ["sdk.commit()", create(GOOD)])
async def test_explicit_noop_and_generalized_candidate_need_no_repair(
    setup_observation, monkeypatch, reply
):
    estimator, item, context, policies, model, _fs = harness(
        setup_observation, monkeypatch, [reply]
    )
    proposals = await estimator.estimate(item, policies, context)
    assert bool(proposals) is (reply != "sdk.commit()")
    assert model.get_completion_async.await_count == 1


@pytest.mark.asyncio
async def test_identifier_gate_switch_is_a_class_variable(setup_observation, monkeypatch):
    assert ExperienceIdentifierGate.ENABLED is True
    assert "ENABLED" not in {f.name for f in fields(ExperienceIdentifierGate)}
    monkeypatch.setattr(ExperienceIdentifierGate, "ENABLED", False)
    estimator, item, context, policies, model, _fs = harness(
        setup_observation, monkeypatch, [create(BAD)]
    )
    (proposal,) = await estimator.estimate(item, policies, context)
    assert proposal.after_file.content == BAD
    assert model.get_completion_async.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("repairable", [True, False])
async def test_quality_gate_uses_same_repair_path_not_a_post_filter(
    setup_observation, monkeypatch, repairable
):
    monkeypatch.setattr(ExperienceImprovementGate, "ENABLED", True)
    validation_count = 0

    async def validate(self, plan, *args):
        nonlocal validation_count
        validation_count += 1
        assert not setup_observation[0].policy_lock.locked()
        if validation_count == 1:
            return PolicyUpdatePlan(
                items=[],
                metadata={
                    "experience_improvement_gate": {
                        "passed": False,
                        "candidates": [
                            {
                                "passed": False,
                                "repairable": repairable,
                                "reason": "unjustified restriction"
                                if repairable
                                else "Jev connection failed",
                            }
                        ],
                    }
                },
            )
        return PolicyUpdatePlan(
            items=plan.items, metadata={"experience_improvement_gate": {"passed": True}}
        )

    monkeypatch.setattr(ExperienceImprovementGate, "_validate", validate)
    gate = ExperienceImprovementGate(config=DagDeciderConfig(), jev=object(), dag_decider=object())
    estimator, item, context, policies, model, _fs = harness(
        setup_observation, monkeypatch, [create(GOOD), create(GOOD)], gates=[gate]
    )
    if repairable:
        (proposal,) = await estimator.estimate(item, policies, context)
        assert proposal.metadata["experience_proposal_gate"]["gates"] == [
            "ExperienceIdentifierGate",
            "ExperienceImprovementGate",
        ]
        assert validation_count == model.get_completion_async.await_count == 2
        assert "unjustified restriction" in json.dumps(
            model.get_completion_async.await_args.kwargs["messages"]
        )
    else:
        with pytest.raises(RuntimeError, match="Jev connection failed"):
            await estimator.estimate(item, policies, context)
        assert validation_count == model.get_completion_async.await_count == 1


@pytest.mark.asyncio
async def test_invalid_dag_is_not_silently_discarded_after_repair(setup_observation, monkeypatch):
    invalid = 'dag = workflow("review")\nr = tell("verify")\nmissing.then(r)'
    estimator, item, context, policies, model, fs = harness(
        setup_observation, monkeypatch, [create(invalid), create(invalid)]
    )
    with pytest.raises(ValueError, match="Provider operation validation failed after repair"):
        await estimator.estimate(item, policies, context)
    assert model.get_completion_async.await_count == 2
    assert TARGET not in fs.files
