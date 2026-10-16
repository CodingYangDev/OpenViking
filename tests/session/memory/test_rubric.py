from __future__ import annotations

import copy
import json
from dataclasses import asdict

import pytest

from openviking.session.memory.experience.rubric import (
    normalize_case_rubric,
    parse_case_rubric,
    snapshot_rubric_results,
)
from openviking.session.memory.experience.trajectory_context import AgentTrajectoryContextProvider
from openviking.session.train import Case, Rubric, RubricCriterion

RUBRIC = [
    {"id": "confirm", "description": "Confirm changes before executing them."},
    {"id": "scope", "description": "Perform only the requested changes."},
]


@pytest.mark.parametrize("as_text", [False, True])
def test_rubric_roundtrip(as_text):
    value = json.dumps(RUBRIC) if as_text else RUBRIC
    assert parse_case_rubric(normalize_case_rubric(value)) == RUBRIC
    assert normalize_case_rubric(None) == "[]"


@pytest.mark.parametrize(
    "value",
    [
        "plain text",
        "{}",
        '[{"id":"a"}]',
        [{"id": "a", "description": ""}],
        [{"id": False, "description": "rule"}],
        [{"id": "a", "description": "rule", "score": 1}],
        [RUBRIC[0], RUBRIC[0]],
    ],
)
def test_invalid_rubric_is_rejected(value):
    with pytest.raises(ValueError):
        parse_case_rubric(value)


@pytest.mark.parametrize("passed", [True, False, None])
def test_results_freeze_complete_definitions_in_case_order(passed):
    definitions = [dict(item) for item in RUBRIC]
    result = snapshot_rubric_results(
        [
            {"id": "scope", "passed": None, "evidence": "Not applicable to this request."},
            {"id": "confirm", "passed": passed, "evidence": "Observed confirmation evidence."},
        ],
        definitions,
    )
    assert result == [
        {**RUBRIC[0], "passed": passed, "evidence": "Observed confirmation evidence."},
        {**RUBRIC[1], "passed": None, "evidence": "Not applicable to this request."},
    ]
    definitions[0]["description"] = "changed"
    definitions.clear()
    assert result[0]["description"] == RUBRIC[0]["description"]


@pytest.mark.parametrize(
    "value",
    [
        [],
        None,
        False,
        0,
        {},
        "not JSON",
        '[{"id":"confirm","passed":true,"evidence":"{"nested":true}"}]',
        [None, False, "not an assessment"],
        [{"id": [], "passed": False, "evidence": "observed"}],
        [{"id": "missing", "passed": False, "evidence": "observed"}],
        [{"id": "confirm", "passed": "false", "evidence": "observed"}],
        [{"id": "confirm", "passed": 0, "evidence": "observed"}],
        [{"id": "confirm", "evidence": "observed"}],
        [{"id": "confirm", "passed": None, "evidence": ""}],
        [{"id": "confirm", "passed": False, "evidence": "   "}],
        [{"id": "confirm", "passed": False, "evidence": {"raw": "report"}}],
        [{"id": "confirm", "passed": False, "evidence": "observed"}] * 2,
    ],
)
def test_unusable_results_are_omitted_without_inventing_judgments(value):
    assert snapshot_rubric_results(value, RUBRIC) == []


@pytest.mark.parametrize("as_text", [False, True])
def test_partial_results_keep_only_valid_case_bound_snapshots(as_text):
    value = [
        {
            "id": "scope",
            "description": "model-authored definition",
            "passed": True,
            "evidence": "Observed scope.",
        },
        {"id": "tau2_reward", "passed": False, "evidence": "External task judgment."},
        {"id": "confirm", "passed": "true", "evidence": "Not a boolean."},
    ]
    original = copy.deepcopy(value)
    assert snapshot_rubric_results(json.dumps(value) if as_text else value, RUBRIC) == [
        {**RUBRIC[1], "passed": True, "evidence": "Observed scope."}
    ]
    assert value == original


@pytest.mark.parametrize(
    "passed_values", [(True, False), (True, "false"), ("true", False), (True, False, None)]
)
def test_duplicate_ids_are_all_omitted_while_other_results_survive(passed_values):
    value = [
        {"id": "confirm", "passed": passed, "evidence": "Conflicting or invalid judgment."}
        for passed in passed_values
    ]
    value.append({"id": "scope", "passed": None, "evidence": "Not applicable."})
    assert snapshot_rubric_results(value, RUBRIC) == [
        {**RUBRIC[1], "passed": None, "evidence": "Not applicable."}
    ]


def test_discarded_results_are_logged_without_logging_the_payload(monkeypatch):
    messages = []
    monkeypatch.setattr(
        "openviking.session.memory.experience.rubric.logger.warning",
        lambda message, *args: messages.append(message % args),
    )
    assert snapshot_rubric_results("private malformed assessment", RUBRIC) == []
    assert "Discarding unparseable evaluation_criteria JSON" in messages[0]
    assert "private malformed assessment" not in messages[0]
    assert snapshot_rubric_results([{"id": "unknown"}], RUBRIC) == []
    assert "Discarded 1 of 1 evaluation_criteria entries" in messages[1]


def test_excessively_nested_result_json_is_omitted():
    import sys

    depth = sys.getrecursionlimit() + 10
    assert snapshot_rubric_results("[" * depth + "0" + "]" * depth, RUBRIC) == []


def test_dropping_bad_rubric_results_does_not_bypass_experience_effect_validation():
    from test_trajectory_context import operations

    provider = AgentTrajectoryContextProvider(messages=[], source_steps=[])
    result = operations("Factual dialogue.")
    result.upsert_operations[0].memory_fields.update(
        evaluation_criteria="not JSON",
        experience_effects={"positive_ids": ["not_injected"], "negative_ids": [], "weak_ids": []},
    )
    errors = provider.validate_resolved_operations(result)
    assert errors == [
        {"error": "experience_effects.positive_ids contains an ID that was not injected"}
    ]


@pytest.mark.asyncio
async def test_extraction_receives_case_rubric_and_arbitrary_external_evidence():
    from test_trajectory_context import operations

    case = Case(
        "library",
        "library",
        {},
        Rubric(
            "r",
            "",
            [RubricCriterion(item["id"], item["description"], True, 1.0) for item in RUBRIC],
        ),
    )
    # No passed/met/action_match/db_match keys in this evaluator-specific evidence.
    external = {"inspection": {"authorization": "absent", "affected_records": ["A"]}}
    provider = AgentTrajectoryContextProvider(
        messages=[],
        source_steps=[],
        case=asdict(case),
        external_evaluation=external,
    )
    payload = json.loads((await provider.prefetch())[0]["content"])
    assert payload["case"]["rubric"] == RUBRIC
    assert payload["external_evaluation"] == external
    result = operations("Recorded dialogue.")
    result.upsert_operations[0].memory_fields["evaluation_criteria"] = json.dumps(
        [
            {
                "id": "confirm",
                "passed": False,
                "evidence": "External report: authorization absent.",
            },
            {"id": "scope", "passed": None, "evidence": "Requested scope not recorded."},
        ]
    )
    assert provider.validate_resolved_operations(result) == []
    snapshots = result.upsert_operations[0].memory_fields["evaluation_criteria"]
    assert snapshots[0] == {
        **RUBRIC[0],
        "passed": False,
        "evidence": "External report: authorization absent.",
    }
    assert snapshots[1]["passed"] is None
    assert result.upsert_operations[0].memory_fields["content"] == "Recorded dialogue."


@pytest.mark.asyncio
@pytest.mark.parametrize("allow_cases", [True, False])
async def test_case_evidence_is_separate_from_conversation_and_respects_schema_filter(allow_cases):
    from unittest.mock import AsyncMock

    from test_memory_create_guard import CTX

    from openviking.message import Message, TextPart
    from openviking.session.memory.memory_isolation_handler import MemoryIsolationHandler
    from openviking.session.memory.memory_type_registry import (
        MemoryTypeRegistry,
        resolve_memory_templates_dir,
    )
    from openviking.session.memory.session_extract_context_provider import (
        SessionExtractContextProvider,
    )

    registry = MemoryTypeRegistry(load_schemas=False)
    registry.load_from_yaml(str(resolve_memory_templates_dir() / "cases.yaml"))
    source = [Message(id="user", role="user", parts=[TextPart(text="Cancel the booking.")])]
    reports = ["# OpenViking OutcomeEvaluation\nExternal requirement and feedback"]
    provider = SessionExtractContextProvider(
        messages=source,
        ctx=CTX,
        memory_registry=registry,
        case_rubric_evidence=reports,
    )
    provider._isolation_handler = MemoryIsolationHandler(
        CTX,
        provider.get_extract_context(),
        allowed_memory_types={"cases"} if allow_cases else set(),
    )
    provider._eval_memory = AsyncMock(return_value={})
    messages = await provider.prefetch()
    assert "Cancel the booking." in messages[0]["content"]
    assert reports[0] not in messages[0]["content"]
    evidence_messages = [
        m for m in messages if (m.get("content") or "").startswith("# Case Rubric Evidence")
    ]
    assert len(evidence_messages) == int(allow_cases)
    if allow_cases:
        assert "external evaluation reports" in evidence_messages[0]["content"]
        assert evidence_messages[0]["content"].endswith(reports[0])
    assert provider.messages == source
