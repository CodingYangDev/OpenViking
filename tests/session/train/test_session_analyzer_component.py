from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_fakes import fake_request_context

from openviking.message import Message, TextPart
from openviking.session.train import Case, Rollout, Rubric, RubricEvaluation
from openviking.session.train.components.session_analyzer import (
    SessionAnalyzerContext,
    SessionRolloutAnalyzer,
)


@pytest.fixture
def analyzer_setup():
    from test_case_experience_pipeline import ObservationFS, trajectory_program

    fs = ObservationFS()
    model = SimpleNamespace(
        model="test-vlm", get_completion_async=AsyncMock(return_value=trajectory_program())
    )
    context = SessionAnalyzerContext(
        fake_request_context(), source_archive_uri="viking://session/s/archives/1"
    )
    return fs, model, SessionRolloutAnalyzer(viking_fs=fs, vlm=model), context


@pytest.mark.asyncio
async def test_session_analysis_persists_trajectory_and_preserves_raw_evidence(analyzer_setup):
    from copy import deepcopy

    fs, model, analyzer, context = analyzer_setup
    rollout = Rollout(
        Case("case_a", "cancel", {}, Rubric("r", "", [])),
        [Message(id="m", role="user", parts=[TextPart(text="cancel")])],
        "p0",
        metadata={"experience_execution": {"exp": {"executed_nodes": ["identify"]}}},
    )
    messages = deepcopy(rollout.messages)
    result = await analyzer.analyze(rollout, context)
    assert result.rollout is rollout
    assert result.evaluation is None
    assert result.rollout.messages == messages
    assert len(result.trajectories) == 1
    assert result.trajectories[0].outcome == "unknown"
    assert result.trajectories[0].uri in fs.files
    assert result.metadata["trajectory_memory"].uri == result.trajectories[0].uri
    assert result.metadata["experience_execution"]["exp"]["executed_nodes"] == ["identify"]
    assert result.metadata["source_session_uri"] == context.source_archive_uri
    before = dict(fs.files)
    cached = await analyzer.analyze(rollout, context)
    assert cached.trajectories[0].uri == result.trajectories[0].uri
    assert fs.files == before
    model.get_completion_async.assert_awaited_once()


@pytest.mark.asyncio
async def test_session_analysis_uses_explicit_evaluation(analyzer_setup):
    _fs, _model, analyzer, context = analyzer_setup
    evaluator = SimpleNamespace(evaluate=AsyncMock())
    analyzer.evaluator = evaluator
    evaluation = RubricEvaluation(False, 0.0, [], ["not cancelled"])
    rollout = Rollout(Case("c", "cancel", {}, Rubric("r", "", [])), [], "p", evaluation)
    result = await analyzer.analyze(rollout, context)
    assert result.evaluation is evaluation
    assert result.trajectories[0].outcome == "failure"
    assert result.trajectories[0].metadata["evaluation"] == {"passed": False, "score": 0.0}
    evaluator.evaluate.assert_not_awaited()


@pytest.mark.asyncio
async def test_evaluation_is_completed_before_extracting_trajectory(analyzer_setup):
    _fs, _model, analyzer, context = analyzer_setup
    evaluation = RubricEvaluation(True, 1.0, [], [])
    evaluator = SimpleNamespace(evaluate=AsyncMock(return_value=evaluation))
    analyzer.evaluator = evaluator
    rollout = Rollout(Case("c", "cancel", {}, Rubric("r", "", [])), [], "p")
    result = await analyzer.analyze(rollout, context)
    assert result.evaluation is evaluation
    assert result.trajectories[0].outcome == "success"
    evaluator.evaluate.assert_awaited_once_with(rollout, context.evaluator_context)


@pytest.mark.asyncio
async def test_analysis_requires_source_archive_before_model_call(analyzer_setup):
    _fs, model, analyzer, context = analyzer_setup
    context.source_archive_uri = ""
    rollout = Rollout(Case("c", "cancel", {}, Rubric("r", "", [])), [], "p")
    with pytest.raises(ValueError, match="source Session archive"):
        await analyzer.analyze(rollout, context)
    model.get_completion_async.assert_not_awaited()


@pytest.mark.asyncio
async def test_trajectory_read_errors_are_not_treated_as_missing(analyzer_setup):
    fs, model, analyzer, context = analyzer_setup
    fs.read_file = AsyncMock(side_effect=PermissionError("denied"))
    rollout = Rollout(Case("c", "cancel", {}, Rubric("r", "", [])), [], "p")
    with pytest.raises(PermissionError, match="denied"):
        await analyzer.analyze(rollout, context)
    model.get_completion_async.assert_not_awaited()


@pytest.mark.asyncio
async def test_analysis_preserves_optional_skill_proposals(analyzer_setup, monkeypatch):
    _fs, model, analyzer, context = analyzer_setup
    skills = [SimpleNamespace(name="skill")]
    extract_skills = AsyncMock(return_value={"skill_gradients": skills})
    monkeypatch.setattr(SessionRolloutAnalyzer, "extract_session_skills", extract_skills)
    context.include_session_skills = True
    rollout = Rollout(Case("c", "cancel", {}, Rubric("r", "", [])), [], "p")
    result = await analyzer.analyze(rollout, context)
    assert result.gradients == skills
    assert len(result.trajectories) == 1
    extract_skills.assert_awaited_once()
    model.get_completion_async.assert_awaited_once()


@pytest.mark.asyncio
async def test_analysis_uses_context_memory_registry(analyzer_setup):
    from openviking.session.memory.memory_type_registry import MemoryTypeRegistry

    _fs, model, analyzer, context = analyzer_setup
    context.memory_registry = MemoryTypeRegistry()
    schema = context.memory_registry.get("trajectories")
    content_field = next(field for field in schema.fields if field.name == "steps")
    content_field.description += "\nACCOUNT_SPECIFIC_TRAJECTORY_RULE"
    rollout = Rollout(Case("c", "cancel", {}, Rubric("r", "", [])), [], "p")
    result = await analyzer.analyze(rollout, context)
    assert len(result.trajectories) == 1
    request = model.get_completion_async.await_args.kwargs["messages"]
    assert "ACCOUNT_SPECIFIC_TRAJECTORY_RULE" in request[0]["content"]
    prompt = " ".join(request[0]["content"].split())
    assert "not the Case's entire business scope" in prompt
    assert "retain enough candidate values to check the choice" in prompt
    model.get_completion_async.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelled_analysis_does_not_write_partial_trajectory(analyzer_setup):
    fs, model, analyzer, context = analyzer_setup
    started = asyncio.Event()

    async def wait_for_model(**kwargs):
        started.set()
        await asyncio.Event().wait()

    model.get_completion_async.side_effect = wait_for_model
    rollout = Rollout(Case("c", "cancel", {}, Rubric("r", "", [])), [], "p")
    before = dict(fs.files)
    task = asyncio.create_task(analyzer.analyze(rollout, context))
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fs.files == before


@pytest.mark.asyncio
async def test_optional_skill_extraction_exposes_only_skill_schema(monkeypatch):
    from openviking.server.identity import RequestContext, Role
    from openviking.session.memory.dataclass import ResolvedOperation, ResolvedOperations
    from openviking.session.train.components import session_analyzer as module
    from openviking_cli.session.user_id import UserIdentifier

    observed = []

    class Loop:
        def __init__(self, **kwargs):
            observed.append(kwargs)

        async def run(self):
            return ResolvedOperations(
                upsert_operations=[
                    ResolvedOperation(
                        memory_type="session_skills",
                        uris=["viking://user/u/skills/check/SKILL.md"],
                        memory_fields={"skill_name": "check", "content": "Check results"},
                    )
                ],
                delete_file_contents=[],
                errors=[],
            ), []

    monkeypatch.setattr(module, "ExtractLoop", Loop)
    ctx = RequestContext(user=UserIdentifier("default", "u"), role=Role.ROOT)
    fs = SimpleNamespace(write_file=AsyncMock())
    result = await SessionRolloutAnalyzer(
        viking_fs=fs, vlm=SimpleNamespace()
    ).extract_session_skills(messages=[], ctx=ctx)
    assert [s.memory_type for s in observed[0]["context_provider"].get_memory_schemas(ctx)] == [
        "session_skills"
    ]
    assert result["skill_gradients"][0].after_file.memory_type == "skills"
    fs.write_file.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("external_passed", [None, True, False])
async def test_trajectory_snapshots_llm_rubric_results_without_rewriting_external_score(
    analyzer_setup, external_passed
):
    import json
    from dataclasses import asdict

    import yaml

    from openviking.message import ToolPart
    from openviking.session.memory.experience.experience_context import _trajectory_model_content
    from openviking.session.memory.utils.memory_file_utils import MemoryFileUtils
    from openviking.session.train import RubricCriterion

    fs, model, analyzer, context = analyzer_setup
    definition = RubricCriterion("confirm", "Confirm changes before execution.", True, 1.0)
    rubric = Rubric(
        "domain",
        "",
        [
            definition,
            RubricCriterion("other", "Check another applicable requirement.", True, 1.0),
        ],
    )
    judgments = [
        {
            "id": "confirm",
            "passed": False,
            "evidence": 'Step 2: update_booking returned "updated" without confirmation.\n'
            "No earlier user confirmation is recorded.",
        },
        {"id": "other", "passed": None, "evidence": "Not applicable to this request."},
    ]
    model.get_completion_async.return_value = r"""sdk.create_trajectories(
        context_rules='',
        evaluation_criteria=r'''
- id: confirm
  passed: false
  evidence: |-
    Step 2: update_booking returned "updated" without confirmation.
    No earlier user confirmation is recorded.
- id: other
  passed: null
  evidence: Not applicable to this request.
''', steps='1. Observed execution.')
sdk.commit()"""
    evaluation = (
        None
        if external_passed is None
        else RubricEvaluation(
            external_passed,
            float(external_passed),
            [],
            [],
            metadata={"custom_verdict": {"state": "reported", "details": "keep verbatim"}},
        )
    )
    task_spec = (
        None
        if external_passed is None
        else {"name": "task", "input": {"query": "private task-only input"}}
    )
    rollout = Rollout(
        Case("library", "library", {}, rubric),
        [
            Message(
                id="m",
                role="user",
                parts=[TextPart(text="Change booking A; ask me before you update it.")],
            ),
            Message(
                id="update",
                role="assistant",
                parts=[
                    ToolPart(
                        tool_id="update_a",
                        tool_name="update_booking",
                        tool_input={"id": "A"},
                        tool_output="updated",
                        tool_status="completed",
                    )
                ],
            ),
        ],
        "p",
        evaluation,
        metadata={"task_spec": task_spec},
    )
    result = await analyzer.analyze(rollout, context)
    trajectory = result.trajectories[0]
    payload = json.loads(model.get_completion_async.await_args.kwargs["messages"][1]["content"])
    instruction = model.get_completion_async.await_args.kwargs["messages"][0]["content"]
    assert "Preserve explicitly reported results for matching criteria" in instruction
    assert "Use null for insufficient evidence or inapplicable criteria" in instruction
    assert "External evaluation is optional" in instruction
    assert "No precomputed rubric results are required" in instruction
    assert "evaluation_criteria=r'''<YAML list>'''" in instruction
    assert "evidence: |" in instruction
    assert payload["case"]["rubric"][0] == {"id": "confirm", "description": definition.description}
    assert payload["external_evaluation"] == (asdict(evaluation) if evaluation else None)
    assert payload["task_spec"] == task_spec
    assert "evaluation_criteria" not in payload
    assert any("update_booking" in step["content"] for step in payload["source_steps"])
    assert result.evaluation is evaluation
    snapshots = trajectory.metadata["evaluation_criteria"]
    assert snapshots[0] == {**judgments[0], "description": definition.description}
    assert snapshots[1]["passed"] is None
    expected = (
        None if evaluation is None else {"passed": external_passed, "score": float(external_passed)}
    )
    assert trajectory.metadata.get("evaluation") == expected
    if evaluation is None:
        assert trajectory.outcome == "unknown"
    stored = MemoryFileUtils.read(fs.files[trajectory.uri], uri=trajectory.uri)
    assert stored.extra_fields["evaluation_criteria"] == snapshots
    assert stored.extra_fields.get("task_spec") == task_spec
    header = yaml.safe_load(stored.content.split("```yaml\n", 1)[1].split("\n```", 1)[0])
    assert header["evaluation_criteria"] == [snapshots[0]]
    assert "task_spec" not in header
    assert "evaluation" not in header
    assert "private task-only input" not in stored.content
    view = yaml.safe_load(
        _trajectory_model_content(stored.content).split("```yaml\n", 1)[1].split("\n```", 1)[0]
    )
    assert view["evaluation_criteria"] == [snapshots[0]]
    original_description = definition.description
    definition.description = "Changed later"
    rubric.criteria.clear()
    # Reusing an immutable archive must not evaluate again against the changed/deleted rubric.
    cached = await analyzer.analyze(rollout, context)
    assert (
        cached.trajectories[0].metadata["evaluation_criteria"][0]["description"]
        == original_description
    )
    assert fs.files[trajectory.uri] == MemoryFileUtils.write(stored)
    model.get_completion_async.assert_awaited_once()
