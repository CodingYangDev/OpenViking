from __future__ import annotations

import json

import pytest

from openviking.message import Message, TextPart
from openviking.session.memory.dataclass import ResolvedOperation, ResolvedOperations
from openviking.session.memory.experience.trajectory_context import (
    SYSTEM_STEP_MAX_CHARS,
    AgentTrajectoryContextProvider,
    trajectory_source_steps,
)


def message(text: str, role: str = "user") -> Message:
    return Message(id="m", role=role, parts=[TextPart(text=text)])


def operations(content: str, *, count: int = 1, memory_type: str = "trajectories"):
    return ResolvedOperations(
        upsert_operations=[
            ResolvedOperation(
                memory_type=memory_type,
                uris=["viking://user/u/memories/trajectories/model_case/model_name.md"],
                memory_fields={
                    "content": content,
                    "trajectory_name": "model_name",
                    "case_name": "model_case",
                    "outcome": "success",
                },
            )
            for _ in range(count)
        ],
        delete_file_contents=[],
        errors=[],
    )


@pytest.mark.parametrize(
    "content",
    [
        "",
        "The user asked to cancel and the agent checked eligibility.",
        "# Trajectory\n1. Missing context section",
        "# Session Context\nRules without any trajectory",
        "# Trajectory\n1. step\n# Session Context\nreversed",
        "# Session Context\n# Trajectory\n1. step\n# Session Context\nextra",
        "# Session Context\n# Trajectory\n1. step\n# Trajectory\n2. extra",
        "# Session Context\n1. Rule only\n# Trajectory\n",
        "# Session Context\n# Trajectory\n2. first\n5. second",
        '1. `tool:update` flights=[{"flight_number":"HAT072"}], payment_id=gift',
        "```markdown\n# Session Context\nRule\n# Trajectory\n1. step\n```",
        "\n".join(f"{index}. step" for index in range(1, 301)),
    ],
)
def test_content_is_kept_without_heading_numbering_count_or_type_binding(content):
    provider = AgentTrajectoryContextProvider(
        messages=[message("Cancel the booking")],
        source_steps=[{"type": "user", "content": "Cancel the booking"}],
        trajectory_name="server_fixed",
        case_name="case",
        outcome="failure",
        target_uri="viking://user/u/memories/trajectories/case/server_fixed.md",
    )
    result = operations(content)

    assert provider.validate_resolved_operations(result) == []

    operation = result.upsert_operations[0]
    assert operation.memory_fields == {
        "content": content,
        "trajectory_name": "server_fixed",
        "case_name": "case",
        "outcome": "failure",
    }
    assert operation.uris == [provider.target_uri]


@pytest.mark.parametrize("count", [0, 2])
def test_single_trajectory_constraint_is_retained(count):
    provider = AgentTrajectoryContextProvider(messages=[], source_steps=[])
    errors = provider.validate_resolved_operations(operations("plain text", count=count))
    assert errors[0]["error"] == "Trajectory extraction must create exactly one memory"


def test_trajectory_memory_type_constraint_is_retained():
    provider = AgentTrajectoryContextProvider(messages=[], source_steps=[])
    errors = provider.validate_resolved_operations(
        operations("plain text", memory_type="experiences")
    )
    assert errors[0]["error"] == "The only operation must create a Trajectory"


def test_source_steps_preserve_system_control_dialogue_and_duplicate_messages():
    messages = [
        message("system:\nOriginal policy."),
        message("Original policy.", "system"),
        message("Original policy.", "system"),
        message("# OpenViking Policy\nObtain authorization.", "system"),
        message("# OpenViking Batch Training CaseSpec v1\ncontrol", "system"),
        message("system:\n# OpenViking OutcomeEvaluation\ncontrol"),
        message("task_success: true", "system"),
        message("[Rollout Evaluation] passed", "system"),
        message("My request is to cancel."),
        message("I think cancellation is forbidden.", "assistant"),
        message(""),
        message("[Experience Reminder] Verify eligibility.", "assistant"),
    ]

    steps = trajectory_source_steps(messages)
    assert [step["content"] for step in steps] == [item.content for item in messages]
    assert [step["type"] for step in steps] == [item.role for item in messages[:-1]] + [
        "experience_reminder"
    ]


@pytest.mark.parametrize("role,prefix", [("system", ""), ("user", "system:\n")])
def test_system_rules_keep_larger_budget_independent_of_record_count(role, prefix):
    rules = (
        prefix
        + "Eligibility rules.\n"
        + "policy " * 3_000
        + "\nException: duplicate bookings qualify."
    )
    messages = [message(rules, role)] + [message("ordinary " * 500) for _ in range(100)]
    steps = trajectory_source_steps(messages)

    assert 16_384 < len(rules) < SYSTEM_STEP_MAX_CHARS
    assert steps[0] == {"type": role, "content": rules}
    assert all(len(step["content"]) <= 64_000 // len(messages) for step in steps[1:])
    assert messages[0].content == rules


@pytest.mark.parametrize("role,prefix", [("system", ""), ("user", "system:\n")])
def test_oversized_system_rules_are_still_bounded_with_head_and_tail(role, prefix):
    text = prefix + "RULE_START\n" + "x" * (SYSTEM_STEP_MAX_CHARS * 2) + "\nEXCEPTION_END"
    steps = trajectory_source_steps([message(text, role)])

    assert len(steps[0]["content"]) == SYSTEM_STEP_MAX_CHARS
    assert steps[0]["content"].startswith(prefix + "RULE_START\n")
    assert steps[0]["content"].endswith("\nEXCEPTION_END")
    assert "truncated" in steps[0]["content"]


def test_empty_source_steps_stay_empty():
    assert trajectory_source_steps([]) == []


def test_source_can_exceed_256_records_with_shared_text_budget():
    records = trajectory_source_steps([message("x" * 2_000) for _ in range(300)])
    assert len(records) == 300
    assert all(record["type"] == "user" for record in records)
    assert sum(len(record["content"]) for record in records) <= 64_000


@pytest.mark.asyncio
async def test_system_rules_reach_model_once_through_source_steps():
    messages = [
        message("system:\nCancel only after confirmation."),
        message("Cancel the booking"),
    ]
    steps = trajectory_source_steps(messages)
    provider = AgentTrajectoryContextProvider(messages=messages, source_steps=steps)
    payload = json.loads((await provider.prefetch())[0]["content"])

    assert set(payload) == {"case", "source_steps"}
    assert payload["source_steps"] == steps
    assert payload["source_steps"][0] == {
        "type": "user",
        "content": "system:\nCancel only after confirmation.",
    }
    assert not hasattr(provider, "source_context")
    prompt = provider.instruction()
    assert "session_context" not in prompt
    assert "All source messages are supplied once in source_steps" in prompt
    assert "# Session Context" in prompt
    assert "training metadata, not execution-time rules or actions" in prompt
    content = "Confirmation precedes cancellation.\nThe user asked to cancel."
    result = operations(content)
    assert provider.validate_resolved_operations(result) == []
    assert result.upsert_operations[0].memory_fields["content"] == content
