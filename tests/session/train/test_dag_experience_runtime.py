from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from benchmark.tau2.train.dag_experience_runtime import (
    EXPERIENCE_REMINDER_MARKER,
    Tau2DagExperienceRuntime,
    _session_state,
)
from openviking.session.memory.experience.state import MAX_SESSION_STATE_CHARS, serialize_state
from openviking.session.train.components.session_analyzer import experience_execution_from_runtime

URI = "viking://user/default/memories/experiences/order.md"


def advice(
    *, uri=URI, slot="review", text="Review current constraints", node_id=1, kind="TellAgent"
):
    provider = {"type": kind}
    if kind == "TellAgent":
        provider["instruction"] = text
    item = {
        "experience_uri": uri,
        "node_id": node_id,
        "slot_name": slot,
        "provider": provider,
        "description": text,
        "conditions": [],
    }
    return {
        "matched_experience_uris": [uri],
        "instructions": [item],
        "errors": [],
        "experiences": [
            {
                "experience_uri": uri,
                "state": "running",
                "actions": [item],
                "node_slots": {node_id: slot},
                "slot_values": {},
                "executed_nodes": [],
                "current_nodes": [node_id],
            }
        ],
    }


def client(result=None):
    return SimpleNamespace(
        search_advice=AsyncMock(return_value=advice() if result is None else result),
        close=AsyncMock(),
    )


def bind(runtime, path, chat="conversation"):
    from vikingbot.config.schema import SessionKey
    from vikingbot.session.manager import SessionManager

    manager = SessionManager(path)
    key = SessionKey(type="cli", channel_id="test", chat_id=chat)
    runtime.bind_session(manager, key)
    return manager, key


async def recall(runtime, query="Current request", messages=None):
    runtime.record_user_message(query)
    notes = [note async for note in runtime.recall(messages or [])]
    return notes[0] if notes else None


def proposal(*names):
    return [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": f"call-{i}",
                    "type": "function",
                    "function": {"name": name, "arguments": "{}"},
                }
                for i, name in enumerate(names)
            ],
        }
    ]


@pytest.mark.asyncio
async def test_recall_preserves_policy_advice_and_real_tool_fields_in_order():
    runtime = Tau2DagExperienceRuntime()
    runtime.client = client({"instructions": [], "experiences": [], "errors": []})
    messages = [
        {"role": "system", "content": "Task policy explains [Experience Reminder]."},
        {"role": "user", "content": "Change my booking"},
        {"role": "user", "content": "[Experience Reminder]\nReview scope"},
        {"role": "user", "content": "What does [Experience Reminder] mean?"},
        {"role": "tool", "name": "get_booking", "content": "Current result"},
    ]
    original = copy.deepcopy(messages)
    assert await recall(runtime, messages=messages) is None
    assert runtime.client.search_advice.call_args.args[0] == original
    assert runtime.client.search_advice.call_args.kwargs == {"query": "Current request"}
    assert messages == original and not runtime._delivered


@pytest.mark.asyncio
async def test_each_user_turn_recalls_once_and_short_replies_keep_context():
    runtime = Tau2DagExperienceRuntime()
    runtime.client = client({})
    runtime.record_user_message("Change my booking")
    assert [n async for n in runtime.recall([])] == []
    assert [n async for n in runtime.recall(proposal("get_booking"))] == []
    runtime.record_user_message("Yes", "Economy?")
    runtime.record_user_message("Yes", "Confirm the change?")
    assert [n async for n in runtime.recall([])] == []
    calls = runtime.client.search_advice.call_args_list
    assert len(calls) == 3
    assert calls[0].kwargs["query"] == "Change my booking"
    assert "Economy?" in calls[1].kwargs["query"]
    assert "Confirm the change?" in calls[2].kwargs["query"]
    assert all("Change my booking" in c.kwargs["query"] for c in calls)
    assert [r["user_message_index"] for r in runtime.recalls] == [0, 1, 2]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "names,expected",
    [
        (("get_booking",), False),
        (("communicate_with_user",), False),
        (("write_booking",), True),
        (("done",), True),
        (("transfer_to_human_agents",), True),
        (("get_booking", "write_booking"), True),
    ],
)
async def test_only_consequential_tool_proposals_trigger_checkpoint(names, expected):
    runtime = Tau2DagExperienceRuntime(review_tools={"write_booking"})
    runtime.client = client({})
    runtime.record_user_message("Update my booking")
    await recall(runtime)
    runtime.client.search_advice.reset_mock()
    messages = proposal(*names)
    assert [n async for n in runtime.recall(messages, before_tools=True)] == []
    assert runtime.client.search_advice.await_count == int(expected)
    if expected:
        assert runtime.recalls[-1]["trigger"] == "before_action"
        assert all(name in runtime.recalls[-1]["query"] for name in names)
        assert (
            runtime.client.search_advice.call_args.args[0][-1]["tool_calls"]
            == messages[-1]["tool_calls"]
        )


@pytest.mark.asyncio
async def test_uninserted_advice_remains_eligible_then_persists_without_server_ack(tmp_path):
    runtime = Tau2DagExperienceRuntime(review_tools={"write_booking"})
    manager, key = bind(runtime, tmp_path)
    runtime.client = client()
    note = await recall(runtime)
    assert note.startswith(EXPERIENCE_REMINDER_MARKER)
    assert "**适用条件**" in note and "**建议**" in note
    assert '"query"' not in note and URI not in note
    assert not manager.has_persisted(key)
    assert await recall(runtime) == note
    await runtime.record_injected(note)
    assert manager.has_persisted(key)
    assert [n async for n in runtime.recall(proposal("write_booking"), before_tools=True)] == []
    assert runtime.recalls[-1]["status"] == "no_applicable_advice"
    assert set(runtime.client.search_advice.call_args.kwargs) == {"query"}
    restarted = Tau2DagExperienceRuntime()
    bind(restarted, tmp_path)
    restarted.client = client()
    assert await recall(restarted) is None
    other = Tau2DagExperienceRuntime()
    bind(other, tmp_path, "other-conversation")
    other.client = client()
    assert await recall(other) == note
    await runtime.close()


@pytest.mark.asyncio
async def test_identity_uses_uri_slot_and_text_not_node_ids(tmp_path):
    runtime = Tau2DagExperienceRuntime()
    bind(runtime, tmp_path)
    runtime.client = client()
    await runtime.record_injected(await recall(runtime))
    runtime.client.search_advice.return_value = advice(node_id=9)
    assert await recall(runtime) is None
    for changes in ({"slot": "other"}, {"text": "Changed guidance"}, {"uri": URI + "other"}):
        runtime.client.search_advice.return_value = advice(**changes)
        assert await recall(runtime)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["TellAgent", "AskUser", "CallTool"])
async def test_delivery_is_recorded_separately_from_condition_or_tool_completion(tmp_path, kind):
    runtime = Tau2DagExperienceRuntime()
    bind(runtime, tmp_path)
    runtime.client = client(advice(kind=kind))
    note = await recall(runtime)
    assert (
        "delivered_advice" not in experience_execution_from_runtime({"events": runtime.events})[URI]
    )
    await runtime.record_injected(note)
    snapshot = experience_execution_from_runtime({"events": runtime.events})[URI]
    assert snapshot["slot_values"] == {} and snapshot["executed_nodes"] == []
    assert snapshot["current_nodes"] == ["review"]
    assert snapshot["delivered_advice"] == [
        {"slot_name": "review", "instruction": "Review current constraints", "provider_type": kind}
    ]
    assert snapshot["state"] == "running"


@pytest.mark.asyncio
async def test_changed_advice_and_failed_search_do_not_erase_delivery_facts(tmp_path):
    runtime = Tau2DagExperienceRuntime()
    bind(runtime, tmp_path)
    runtime.client = client()
    await runtime.record_injected(await recall(runtime))
    runtime.client.search_advice.return_value = advice(text="Changed", node_id=8)
    note = await recall(runtime)
    snapshot = experience_execution_from_runtime({"events": runtime.events})[URI]
    assert [x["instruction"] for x in snapshot["delivered_advice"]] == [
        "Review current constraints"
    ]
    assert snapshot["slot_values"] == {}
    await runtime.record_injected(note)
    runtime.client.search_advice.side_effect = httpx.ReadError("offline")
    assert await recall(runtime) is None
    snapshot = experience_execution_from_runtime({"events": runtime.events})[URI]
    assert [x["instruction"] for x in snapshot["delivered_advice"]] == [
        "Review current constraints",
        "Changed",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", [None, False, True])
async def test_current_condition_facts_do_not_become_delivery_facts(tmp_path, condition):
    runtime = Tau2DagExperienceRuntime()
    bind(runtime, tmp_path)
    runtime.client = client()
    await runtime.record_injected(await recall(runtime))
    payload = advice(node_id=8)
    facts = {} if condition is None else {"relevant": condition}
    payload["experiences"][0].update(
        slot_values=facts, node_slots={3: "relevant", 8: "review"}, executed_nodes=[]
    )
    runtime.client.search_advice.return_value = payload
    assert await recall(runtime) is None
    snapshot = experience_execution_from_runtime({"events": runtime.events})[URI]
    assert snapshot["slot_values"] == facts
    assert len(snapshot["delivered_advice"]) == 1


@pytest.mark.asyncio
async def test_identical_markdown_from_distinct_sources_is_acknowledged_in_yield_order(tmp_path):
    runtime = Tau2DagExperienceRuntime()
    bind(runtime, tmp_path)
    runtime.client = client()
    runtime.client.search_advice.side_effect = [advice(uri=URI + "a"), advice(uri=URI + "b")]
    runtime.record_user_message("First")
    runtime.record_user_message("Second")
    notes = []
    async for note in runtime.recall([]):
        notes.append(note)
        await runtime.record_injected(note)
    assert notes[0] == notes[1]
    assert {key[0] for key in runtime._delivered} == {URI + "a", URI + "b"}


@pytest.mark.asyncio
async def test_persistence_failure_dedups_in_memory_and_close_retries(tmp_path, monkeypatch):
    runtime = Tau2DagExperienceRuntime()
    manager, _ = bind(runtime, tmp_path)
    runtime.client = client()
    save = manager.save
    monkeypatch.setattr(manager, "save", AsyncMock(side_effect=OSError("disk unavailable")))
    with pytest.raises(OSError):
        await runtime.record_injected(await recall(runtime))
    assert await recall(runtime) is None
    monkeypatch.setattr(manager, "save", save)
    await runtime.close()
    restarted = Tau2DagExperienceRuntime()
    bind(restarted, tmp_path)
    assert restarted._delivered


@pytest.mark.asyncio
async def test_concurrent_runtime_saves_merge_delivery_without_losing_conversation(tmp_path):
    runtimes = [Tau2DagExperienceRuntime(), Tau2DagExperienceRuntime()]
    for i, runtime in enumerate(runtimes):
        manager, key = bind(runtime, tmp_path)
        session = manager.get_or_create(key)
        session.messages.append({"role": "user", "content": "Keep this conversation"})
        session.metadata["other"] = {"enabled": True}
        runtime.client = client(advice(slot=f"node_{i}"))
    notes = [await recall(runtime) for runtime in runtimes]
    await asyncio.gather(
        *(r.record_injected(note) for r, note in zip(runtimes, notes, strict=True))
    )
    restarted = Tau2DagExperienceRuntime()
    manager, key = bind(restarted, tmp_path)
    assert {k[1] for k in restarted._delivered} == {"node_0", "node_1"}
    assert manager.get_or_create(key).messages
    assert manager.get_or_create(key).metadata["other"] == {"enabled": True}


def test_corrupt_delivery_records_fail_binding(tmp_path):
    runtime = Tau2DagExperienceRuntime()
    manager, key = bind(runtime, tmp_path)
    manager.get_or_create(key).metadata["delivered_experience_advice"] = [["bad"]]
    with pytest.raises(ValueError, match="Invalid client advice"):
        Tau2DagExperienceRuntime().bind_session(manager, key)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,status",
    [
        ({}, "no_match"),
        ({"matched_experience_uris": [URI]}, "no_applicable_advice"),
        (
            {"errors": [{"experience_uri": URI, "error": "unavailable", "retryable": True}]},
            "unavailable",
        ),
    ],
)
async def test_no_match_inactive_and_unavailable_are_distinct(payload, status):
    runtime = Tau2DagExperienceRuntime()
    runtime.client = client(payload)
    assert await recall(runtime) is None
    assert runtime.recalls[-1]["status"] == status


@pytest.mark.asyncio
async def test_invalid_experience_does_not_block_valid_instructions(tmp_path):
    runtime = Tau2DagExperienceRuntime()
    bind(runtime, tmp_path)
    payload = advice()
    payload["errors"] = [{"experience_uri": URI + "bad", "error": "invalid", "retryable": False}]
    runtime.client = client(payload)
    assert await recall(runtime)
    assert any(e.get("state") == "invalid" for e in runtime.events)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [httpx.ReadError("offline"), ValueError("bad response")])
async def test_recall_failure_is_nonfatal_and_next_checkpoint_can_retry(failure):
    runtime = Tau2DagExperienceRuntime()
    runtime.client = client()
    runtime.client.search_advice.side_effect = failure
    assert await recall(runtime) is None
    assert runtime.recalls[-1]["status"] == "unavailable"
    runtime.client.search_advice.side_effect = None
    assert [n async for n in runtime.recall(proposal("done"), before_tools=True)]


@pytest.mark.asyncio
async def test_cancellation_propagates_without_marking_delivery():
    runtime = Tau2DagExperienceRuntime()
    runtime.client = client()
    runtime.client.search_advice.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await recall(runtime)
    assert not runtime._delivered


@pytest.mark.asyncio
async def test_bad_instruction_metadata_is_not_injected_or_marked_delivered():
    runtime = Tau2DagExperienceRuntime()
    payload = advice()
    payload["instructions"][0]["conditions"] = [
        {"description": "bad", "operator": "not_in", "values": []}
    ]
    runtime.client = client(payload)
    assert await recall(runtime) is None
    assert runtime.recalls[-1]["status"] == "unavailable"
    assert not runtime._delivered and runtime._pending is None


def test_state_preserves_native_calls_and_delivery_but_not_private_reasoning():
    messages = [
        {"role": "system", "content": "Rules mentioning [Experience Reminder]"},
        {"role": "assistant", "content": "Reviewing", "reasoning_content": "PRIVATE"},
        *proposal("write_booking"),
        {
            "role": "tool",
            "name": "write_booking",
            "tool_call_id": "call-0",
            "content": "Error: not executed",
        },
        {"role": "assistant", "content": "Delivered reply"},
    ]
    state = _session_state(messages, delivered_assistant_messages=["Delivered reply"])
    assert state[1]["delivered_to_user"] is False and state[-1]["delivered_to_user"] is True
    assert state[2]["tool_calls"] == messages[2]["tool_calls"]
    assert state[3]["tool_call_id"] == "call-0"
    assert "PRIVATE" not in serialize_state(state)


def test_large_state_is_bounded_and_does_not_mutate_input():
    messages = [
        {"role": "tool", "name": "get_booking", "tool_call_id": "call", "content": "x" * 200000}
    ]
    state = _session_state(messages)
    assert len(serialize_state(state)) <= MAX_SESSION_STATE_CHARS
    assert state[0]["tool_call_id"] == "call" and len(messages[0]["content"]) == 200000


def test_each_rollout_has_a_fresh_local_id():
    a, b = Tau2DagExperienceRuntime(), Tau2DagExperienceRuntime()
    assert a.runtime_id != b.runtime_id and a.runtime_id.startswith("tau2_advice_")


@pytest.mark.asyncio
async def test_delivery_save_preserves_latest_session_object(tmp_path):
    runtime = Tau2DagExperienceRuntime()
    manager, key = bind(runtime, tmp_path)
    runtime.client = client()
    note = await recall(runtime)
    await manager.update_session(
        key, lambda session: session.messages.append({"role": "user", "content": "newer message"})
    )
    await runtime.record_injected(note)
    from vikingbot.session.manager import SessionManager

    restored = SessionManager(tmp_path).get_or_create(key)
    assert restored.messages == [{"role": "user", "content": "newer message"}]
    assert restored.metadata["delivered_experience_advice"]


@pytest.mark.asyncio
@pytest.mark.parametrize("save_fails", [False, True])
async def test_injected_advice_names_are_captured_before_persistence(
    tmp_path, monkeypatch, save_fails
):
    runtime = Tau2DagExperienceRuntime()
    bind(runtime, tmp_path)
    payload = advice(slot="require_chinese", text="Communicate clearly.")
    payload["instructions"].extend(
        advice(slot="ask_reason", text="Ask for the reason.", node_id=2)["instructions"]
    )
    runtime.client = client(payload)
    note = await recall(runtime)
    captured = {"role": "user", "content": note, "after_tool_count": 3}
    original = copy.deepcopy(captured)
    if save_fails:
        monkeypatch.setattr(runtime, "_save", AsyncMock(side_effect=OSError("disk error")))
        with pytest.raises(OSError, match="disk error"):
            await runtime.record_injected(note, captured_message=captured)
    else:
        await runtime.record_injected(note, captured_message=captured)
    assert captured == {**original, "advice_names": ["require_chinese", "ask_reason"]}
    assert "require_chinese" not in note and "ask_reason" not in note
    assert len(runtime._delivered) == 2


@pytest.mark.asyncio
async def test_identical_reminders_capture_their_own_advice_names(tmp_path):
    runtime = Tau2DagExperienceRuntime()
    bind(runtime, tmp_path)
    runtime.client = client()
    runtime.client.search_advice.side_effect = [advice(slot="first"), advice(slot="second")]
    captures = []
    for _ in range(2):
        note = await recall(runtime)
        captured = {"role": "user", "content": note, "after_tool_count": 0}
        await runtime.record_injected(note, captured_message=captured)
        captures.append(captured)
    assert captures[0]["content"] == captures[1]["content"]
    assert [x["advice_names"] for x in captures] == [["first"], ["second"]]


@pytest.mark.asyncio
async def test_mismatched_capture_is_not_annotated_with_advice_names(tmp_path):
    runtime = Tau2DagExperienceRuntime()
    bind(runtime, tmp_path)
    runtime.client = client()
    note = await recall(runtime)
    captured = {"content": "a different message"}
    await runtime.record_injected(note, captured_message=captured)
    assert captured == {"content": "a different message"}
    assert len(runtime._delivered) == 1
