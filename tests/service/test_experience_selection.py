# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from test_experience_runtime import EXPERIENCE, TELL_PROGRAM
from test_experience_runtime import env as env
from test_jev_dag_decider import _instance

from openviking.models.jev import JevClient
from openviking.service.experience_dag_decider import (
    ExperienceDagDecider,
    LlmDagDecisionEvaluator,
)
from openviking.service.experience_runtime import ExperienceRuntime, SearchExperienceRequest
from openviking.session.memory.dataclass import MemoryFile
from openviking.session.memory.utils.memory_file_utils import MemoryFileUtils
from openviking_cli.utils.config.agent_evolution_config import DagDeciderConfig
from openviking_cli.utils.config.jev_config import JevConfig

STATE = [
    {"role": "system", "content": "Use the authoritative rules and confirmed facts."},
    {"role": "user", "content": "Please modify the reservation, not cancel it."},
    {"role": "tool", "name": "lookup", "content": "Reservation exists; cancellation not allowed."},
]


def _noul(score):
    return {"type": "noul", "noul": score}


def _score(value):
    return {"type": "score", "score": value * 4}


def _mixed_answers():
    return {
        "experience_0_relevance": _score(0.8),
        "experience_1_relevance": _score(0.95),
        "slot_0": _noul(0.95),
        "slot_1": _noul(0.05),
        "slot_2": {"type": "choice", "choice": "cancel", "confidence": 0.9},
        "slot_3": _noul(0.95),
        "slot_4": _noul(0.05),
        "slot_5": {"type": "choice", "choice": "modify", "confidence": 0.9},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["vlm", "jev"])
async def test_relevance_and_mixed_node_types_share_one_backend_request(backend):
    instances = [_instance(), _instance()]
    instances[1].experience_uri = EXPERIENCE
    state = copy.deepcopy(STATE)
    calls = []
    answers = _mixed_answers()
    model = SimpleNamespace(
        get_completion_async=AsyncMock(return_value=json.dumps({"answers": answers}))
    )

    def respond(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"answers": answers})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        evaluator = (
            LlmDagDecisionEvaluator(model)
            if backend == "vlm"
            else JevClient(
                JevConfig(api_url="https://jev.invalid/v1/systemone", api_key="test"), client
            )
        )
        result = await ExperienceDagDecider(DagDeciderConfig(provider=backend), evaluator).decide(
            instances, state=state, select_limit=1
        )

    assert result == {EXPERIENCE: {"known": True, "eligible": False, "intent": "modify"}}
    if backend == "vlm":
        model.get_completion_async.assert_awaited_once()
        calls = [json.loads(model.get_completion_async.await_args.kwargs["messages"][1]["content"])]
    assert len(calls) == 1
    payload = calls[0]
    assert payload["state"] == STATE == state
    assert set(payload) == {"state", "questions"}
    assert len(payload["questions"]) == 8
    assert payload["questions"]["slot_5"]["type"] == "choice"
    assert payload["questions"]["slot_4"]["type"] == "noul"
    relevance = payload["questions"]["experience_1_relevance"]
    assert relevance["type"] == "score"
    assert isinstance(relevance["criteria"], list) and len(relevance["criteria"]) == 5
    assert "priority" in relevance["instructions"]
    assert "not a yes/no probability" in relevance["instructions"]
    assert "fractional" in relevance["instructions"]
    assert (
        "Modify the reservation" in payload["questions"]["experience_1_relevance"]["instructions"]
    )
    assert "Candidate Experience" not in json.dumps(payload["state"])
    assert all(not instance.slot_values for instance in instances)


@pytest.mark.asyncio
@pytest.mark.parametrize("score", [None, True, "3.6", -1, 4.1, float("nan"), float("inf"), 0.8])
async def test_missing_invalid_or_irrelevant_score_does_not_fall_back_to_node_truth(score):
    answers = {"slot_0": _noul(1)}
    if score is not None:
        answers["experience_0_relevance"] = {"type": "score", "score": score}
    evaluator = SimpleNamespace(evaluate=AsyncMock(return_value=answers))
    result = await ExperienceDagDecider(DagDeciderConfig(), evaluator).decide(
        [_instance()], state=STATE, select_limit=3
    )
    assert result == {}
    evaluator.evaluate.assert_awaited_once()


@pytest.mark.asyncio
async def test_relevance_does_not_establish_unknown_or_invalid_node_answers():
    evaluator = SimpleNamespace(
        evaluate=AsyncMock(
            return_value={
                "experience_0_relevance": _score(1),
                "slot_0": _noul(float("inf")),
                "slot_1": _noul(True),
                "slot_2": {"type": "choice", "choice": "invented", "confidence": 1},
            }
        )
    )
    instance = _instance()
    assert await ExperienceDagDecider(DagDeciderConfig(), evaluator).decide(
        [instance], state=STATE, select_limit=1
    ) == {instance.experience_uri: {}}


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 3, 10])
async def test_semantic_search_ranks_ten_candidates_with_one_shared_evaluation(env, limit):
    ctx, fs, _, resolver = env
    uris = [EXPERIENCE.replace("order.md", f"candidate_{i}.md") for i in range(10)]
    assert len(set(uris)) == 10
    for uri in uris:
        fs.files[fs._uri_to_path(uri, ctx)] = MemoryFileUtils.write(
            MemoryFile(content=TELL_PROGRAM, memory_type="experiences")
        )
    fs.find_memories = [SimpleNamespace(uri=uri) for uri in uris]
    before = dict(fs.files)
    answers = {f"slot_{i}": _noul(1) for i in range(10)}
    answers.update({f"experience_{i}_relevance": _score(0.1) for i in range(10)})
    answers.update(
        {
            "experience_9_relevance": _score(0.99),
            "experience_4_relevance": _score(0.9),
            "experience_2_relevance": _score(0.8),
        }
    )
    evaluator = SimpleNamespace(evaluate=AsyncMock(return_value=answers))
    runtime = ExperienceRuntime(
        fs, resolver, dag_decider=ExperienceDagDecider(DagDeciderConfig(), evaluator)
    )
    result = await runtime.search(
        SearchExperienceRequest(query="unchanged query", state=STATE, limit=limit),
        ctx,
    )
    assert fs.find_calls[0]["limit"] == 10
    assert fs.find_calls[0]["score_threshold"] == 0.15
    assert fs.find_calls[0]["query"] == "unchanged query"
    assert result.matched_experience_uris == [uris[9], uris[4], uris[2]][:limit]
    assert [item.experience_uri for item in result.experiences] == result.matched_experience_uris
    assert [item.experience_uri for item in result.instructions] == result.matched_experience_uris
    assert len(evaluator.evaluate.await_args.kwargs["questions"]) == 20
    evaluator.evaluate.assert_awaited_once()
    assert fs.files == before


@pytest.mark.asyncio
async def test_selection_is_ephemeral_and_does_not_override_conditions(env):
    ctx, fs, model, resolver = env
    fs.files[fs._uri_to_path(EXPERIENCE, ctx)] = MemoryFileUtils.write(
        MemoryFile(content=TELL_PROGRAM, memory_type="experiences")
    )
    before = dict(fs.files)
    runtime = ExperienceRuntime(fs, resolver)
    for relevance, condition, matched, advice in [
        (0.1, 1, False, False),
        (1, 0, True, False),
        (1, 1, True, True),
        (0.1, 1, False, False),
        (1, 1, True, True),
    ]:
        model.get_completion_async.return_value = json.dumps(
            {"answers": {"experience_0_relevance": _score(relevance), "slot_0": _noul(condition)}}
        )
        model.get_completion_async.reset_mock()
        result = await runtime.search(SearchExperienceRequest(query="q", state=STATE), ctx)
        assert bool(result.matched_experience_uris) is matched
        assert bool(result.instructions) is advice
        assert not result.errors
        model.get_completion_async.assert_awaited_once()
        if result.experiences:
            assert "reminder" not in result.experiences[0].slot_values
    assert fs.files == before


@pytest.mark.asyncio
async def test_explicit_experience_names_keep_node_only_evaluation(env):
    ctx, fs, model, resolver = env
    model.get_completion_async.return_value = json.dumps({"answers": {}})
    result = await ExperienceRuntime(fs, resolver).search(
        SearchExperienceRequest(query="q", state=STATE, experience_names=["order"]),
        ctx,
    )
    assert result.matched_experience_uris == [EXPERIENCE]
    assert not fs.find_calls
    payload = json.loads(model.get_completion_async.await_args.kwargs["messages"][1]["content"])
    assert not any(key.endswith("_relevance") for key in payload["questions"])
    model.get_completion_async.assert_awaited_once()


@pytest.mark.asyncio
async def test_jev_can_split_oversized_joint_batch_without_losing_typed_answers():
    instances = [_instance() for _ in range(6)]
    for index, instance in enumerate(instances):
        instance.experience_uri = f"viking://user/u/memories/experiences/candidate_{index}.md"
    calls = []

    def respond(request):
        payload = json.loads(request.content)
        calls.append(payload)
        return httpx.Response(
            200,
            json={
                "answers": {
                    key: (
                        _score(0.9)
                        if value["type"] == "score"
                        else _noul(0.9)
                        if value["type"] == "noul"
                        else {"type": "choice", "choice": "modify", "confidence": 0.9}
                    )
                    for key, value in payload["questions"].items()
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        evaluator = JevClient(
            JevConfig(
                api_url="https://jev.invalid/v1/systemone", api_key="test", max_input_tokens=1024
            ),
            client,
        )
        result = await ExperienceDagDecider(DagDeciderConfig(), evaluator).decide(
            instances, state=STATE, select_limit=3
        )
    assert len(calls) > 1
    assert sum(len(call["questions"]) for call in calls) == 24
    assert list(result) == [instance.experience_uri for instance in instances[:3]]
    assert all(values["intent"] == "modify" for values in result.values())


@pytest.mark.asyncio
async def test_model_resolution_failure_is_reported_without_stale_selection(env):
    ctx, fs, model, resolver = env
    resolver.get_vlm.side_effect = RuntimeError("account model unavailable")
    result = await ExperienceRuntime(fs, resolver).search(
        SearchExperienceRequest(query="q", state=STATE), ctx
    )
    assert not result.instructions and not result.matched_experience_uris
    assert len(result.errors) == 1
    assert result.errors[0].retryable
    assert result.errors[0].error == "account model unavailable"
    model.get_completion_async.assert_not_awaited()


@pytest.mark.asyncio
async def test_relevance_uses_configured_threshold_and_preserves_vector_order_on_ties():
    instances = [_instance() for _ in range(3)]
    for index, instance in enumerate(instances):
        instance.experience_uri = f"viking://user/u/memories/experiences/{index}.md"
    evaluator = SimpleNamespace(
        evaluate=AsyncMock(
            return_value={
                "experience_0_relevance": _score(0.75),
                "experience_1_relevance": _score(0.8),
                "experience_2_relevance": _score(0.8),
            }
        )
    )
    result = await ExperienceDagDecider(
        DagDeciderConfig(relevance_threshold=0.8), evaluator
    ).decide(instances, state=STATE, select_limit=1)
    assert list(result) == [instances[1].experience_uri]


@pytest.mark.asyncio
async def test_relevance_threshold_is_independent_of_node_truth_threshold():
    instance = _instance()
    evaluator = SimpleNamespace(
        evaluate=AsyncMock(
            return_value={
                "experience_0_relevance": _score(0.65),
                "slot_0": _noul(0.8),
                "slot_1": _noul(0.2),
            }
        )
    )
    config = DagDeciderConfig(relevance_threshold=0.6, noul_true_threshold=0.9)
    result = await ExperienceDagDecider(config, evaluator).decide(
        [instance], state=STATE, select_limit=3
    )
    assert result == {instance.experience_uri: {"eligible": False}}
    config.relevance_threshold = 0.7
    assert (
        await ExperienceDagDecider(config, evaluator).decide(
            [instance], state=STATE, select_limit=3
        )
        == {}
    )


@pytest.mark.asyncio
async def test_relevance_preserves_fractional_priority_and_rejects_old_noul_answer():
    instances = [_instance() for _ in range(3)]
    for i, instance in enumerate(instances):
        instance.experience_uri = f"viking://user/u/memories/experiences/{i}.md"
    evaluator = SimpleNamespace(
        evaluate=AsyncMock(
            return_value={
                "experience_0_relevance": _score(0.901),
                "experience_1_relevance": _score(0.902),
                "experience_2_relevance": _noul(1),
            }
        )
    )
    result = await ExperienceDagDecider(
        DagDeciderConfig(noul_true_threshold=0.99), evaluator
    ).decide(instances, state=STATE, select_limit=3)
    assert list(result) == [instances[1].experience_uri, instances[0].experience_uri]


@pytest.mark.parametrize("value", [-0.01, 1.01, float("nan"), float("inf")])
def test_relevance_threshold_rejects_values_outside_unit_interval(value):
    with pytest.raises(ValueError):
        DagDeciderConfig(relevance_threshold=value)


def test_relevance_threshold_default_and_independent_lower_boundary():
    assert DagDeciderConfig().relevance_threshold == 0.7
    assert DagDeciderConfig(relevance_threshold=0).noul_true_threshold == 0.7
