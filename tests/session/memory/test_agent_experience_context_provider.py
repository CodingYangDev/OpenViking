from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
import yaml

from openviking.message import Message, TextPart
from openviking.session.memory.experience.experience_context import (
    AgentExperienceContextProvider,
    _existing_experience_message,
    _pack_trajectory_comparison,
    _trajectory_comparison_messages,
)
from openviking.session.train import Case, Rubric, Trajectory


def metadata(message):
    return yaml.safe_load(message["content"].split("```yaml\n", 1)[1].split("\n```", 1)[0])


@pytest.mark.parametrize("error", ["File not found: missing.md", "Permission denied", "ReadError"])
def test_existing_experience_errors_are_shown_without_claiming_an_existing_memory(error):
    text = _existing_experience_message(
        "viking://user/u/memories/experiences/case.md", {"error": error}
    )
    assert error in text
    assert "The existing memory was read." not in text
    assert "Only an explicit not-found result permits creation" in text


def test_existing_experience_content_is_not_json_escaped_or_line_numbered():
    source = 'dag = workflow("示例")\nreminder = tell("Verify eligibility")'
    numbered = '1\tdag = workflow("示例")\n2\treminder = tell("Verify eligibility")'
    text = _existing_experience_message("experience", {"content": numbered})
    assert text.endswith(source)
    assert "\\n" not in text


@pytest.mark.asyncio
async def test_prefetch_reads_only_fixed_case_experience():
    uri = "viking://user/u/memories/experiences/case_a.md"
    case = Case(name="case_a", task_signature="cancel", input={}, rubric=Rubric("r", "", []))
    provider = AgentExperienceContextProvider(
        messages=[Message(id="m", role="user", parts=[TextPart(text="cancel duplicate")])],
        case=case,
        target_uri=uri,
        source_session_uri="viking://session/s/archives/1",
        trajectories=[
            Trajectory(
                name="sample",
                uri="viking://user/u/memories/trajectories/case_a/sample.md",
                outcome="failure",
                content="# Session Context\n\n# Trajectory\n\n1. `user` cancel duplicate\n",
                metadata={
                    "case_name": "case_a",
                    "source_session_uri": "viking://session/s/archives/1",
                    "evaluation": None,
                    "evidence": [],
                },
            )
        ],
    )
    provider._eval_memory = AsyncMock(
        return_value={
            "experience": {
                "content": "dag = workflow('cancel')",
                "source_sessions": [{"feedback": "raw evaluator payload"}],
            }
        }
    )
    provider.search_files = AsyncMock(
        side_effect=AssertionError("must not search other Experiences")
    )
    result = provider.format_prefetch_messages(await provider.prefetch())
    provider._eval_memory.assert_awaited_once_with(f"experience = sdk.read(uri={uri!r})")
    provider.search_files.assert_not_called()
    text = json.dumps(result, ensure_ascii=False)
    assert "cancel duplicate" in text and "case_a" in text
    assert len(result) == 4
    assert all(message["role"] == "user" for message in result)
    assert all(set(message) == {"role", "content"} for message in result)
    assert metadata(result[0])["case"]["name"] == "case_a"
    assert "raw evaluator payload" not in text
    trajectory = metadata(result[1])
    assert trajectory["outcome"] == "failure"
    assert result[1]["content"].endswith(provider.trajectories[0].content)
    assert result[2]["content"].startswith("# Existing Experience\n")
    assert result[2]["content"].endswith("dag = workflow('cancel')")
    assert "task_query" not in trajectory
    assert "evidence" not in trajectory
    assert "exact name" in provider.instruction()
    assert "executed_nodes" in provider.instruction()
    assert "slot_values" in provider.instruction()
    assert "delivered_advice" not in provider.instruction()
    assert provider.get_tools() == []
    final_instruction = result[-1]["content"]
    assert "experience.content.overwrite(...)" in final_instruction
    assert "sdk.create_experiences(...)" in final_instruction
    assert "sdk.commit() for no change" in final_instruction
    assert "Do not return a bare DAG" in final_instruction


def test_comparison_budget_keeps_all_current_and_omits_history():
    def record(index: int, outcome: str, size: int) -> dict:
        return {
            "trajectory_uri": f"trajectory:{index}",
            "outcome": outcome,
            "content": "x" * size,
        }

    current = [record(0, "failure", 200), record(1, "failure", 200)]
    history = [record(2, "success", 2_000)]
    base = _pack_trajectory_comparison(
        case={},
        target_uri="experience",
        records=current,
        current_trajectory_uris={"trajectory:0", "trajectory:1"},
        char_limit=100_000,
    )
    budget = sum(len(message["content"]) for message in _trajectory_comparison_messages(base)) + 100
    packed = _pack_trajectory_comparison(
        case={},
        target_uri="experience",
        records=[*current, *history],
        current_trajectory_uris={"trajectory:0", "trajectory:1"},
        char_limit=budget,
    )

    selected = [
        *packed["successful_trajectories"],
        *packed["failed_trajectories"],
        *packed["other_trajectories"],
    ]
    assert {item["trajectory_uri"] for item in selected} == {
        "trajectory:0",
        "trajectory:1",
    }
    assert packed["omitted_historical_trajectory_count"] == 1
    assert (
        sum(len(message["content"]) for message in _trajectory_comparison_messages(packed))
        <= budget
    )


def test_comparison_budget_rejects_oversized_current_trajectories():
    current = [
        {
            "trajectory_uri": f"trajectory:{index}",
            "outcome": "failure",
            "content": "x" * 4_000,
        }
        for index in range(2)
    ]

    with pytest.raises(ValueError, match="Current Trajectories exceed"):
        _pack_trajectory_comparison(
            case={},
            target_uri="experience",
            records=current,
            current_trajectory_uris={"trajectory:0", "trajectory:1"},
            char_limit=1_000,
        )


def test_comparison_budget_counts_the_rendered_message_headers_and_unicode_text():
    arguments = {
        "case": {"name": "案例"},
        "target_uri": "experience",
        "records": [
            {"trajectory_uri": "trajectory:0", "outcome": "success", "content": "规则\n步骤"}
        ],
        "current_trajectory_uris": {"trajectory:0"},
    }
    packed = _pack_trajectory_comparison(**arguments, char_limit=10_000)
    messages = _trajectory_comparison_messages(packed)
    size = sum(len(message["content"]) for message in messages)
    assert _pack_trajectory_comparison(**arguments, char_limit=size) == packed
    with pytest.raises(ValueError, match="Current Trajectories exceed"):
        _pack_trajectory_comparison(**arguments, char_limit=size - 1)


@pytest.mark.asyncio
async def test_prefetch_does_not_promote_successful_communication_value_to_literal_requirement():
    uri = "viking://user/u/memories/experiences/case_a.md"
    trajectory = Trajectory(
        name="successful",
        uri="viking://user/u/memories/trajectories/case_a/successful.md",
        outcome="success",
        content="# Session Context\n\n# Trajectory\n\n1. `assistant` communicated total 1628\n",
        metadata={
            "case_name": "case_a",
            "source_session_uri": "viking://session/s/archives/1",
            "evaluation": {"passed": True, "score": 1.0},
            "evaluation_criteria": [
                {
                    "id": "evaluation:metadata.result.communicate_checks[0].met",
                    "kind": "criterion",
                    "passed": True,
                    "summary": '{"info":"1628","met":true}',
                }
            ],
            "experience_execution": {},
        },
    )
    provider = AgentExperienceContextProvider(
        messages=[],
        case=Case("case_a", "report", {}, Rubric("r", "", [])),
        target_uri=uri,
        source_session_uri="viking://session/s/archives/1",
        trajectories=[trajectory],
    )
    provider._eval_memory = AsyncMock(return_value={"experience": {"error": "File not found"}})

    messages = provider.format_prefetch_messages(await provider.prefetch())
    record = metadata(messages[1])

    assert "must_preserve_success_facts" not in record
    assert record["evaluation_criteria"][0]["summary"] == ('{"info":"1628","met":true}')


@pytest.mark.asyncio
async def test_prefetch_keeps_each_sessions_rules_with_its_own_dialogue():
    trajectories = [
        Trajectory(
            name=str(index),
            uri=f"viking://user/u/memories/trajectories/case_a/{index}.md",
            outcome="success" if index == 0 else "failure",
            content=f"# Session Context\n\n{rule}\n\n# Trajectory\n\n1. `user` cancel duplicate\n",
            metadata={
                "case_name": "case_a",
                "source_session_uri": f"viking://session/s{index}/archives/1",
            },
        )
        for index, rule in enumerate(
            ["Cancellation needs confirmation.", "This workflow requires supervisor approval."]
        )
    ]
    provider = AgentExperienceContextProvider(
        messages=[],
        case=Case("case_a", "cancel", {}, Rubric("r", "", [])),
        target_uri="viking://user/u/memories/experiences/case_a.md",
        source_session_uri=trajectories[0].metadata["source_session_uri"],
        trajectories=trajectories,
    )
    provider._eval_memory = AsyncMock(return_value={"experience": {"error": "File not found"}})

    messages = provider.format_prefetch_messages(await provider.prefetch())

    assert len(messages) == 5
    assert all(message["role"] == "user" for message in messages)
    for message, trajectory in zip(messages[1:3], trajectories, strict=True):
        assert message["content"].endswith(trajectory.content)
        assert metadata(message)["source_session_uri"] == trajectory.metadata["source_session_uri"]
        assert "experience_execution" in metadata(message)
