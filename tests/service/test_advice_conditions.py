# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_experience_runtime import EXPERIENCE
from test_experience_runtime import env as env

from benchmark.tau2.train.dag_experience_runtime import (
    EXPERIENCE_REMINDER_MARKER,
    Tau2DagExperienceRuntime,
)
from openviking.service.experience_runtime import (
    ExperienceInstruction,
    ExperienceRuntime,
    SearchExperienceRequest,
    _advance_result,
    _LoadedExperience,
)
from openviking.session.memory.dataclass import MemoryFile
from openviking.session.memory.experience.compiler import compile_dag
from openviking.session.memory.experience.dag import Dag, DagInstance, TellAgent
from openviking.session.memory.utils.memory_file_utils import MemoryFileUtils


def advance(source, **values):
    instance = DagInstance(experience_uri=EXPERIENCE, dag=Dag.from_content(compile_dag(source)))
    instance.merge_slot_values(values)
    result = _advance_result(_LoadedExperience(uri=EXPERIENCE, instance=instance))
    return {
        action.slot_name: ExperienceInstruction(
            experience_uri=EXPERIENCE,
            **action.model_dump(),
            conditions=instance.trigger_conditions(action.node_id),
        )
        for action in result.actions
        if isinstance(action.provider, TellAgent)
    }


def predicates(action):
    return [item.model_dump() for item in action.conditions]


def render(action):
    from benchmark.tau2.train.dag_experience_runtime import _render_advice

    return _render_advice(
        [
            {
                "experience_name": "internal-experience-name",
                "slot_name": "internal-slot-name",
                "instruction": action.description,
                "conditions": predicates(action),
            }
        ]
    )


def test_only_reached_ancestors_are_included_with_shared_ancestors_once():
    actions = advance(
        """dag = workflow("compound checks")
root = check("A or B")
left = check("C")
right = check("D")
other = check("Unrelated condition")
remind = tell("Review the total")
unrelated = tell("Unrelated advice")
root.then(left)
root.then(right)
left.then(remind)
right.then(remind)
other.then(unrelated)""",
        root=True,
        left=True,
        right=True,
        other=True,
    )
    assert [c.description for c in actions["remind"].conditions] == ["A or B", "C", "D"]
    assert [c.description for c in actions["unrelated"].conditions] == ["Unrelated condition"]
    note = render(actions["remind"])
    assert "以下条件需同时满足" in note
    assert "- A or B\n- C\n- D" in note
    assert "Unrelated" not in note


def test_prior_tell_is_not_a_delivery_condition_but_its_ancestors_are_preserved():
    actions = advance(
        """dag = workflow("two reminders")
ready = check("User has requested a review")
first = tell("First reminder")
later = check("New facts available")
second = tell("Second reminder")
ready.then(first)
first.then(later)
later.then(second)""",
        ready=True,
        later=True,
    )
    assert [c.description for c in actions["first"].conditions] == ["User has requested a review"]
    assert [c.description for c in actions["second"].conditions] == [
        "User has requested a review",
        "New facts available",
    ]
    assert "First reminder" not in json.dumps(predicates(actions["second"]))


@pytest.mark.parametrize(
    "value,slot,operator", [(True, "yes", "is_true"), (False, "no", "is_false")]
)
def test_if_else_preserves_whole_predicate_negation(value, slot, operator):
    actions = advance(
        """dag = workflow("condition branches")
eligible = check("A AND B")
yes = tell("Review eligible handling")
no = tell("Review ineligible handling")
eligible.if_true(yes)
eligible.if_false(no)""",
        eligible=value,
    )
    assert list(actions) == [slot]
    assert predicates(actions[slot]) == [
        {"description": "A AND B", "operator": operator, "values": []}
    ]
    if not value:
        assert "以下整体条件不成立：A AND B" in render(actions[slot])


@pytest.mark.parametrize(
    "choice,slot,operator,values",
    [
        ("change", "change_note", "equals", ["change"]),
        ("cancel", "cancel_note", "equals", ["cancel"]),
        ("__default__", "other_note", "not_in", ["cancel", "change"]),
    ],
)
def test_choice_conditions_keep_selected_or_explicit_default_branch(choice, slot, operator, values):
    actions = advance(
        """dag = workflow("intent")
intent = choose("What is the current request?")
change_note = tell("Review change rules")
cancel_note = tell("Review cancellation rules")
other_note = tell("Review other requests")
intent.case("change", change_note)
intent.case("cancel", cancel_note)
intent.default(other_note)""",
        intent=choice,
    )
    assert list(actions) == [slot]
    condition = predicates(actions[slot])[0]
    assert condition["operator"] == operator and condition["values"] == values
    note = render(actions[slot])
    if choice == "__default__":
        assert "其他明确分支" in note and "cancel、change" in note
        assert "__default__" not in note
    else:
        assert f"选择分支：{choice}" in note


def test_unknown_choice_does_not_become_default_and_inactive_true_nodes_do_not_leak():
    source = """dag = workflow("choice with conditions")
intent = choose("Which operation?")
a = check("Condition for A")
b = check("Condition for B")
a_note = tell("Review A")
b_note = tell("Review B")
other_note = tell("Review other")
intent.case("a", a)
intent.case("b", b)
intent.default(other_note)
a.then(a_note)
b.then(b_note)"""
    assert advance(source, a=True, b=True) == {}
    actions = advance(source, intent="a", a=True, b=True)
    assert list(actions) == ["a_note"]
    assert [c.description for c in actions["a_note"].conditions] == [
        "Which operation?",
        "Condition for A",
    ]


def test_completed_information_and_tool_steps_are_not_dropped_from_trigger_path():
    actions = advance(
        """dag = workflow("review after lookup")
known = ask("Order ID?")
looked_up = call("lookup_order", "Use the supplied order ID")
review = tell("Review the returned facts")
known.then(looked_up)
looked_up.then(review)""",
        known=True,
        looked_up=True,
    )
    conditions = actions["review"].conditions
    assert "user has supplied" in conditions[0].description
    assert "Order ID?" in conditions[0].description
    assert "tool step has completed successfully" in conditions[1].description
    assert "lookup_order" in conditions[1].description


def test_root_advice_has_no_invented_conditions_and_preserves_markdown_body():
    action = advance("""dag = workflow("review")
remind = tell("First line\\n\\n- Keep this list\\n- And this item")""")["remind"]
    note = render(action)
    assert action.conditions == []
    assert "未设置额外条件" in note
    assert note.startswith(EXPERIENCE_REMINDER_MARKER + "\n\n### 建议 1")
    assert note.endswith(action.description)
    assert "internal-experience-name" not in note and "internal-slot-name" not in note


@pytest.mark.asyncio
async def test_server_structured_conditions_reach_markdown_without_extra_model_calls(env, tmp_path):
    from vikingbot.config.schema import SessionKey
    from vikingbot.session.manager import SessionManager

    ctx, fs, model, resolver = env
    source = """dag = workflow("review totals")
requested = check("User asks for the full total")
remind = tell("Review all qualifying items before reporting the total")
requested.then(remind)"""
    fs.files[fs._uri_to_path(EXPERIENCE, ctx)] = MemoryFileUtils.write(
        MemoryFile(content=source, memory_type="experiences")
    )
    model.get_completion_async.return_value = json.dumps(
        {
            "answers": {
                "experience_0_relevance": {"type": "score", "score": 3.8},
                "slot_0": {"type": "noul", "noul": 0.95},
            }
        }
    )
    state = [{"role": "user", "content": "What is the full total?"}]
    result = await ExperienceRuntime(fs, resolver).search(
        SearchExperienceRequest(query="query-must-not-be-shown", state=state),
        ctx,
    )
    model.get_completion_async.assert_awaited_once()
    payload = result.model_dump()
    assert "conditions" not in payload["experiences"][0]["actions"][0]
    assert not result.errors
    assert payload["instructions"][0]["conditions"] == [
        {"description": "User asks for the full total", "operator": "is_true", "values": []}
    ]
    client = Tau2DagExperienceRuntime()
    manager = SessionManager(tmp_path)
    key = SessionKey(type="cli", channel_id="test", chat_id="markdown")
    client.bind_session(manager, key)
    client.client = SimpleNamespace(
        ensure_session=AsyncMock(), search_advice=AsyncMock(return_value=payload)
    )
    client.record_user_message("query-must-not-be-shown")
    notes = []
    async for note in client.recall(state):
        assert not client._delivered
        notes.append(note)
        await client.record_injected(note)
    assert len(notes) == 1
    assert "**适用条件**\n- User asks for the full total" in notes[0]
    assert "**建议**\nReview all qualifying items" in notes[0]
    assert all(
        value not in notes[0]
        for value in [
            "query-must-not-be-shown",
            EXPERIENCE,
            '"slot_name"',
            '"provider"',
            '"status"',
        ]
    )
    expected_key = (EXPERIENCE, "remind", payload["instructions"][0]["provider"]["instruction"])
    assert client._delivered == {expected_key}
    assert client._pending is None
    # Changing only the displayed condition is not a new advice identity.
    payload["instructions"][0]["conditions"][0]["description"] = "A different applicable condition"
    client.record_user_message("Recheck")
    assert [note async for note in client.recall(state)] == []
    assert client.recalls[-1]["status"] == "no_applicable_advice"
    restarted = Tau2DagExperienceRuntime()
    restarted.bind_session(manager, key)
    assert restarted._delivered == {expected_key}
    model.get_completion_async.assert_awaited_once()


def test_markdown_sections_keep_each_advice_with_its_own_conditions():
    from benchmark.tau2.train.dag_experience_runtime import _render_advice

    text = _render_advice(
        [
            {
                "instruction": "First recommendation",
                "conditions": [
                    {"description": "First condition\ncontinued", "operator": "is_true"}
                ],
            },
            {
                "instruction": "Second recommendation",
                "conditions": [{"description": "Second condition", "operator": "is_false"}],
            },
        ]
    )
    first, second = text.split("### 建议 2")
    assert "### 建议 1" in first and "First recommendation" in first
    assert "- First condition\n  continued" in first
    assert "Second condition" not in first
    assert "以下整体条件不成立：Second condition" in second
    assert "Second recommendation" in second and "First condition" not in second
