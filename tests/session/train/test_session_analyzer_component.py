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
    content_field = next(field for field in schema.fields if field.name == "content")
    content_field.description += "\nACCOUNT_SPECIFIC_TRAJECTORY_RULE"
    rollout = Rollout(Case("c", "cancel", {}, Rubric("r", "", [])), [], "p")
    result = await analyzer.analyze(rollout, context)
    assert len(result.trajectories) == 1
    request = model.get_completion_async.await_args.kwargs["messages"]
    assert "ACCOUNT_SPECIFIC_TRAJECTORY_RULE" in request[0]["content"]


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
