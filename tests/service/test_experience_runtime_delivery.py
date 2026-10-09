# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from test_experience_runtime import EXPERIENCE, TELL_PROGRAM
from test_experience_runtime import env as env

from openviking.service.experience_runtime import ExperienceRuntime, SearchExperienceRequest
from openviking.session.memory.dataclass import MemoryFile
from openviking.session.memory.utils.memory_file_utils import MemoryFileUtils
from openviking_cli.utils.config.agent_evolution_config import DagDeciderConfig

SESSION = "viking://session/s1"


@pytest.fixture(params=[None, "vlm"], ids=["default", "configured-vlm"])
def runtime(env, request):
    return ExperienceRuntime(
        env[1],
        env[3],
        dag_decider_config=(DagDeciderConfig(provider=request.param) if request.param else None),
    )


def store(env, program=TELL_PROGRAM, uri=EXPERIENCE):
    ctx, fs, _, _ = env
    fs.files[fs._uri_to_path(uri, ctx)] = MemoryFileUtils.write(
        MemoryFile(content=program, memory_type="experiences")
    )


def fill(env, runtime, **values):
    model = env[2]
    model.get_completion_async.side_effect = None
    payload = {
        "answers": {
            "experience_0_relevance": {"type": "score", "score": 4.0},
            **{
                f"slot_{i}": {"type": "noul", "noul": float(value)}
                for i, value in enumerate(values.values())
            },
        }
    }
    model.get_completion_async.return_value = json.dumps(payload)


async def search(env, runtime):
    return await runtime.search(
        SearchExperienceRequest(
            query="Current request", state=[{"role": "user", "content": "Current facts"}]
        ),
        env[0],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("edge", ["then", "if_true"])
async def test_applicability_is_evaluated_afresh_without_session_writes(env, runtime, edge):
    store(env, TELL_PROGRAM.replace("relevant.then", f"relevant.{edge}"))
    before = dict(env[1].files)
    env[1].write_file = AsyncMock(side_effect=AssertionError("server must not write"))
    env[1]._async_agfs.pathlock_acquire_exact = AsyncMock(
        side_effect=AssertionError("no state locks")
    )
    for values, expected in [
        ({"relevant": False}, []),
        ({"relevant": True}, ["reminder"]),
        ({"relevant": True}, ["reminder"]),
        ({}, []),
        ({"relevant": False}, []),
        ({"relevant": True}, ["reminder"]),
    ]:
        fill(env, runtime, **values)
        result = await search(env, runtime)
        assert result.errors == []
        assert [item.slot_name for item in result.instructions] == expected
        assert "reminder" not in result.experiences[0].slot_values
        assert not hasattr(result.experiences[0], "revision")
        payload = json.loads(
            env[2].get_completion_async.await_args.kwargs["messages"][1]["content"]
        )
        assert "previous_slot_values" not in payload.get("state", payload)
    assert env[1].files == before
    env[1].write_file.assert_not_awaited()
    env[1]._async_agfs.pathlock_acquire_exact.assert_not_awaited()


@pytest.mark.asyncio
async def test_each_request_ignores_old_server_delivery_state_and_reads_latest_template(
    env, runtime
):
    store(env)
    env[1].files[env[1]._uri_to_path(f"{SESSION}/.experience_instances/_recall.json", env[0])] = (
        json.dumps(
            {
                "active_experience_uris": [
                    f"viking://user/u/memories/experiences/old-{i}.md" for i in range(10)
                ],
                "completed_experience_uris": [EXPERIENCE],
                "invalid_experience_uris": [EXPERIENCE],
            }
        )
    )
    # A malformed obsolete snapshot must not even be read.
    env[1].files[
        env[1]._uri_to_path(
            f"{SESSION}/.experience_instances/{hashlib.sha256(EXPERIENCE.encode()).hexdigest()}/default.json",
            env[0],
        )
    ] = "broken"
    fill(env, runtime, relevant=True)
    first = await search(env, runtime)
    assert len(first.instructions) == 1
    store(
        env,
        TELL_PROGRAM.replace(
            "Verify the complete order total before acting", "Review the updated rule"
        ),
    )
    restarted = ExperienceRuntime(env[1], env[3], dag_decider_config=runtime.dag_decider_config)
    assert (await search(env, restarted)).instructions[0].description == "Review the updated rule"
    env[1].find_memories = []
    assert (await search(env, runtime)).instructions == []
    assert (await search(env, runtime)).experiences == []


@pytest.mark.asyncio
async def test_stateless_tell_nodes_do_not_block_later_advice(env, runtime):
    store(
        env,
        """dag = workflow("Review")
relevant = check("Current concern?")
first = tell("First concern")
later = check("Second concern?")
second = tell("Second concern")
relevant.then(first)
first.then(later)
later.then(second)""",
    )
    fill(env, runtime, relevant=True, later=False)
    assert [i.slot_name for i in (await search(env, runtime)).instructions] == ["first"]
    fill(env, runtime, relevant=True, later=True)
    for _ in range(2):
        result = await search(env, runtime)
        assert [i.slot_name for i in result.instructions] == ["first", "second"]
        assert set(result.experiences[0].slot_values) == {"relevant", "later"}


@pytest.mark.asyncio
async def test_failure_does_not_ban_experience_and_does_not_return_stale_advice(env, runtime):
    store(env)
    fill(env, runtime, relevant=True)
    assert (await search(env, runtime)).instructions
    env[2].get_completion_async.side_effect = httpx.ReadError("reset")
    failed = await search(env, runtime)
    assert failed.instructions == []
    assert len(failed.errors) == 1 and failed.errors[0].retryable
    fill(env, runtime)
    assert (await search(env, runtime)).instructions == []
    fill(env, runtime, relevant=True)
    assert (await search(env, runtime)).instructions


@pytest.mark.asyncio
async def test_invalid_template_is_rejected_but_valid_replacement_can_be_evaluated(env, runtime):
    store(env, "invalid")
    assert (await search(env, runtime)).errors
    store(env)
    fill(env, runtime, relevant=True)
    assert (await search(env, runtime)).instructions


@pytest.mark.asyncio
async def test_model_cannot_turn_advice_into_delivery_facts(env):
    store(env)
    decider = SimpleNamespace(
        decide=AsyncMock(return_value={EXPERIENCE: {"relevant": True, "reminder": True}})
    )
    runtime = ExperienceRuntime(
        env[1], env[3], dag_decider_config=DagDeciderConfig(provider="jev"), dag_decider=decider
    )
    for _ in range(2):
        result = await search(env, runtime)
        assert result.instructions[0].slot_name == "reminder"
        assert result.experiences[0].slot_values == {"relevant": True}
        assert result.experiences[0].executed_nodes == [1]


def test_search_does_not_accept_client_delivery_state():
    with pytest.raises(ValueError):
        SearchExperienceRequest(
            query="q", state=[{"role": "user", "content": "facts"}], delivered_node_ids=[1]
        )


@pytest.mark.asyncio
async def test_tell_successors_request_information_and_tool_steps_without_executing_them(
    env, runtime
):
    store(
        env,
        """dag = workflow("Review the requested record")
review = tell("Check which record the user means")
identifier = ask("What is the requested record ID?")
lookup = call("lookup_record", "Use the ID supplied by the user")
result = tell("Review the returned record before deciding")
review.then(identifier)
identifier.then(lookup)
lookup.then(result)""",
    )
    before = dict(env[1].files)
    for values, expected in [
        (
            {"identifier": False, "lookup": False},
            [("review", "TellAgent"), ("identifier", "AskUser")],
        ),
        ({"identifier": True, "lookup": False}, [("review", "TellAgent"), ("lookup", "CallTool")]),
        ({"identifier": True, "lookup": True}, [("review", "TellAgent"), ("result", "TellAgent")]),
    ]:
        fill(env, runtime, **values)
        result = await search(env, runtime)
        assert result.errors == []
        assert [(i.slot_name, i.provider.type) for i in result.instructions] == expected
        assert "review" not in result.experiences[0].slot_values
        assert "result" not in result.experiences[0].slot_values
    assert env[1].files == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "choice,confidence,expected",
    [
        ("modify", 0.9, ("modify", "TellAgent")),
        ("cancel", 0.9, ("cancel", "TellAgent")),
        ("__default__", 0.9, ("other", "TellAgent")),
        ("__unknown__", 0.9, ("intent", "AskUser")),
        ("modify", 0.1, ("intent", "AskUser")),
        ("undeclared", 0.9, ("intent", "AskUser")),
    ],
)
async def test_choose_routes_known_cases_without_treating_unknown_as_default(
    env, runtime, choice, confidence, expected
):
    store(
        env,
        """dag = workflow("Review the current operation")
intent = choose("Which operation is currently requested?")
modify = tell("Review modification constraints")
cancel = tell("Review cancellation eligibility")
other = tell("Review the rules for the other operation")
intent.case("modify", modify)
intent.case("cancel", cancel)
intent.default(other)""",
    )
    before = dict(env[1].files)
    env[2].get_completion_async.side_effect = None
    env[2].get_completion_async.return_value = json.dumps(
        {
            "answers": {
                "experience_0_relevance": {"type": "score", "score": 4.0},
                "slot_0": {"type": "choice", "choice": choice, "confidence": confidence},
            }
        }
    )

    result = await search(env, runtime)

    assert result.errors == []
    assert [(i.slot_name, i.provider.type) for i in result.instructions] == [expected]
    if expected[1] == "TellAgent":
        condition = result.instructions[0].conditions[0]
        assert condition.operator == ("not_in" if choice == "__default__" else "equals")
    else:
        assert "intent" not in result.experiences[0].slot_values
    assert env[1].files == before
