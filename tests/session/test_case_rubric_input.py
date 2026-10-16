from __future__ import annotations

import json
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openviking.message import Message, TextPart
from openviking.session.compressor_v3 import (
    SessionCompressorV3,
    _case_rubric_model_evidence,
    _training_case_from_first_message,
    _training_evaluation_from_messages,
)
from openviking.session.train import (
    Case,
    CriterionResult,
    Rollout,
    Rubric,
    RubricCriterion,
    RubricEvaluation,
)
from openviking.session.train.components.session_commit import (
    _case_spec_message_to_request,
    _evaluation_message_to_request,
)


def envelope(request):
    return Message(
        id="control", role=request["role"], parts=[TextPart(text=request["parts"][0]["text"])]
    )


@pytest.mark.parametrize("name", ["tau2_airline_train_5", "support_suite_question_42"])
def test_projection_removes_control_identity_and_preserves_business_and_rubric_evidence(name):
    case = Case(
        name=name,
        task_signature=f"{name}:signature:trial:7",
        input={
            "domain": "reservation_service",
            "user_query": "Cancel reservation XEHM4B after confirmation; report its refund.",
            "split": "identity_only_split",
            "data_split": "identity_only_dataset",
            "task_id": "identity_only_task_id",
            "task_no": 4278,
        },
        rubric=Rubric(
            f"{name}_rubric",
            "Confirm before changing reservations.",
            [
                RubricCriterion(
                    f"{name}_criterion", "The authorized change was performed.", True, 0.75
                )
            ],
        ),
        metadata={"train_trial": 7, "original_case_name": name, "private_note": "control_only"},
    )
    evaluation = RubricEvaluation(
        False,
        0.0,
        [
            CriterionResult(
                f"{name}_criterion",
                False,
                0.0,
                ["Reservation was not cancelled."],
                ["The booking is still confirmed."],
                {"reward": 0.0},
            )
        ],
        ["The requested change was not completed."],
        metadata={"custom_inspection": {"reservation_id": "XEHM4B", "verdict": "still confirmed"}},
    )
    originals = asdict(case), asdict(evaluation)
    rollout = Rollout(case, [], "snapshot", evaluation)
    controls = [
        envelope(_case_spec_message_to_request(rollout)),
        envelope(_evaluation_message_to_request(rollout)),
    ]
    raw_before = [asdict(m) for m in controls]
    parsed_case = _training_case_from_first_message(controls)
    parsed_evaluation = _training_evaluation_from_messages(controls)
    documents = _case_rubric_model_evidence(parsed_case, parsed_evaluation)
    assert len(documents) == 2
    text = "\n".join(documents)
    for identifier in (
        name,
        "identity_only_split",
        "identity_only_dataset",
        "identity_only_task_id",
        "4278",
        "control_only",
    ):
        assert identifier not in text
    assert "Batch Training CaseSpec" not in text and '"protocol"' not in text
    requirements, report = map(json.loads, documents)
    assert requirements == {
        "task_context": {"domain": case.input["domain"], "user_query": case.input["user_query"]},
        "rubric": {
            "description": case.rubric.description,
            "criteria": [
                {
                    "description": case.rubric.criteria[0].description,
                    "required": True,
                    "weight": 0.75,
                }
            ],
        },
    }
    expected_report = asdict(evaluation)
    expected_report["criterion_results"][0].pop("criterion_name")
    expected_report["criterion_results"][0]["description"] = case.rubric.criteria[0].description
    assert report["external_evaluation"] == expected_report
    assert "experience_execution" not in text
    assert parsed_case.name == name  # Internal tracing/task transport is not rewritten.
    assert [asdict(m) for m in controls] == raw_before
    assert (asdict(case), asdict(evaluation)) == originals


def test_projection_does_not_scrub_identifiers_from_actual_business_text():
    text = "Please investigate the job named tau2_airline_train_5; do not rename it."
    case = Case("control_name", "control_signature", {"user_query": text}, Rubric("r", "", []))
    documents = _case_rubric_model_evidence(case, None)
    assert json.loads(documents[0])["task_context"]["user_query"] == text
    assert "control_name" not in documents[0]
    assert _case_rubric_model_evidence(None, None) == []


def test_evaluation_without_task_retains_results_without_inventing_criterion_descriptions():
    evaluation = RubricEvaluation(
        False,
        0.0,
        [
            CriterionResult("private_task_label", False, 0.0, ["Incomplete"], ["Record unchanged"]),
        ],
        [],
    )
    report = json.loads(_case_rubric_model_evidence(None, evaluation)[0])["external_evaluation"]
    assert report["criterion_results"] == [
        {
            "passed": False,
            "score": 0.0,
            "feedback": ["Incomplete"],
            "evidence": ["Record unchanged"],
            "metadata": {},
        }
    ]
    assert evaluation.criterion_results[0].criterion_name == "private_task_label"


@pytest.mark.asyncio
async def test_disabled_case_extraction_does_not_parse_or_forward_training_evidence():
    from openviking.server.identity import RequestContext, Role
    from openviking_cli.session.user_id import UserIdentifier

    ctx = RequestContext(user=UserIdentifier.the_default_user("u"), role=Role.ROOT)

    compressor = SessionCompressorV3(vikingdb=None, rollout_analyzer=SimpleNamespace())
    compressor._extract_user_memories = AsyncMock(
        return_value=SimpleNamespace(
            contexts=[],
            cases=[],
            memory_diff=None,
            case_uri_by_name={},
        )
    )
    compressor._write_final_memory_diff = AsyncMock()
    compressor._session_skill_extraction_enabled = lambda: False
    message = Message(
        id="user", role="user", parts=[TextPart(text="Please remember my preference.")]
    )
    malformed = Message(
        id="control",
        role="system",
        parts=[TextPart(text="# OpenViking OutcomeEvaluation\ninvalid JSON")],
    )
    await compressor.extract_long_term_memories(
        messages=[message, malformed],
        ctx=ctx,
        allowed_memory_types={"preferences"},
        strict_extract_errors=True,
    )
    kwargs = compressor._extract_user_memories.await_args.kwargs
    assert kwargs["messages"] == [message]
    assert kwargs["case_rubric_evidence"] == []
