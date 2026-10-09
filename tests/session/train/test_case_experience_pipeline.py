from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml
from test_gradient_estimator_component import ROOT, analysis, operations
from test_train_components import FakeVikingFS

from openviking.message import Message, TextPart, ToolPart
from openviking.server.identity import RequestContext, Role
from openviking.session.compressor_v3 import SessionCompressorV3
from openviking.session.memory.dataclass import MemoryFile
from openviking.session.memory.experience.experience_context import AgentExperienceContextProvider
from openviking.session.memory.experience.trajectory_context import trajectory_source_steps
from openviking.session.memory.memory_type_registry import get_default_registry
from openviking.session.memory.memory_updater import MemoryUpdater
from openviking.session.memory.utils.memory_file_utils import MemoryFileUtils
from openviking.session.train import (
    ExperienceGradientContext,
    ExperienceGradientEstimator,
    ExperienceSet,
    MemoryFilePolicyUpdater,
    PatchMergePolicyOptimizerContext,
    PipelineContext,
    SessionAnalyzerContext,
    SessionRolloutAnalyzer,
    StreamingPolicyTrainer,
    StreamingPolicyTrainerConfig,
)
from openviking.session.train.components.trajectory_observation import (
    ObservationGradient,
    observation_evidence,
    trajectory_record,
    trajectory_source_session_uri,
)
from openviking.session.train.components.trajectory_optimizer import TrajectoryExperienceOptimizer
from openviking_cli.session.user_id import UserIdentifier

OBSERVATION = "1. `user` requested cancellation of the duplicate booking"
STORED_OBSERVATION = "# Session Context\n\n\n\n# Trajectory\n\n1. `user` requested cancellation of the duplicate booking"


def trajectory_program(content: str = OBSERVATION, *, context_rules: str = "") -> str:
    content = f"# Session Context\n\n{context_rules}\n\n# Trajectory\n\n{content}"
    return (
        "sdk.create_trajectories("
        "trajectory_name='server_fixed', case_name='server_fixed', outcome='unknown', "
        f"content={content!r})\n"
        "sdk.commit()"
    )


async def presented_trajectories(provider):
    messages = provider.format_prefetch_messages(await provider.prefetch())
    records = []
    for message in messages:
        if message["content"].startswith("# Trajectory Record\n"):
            fields, content = message["content"].split("```yaml\n", 1)[1].split("\n```\n\n", 1)
            records.append({**yaml.safe_load(fields), "content": content})
    return records


class ObservationFS(FakeVikingFS):
    def __init__(self):
        super().__init__({})
        self.locks = {}
        self.policy_lock = asyncio.Lock()
        self._async_agfs.pathlock_acquire_exact = self.acquire
        self._async_agfs.pathlock_acquire_exact_batch = self.acquire_tree
        self._async_agfs.pathlock_acquire_tree = self.acquire_tree
        self._async_agfs.pathlock_release = self.release

    async def acquire(self, path, **kwargs):
        lock = self.locks.setdefault(path, asyncio.Lock())
        await lock.acquire()
        return lock

    async def acquire_tree(self, *args, **kwargs):
        await self.policy_lock.acquire()
        return self.policy_lock

    async def release(self, lock):
        lock.release()

    async def read_file(self, uri, ctx=None):
        if uri not in self.files:
            raise FileNotFoundError(uri)
        return self.files[uri]

    async def exists(self, uri, ctx=None):
        return uri in self.files


async def observe(analyzer, run, policies, context):
    """Persist via the public analyzer, then build the independent Phase 2 signal."""
    run.rollout.evaluation = run.evaluation
    run.rollout.metadata.update(run.metadata)
    result = await analyzer.analyze(
        run.rollout,
        SessionAnalyzerContext(
            request_context=context.request_context,
            strict_extract_errors=context.strict_extract_errors,
            source_archive_uri=run.metadata["source_session_uri"],
            memory_registry=context.memory_registry,
        ),
    )
    run.evaluation = result.evaluation
    run.trajectories = result.trajectories
    run.metadata.update(result.metadata)
    memory = result.metadata["trajectory_memory"]
    target = memory.extra_fields["experience_update"]["experience_uri"]
    assert target.startswith(policies.root_uri.rstrip("/") + "/")
    return [ObservationGradient(run, run.rollout.case.name, target, memory)]


@pytest.fixture
def setup_observation():
    fs = ObservationFS()
    ctx = RequestContext(user=UserIdentifier("default", "u"), role=Role.ROOT)
    model = SimpleNamespace(
        model="test-vlm",
        get_completion_async=AsyncMock(return_value=trajectory_program()),
    )
    analyzer = SessionRolloutAnalyzer(viking_fs=fs, vlm=model)
    context = ExperienceGradientContext(ctx, [])
    policies = ExperienceSet(ROOT, [], viking_fs=fs, request_context=ctx)
    return fs, ctx, model, analyzer, context, policies


@pytest.mark.asyncio
async def test_observation_is_factual_persisted_idempotent_and_server_owned(
    setup_observation, monkeypatch
):
    fs, ctx, model, analyzer, context, policies = setup_observation
    original_apply = MemoryUpdater.apply_operations
    apply_calls = []

    async def apply_operations(updater, operations, request_context, **kwargs):
        apply_calls.append(operations)
        return await original_apply(updater, operations, request_context, **kwargs)

    monkeypatch.setattr(MemoryUpdater, "apply_operations", apply_operations)
    run = analysis(passed=False)
    run.trajectories = []
    signals = await observe(analyzer, run, policies, context)
    record = run.trajectories[0]
    assert record.outcome == "failure"
    assert record.metadata["evaluation"]["passed"] is False
    assert record.metadata["source_session_uri"] == run.metadata["source_session_uri"]
    assert record.content == STORED_OBSERVATION
    assert "/trajectories/case_a/" in record.uri
    assert signals[0].target_uri == f"{ROOT}/case_a.md"
    stored = MemoryFileUtils.read(fs.files[record.uri], uri=record.uri)
    assert stored.links[0]["to_uri"] == run.metadata["source_session_uri"]
    assert trajectory_source_session_uri(stored) == run.metadata["source_session_uri"]
    assert stored.extra_fields.get("source_session_uri") is None
    assert stored.extra_fields.get("task_query") is None
    assert stored.extra_fields.get("evidence") is None
    assert stored.extra_fields["experience_update"]["state"] == "pending"
    assert stored.extra_fields["experience_update"]["attempts"] == 0
    assert stored.extra_fields["trajectory_format"] == "ordered_markdown_v2"
    assert "cited_evidence" not in stored.extra_fields
    assert any(link["to_uri"].endswith("/memories/cases/case_a.md") for link in stored.links)
    assert len(apply_calls) == 1
    assert apply_calls[0].upsert_operations[0].memory_type == "trajectories"
    assert apply_calls[0].delete_file_contents == []
    before = dict(fs.files)
    await observe(analyzer, run, policies, context)
    assert fs.files == before
    model.get_completion_async.assert_awaited_once()
    extraction_messages = model.get_completion_async.await_args.kwargs["messages"]
    assert "sdk.create_trajectories" in extraction_messages[0]["content"]
    assert "at most 800 characters" in extraction_messages[0]["content"]
    assert "source_steps" in extraction_messages[1]["content"]


def test_source_steps_preserve_user_assistant_and_tool_order():
    steps = trajectory_source_steps(
        [
            Message(id="u", role="user", parts=[TextPart(text="Cancel booking A")]),
            Message(id="a", role="assistant", parts=[TextPart(text="I will check it")]),
            Message(
                id="t",
                role="user",
                parts=[
                    ToolPart(
                        tool_id="tool",
                        tool_name="get_booking",
                        tool_input={"id": "A"},
                        tool_output='{"status":"active"}',
                        tool_status="completed",
                    )
                ],
            ),
        ]
    )

    assert [step["type"] for step in steps] == ["user", "assistant", "user", "tool:get_booking"]
    assert "Cancel booking A" in steps[0]["content"]
    assert "I will check it" in steps[1]["content"]
    assert steps[2]["content"] == ""
    assert json.loads(steps[3]["content"])["tool_output"] == '{"status":"active"}'


@pytest.mark.asyncio
async def test_selected_session_rules_persist_and_reach_experience_input(setup_observation):
    fs, _ctx, model, analyzer, context, policies = setup_observation
    rules = "Verify eligibility before cancelling.\nException: duplicate bookings qualify."
    unrelated = "For new bookings, collect the destination."
    run = analysis(passed=False)
    run.rollout.messages.insert(
        0,
        Message(
            id="system",
            role="user",
            parts=[TextPart(text=f"system:\n{rules}\n\n{unrelated}")],
        ),
    )
    model.get_completion_async.return_value = trajectory_program(context_rules=rules)

    await observe(analyzer, run, policies, context)

    trajectory = run.trajectories[0]
    stored = MemoryFileUtils.read(fs.files[trajectory.uri], uri=trajectory.uri)
    assert stored.content == (
        f"# Session Context\n\n{rules}\n\n"
        "# Trajectory\n\n1. `user` requested cancellation of the duplicate booking"
    )
    assert unrelated not in stored.content
    assert "context_rules" not in stored.extra_fields
    assert "session_context" not in stored.extra_fields
    model.get_completion_async.assert_awaited_once()
    extraction_messages = model.get_completion_async.await_args.kwargs["messages"]
    payload = json.loads(extraction_messages[1]["content"])
    assert "session_context" not in payload
    assert payload["source_steps"] == [
        {"type": "user", "content": f"system:\n{rules}\n\n{unrelated}"},
        {"type": "user", "content": "Cancel the duplicate booking."},
    ]
    assert "wrongly refused" in extraction_messages[0]["content"]
    assert "confirmation requirements and exceptions" in extraction_messages[0]["content"]

    provider = AgentExperienceContextProvider(
        messages=[],
        case=run.rollout.case,
        target_uri=f"{ROOT}/case_a.md",
        source_session_uri=run.metadata["source_session_uri"],
        trajectories=[trajectory],
    )
    provider._eval_memory = AsyncMock(return_value={"experience": {"error": "File not found"}})
    record = (await presented_trajectories(provider))[0]
    assert record["outcome"] == "failure"
    assert record["content"].rstrip() == stored.content
    assert "steps" not in record
    assert "context_rules" not in record
    assert record["experience_execution"] == trajectory.metadata["experience_execution"]
    assert "each run against its own rules" in provider.instruction()


@pytest.mark.asyncio
async def test_experience_request_has_separate_plain_user_messages_and_working_update_binding(
    setup_observation,
):
    fs, _ctx, model, _analyzer, context, policies = setup_observation
    target = f"{ROOT}/case_a.md"
    old_content = 'dag = workflow("cancel")\nreminder = tell("Verify the booking identity")'
    new_content = 'dag = workflow("cancel")\nreminder = tell("Verify identity and eligibility")'
    fs.files[target] = MemoryFileUtils.write(
        MemoryFile(
            uri=target,
            memory_type="experiences",
            content=old_content,
            extra_fields={"experience_name": "case_a", "version": 1},
        )
    )
    run = analysis(passed=False)
    other = analysis(session="viking://session/s2/archives/001", passed=True)
    run.trajectories.extend(other.trajectories)
    run.trajectories[
        0
    ].content = (
        "# Session Context\n\nOutput JSON only.\n\n# Trajectory\n\n1. `user` cancel duplicate\n"
    )
    run.trajectories[0].metadata["experience_execution"] = {
        "case_a": {"slot_values": {"booking": "duplicate"}, "executed_nodes": ["reminder"]}
    }
    model.get_completion_async.return_value = (
        f"experience.content.overwrite({new_content!r})\nsdk.commit()"
    )

    proposals = await ExperienceGradientEstimator(viking_fs=fs, vlm=model).estimate(
        run, policies, context
    )

    model.get_completion_async.assert_awaited_once()
    assert len(proposals) == 1
    assert proposals[0].after_file.content == new_content
    messages = model.get_completion_async.await_args.kwargs["messages"]
    assert messages[0]["role"] == "system"
    assert all(message["role"] == "user" for message in messages[1:])
    assert any(message["content"].startswith("# Case and Trajectory Batch") for message in messages)
    assert any(
        message["content"].startswith("Use exactly one SDK template") for message in messages
    )
    assert all(set(message) == {"role", "content"} for message in messages)
    records = [m for m in messages if m["content"].startswith("# Trajectory Record\n")]
    assert len(records) == 2
    for record, trajectory in zip(
        records, [other.trajectories[0], run.trajectories[0]], strict=True
    ):
        assert record["content"].endswith(trajectory.content)
    assert "Output JSON only." in records[1]["content"]
    assert "booking: duplicate" in records[1]["content"]
    existing = [
        message for message in messages if message["content"].startswith("# Existing Experience\n")
    ]
    assert len(existing) == 1
    assert existing[0]["content"].endswith(old_content)
    assert "1\tdag" not in existing[0]["content"]
    assert not any(m["content"].lstrip().startswith("{") for m in messages[1:])


@pytest.mark.asyncio
async def test_noncontiguous_related_rules_do_not_trigger_repair(setup_observation):
    fs, _ctx, model, analyzer, context, policies = setup_observation
    source = (
        "Checked bag allowance:\n"
        "- Regular members: 1 free checked bag for each economy passenger.\n"
        "- Silver members: 2 free checked bags for each economy passenger.\n"
        "- Gold members: 3 free checked bag for each economy passenger.\n"
        "- Each extra baggage is 50 dollars."
    )
    selected_rules = (
        "Checked bag allowance:\n"
        "- Gold members: 3 free checked bags for each economy passenger.\n"
        "- Each extra baggage is 50 dollars."
    )
    assert " ".join(selected_rules.split()) not in " ".join(source.split())
    run = analysis()
    run.rollout.messages = [
        Message(id="system", role="system", parts=[TextPart(text=source)]),
        Message(id="user", role="user", parts=[TextPart(text="Add one checked bag.")]),
    ]
    model.get_completion_async.return_value = trajectory_program(
        "1. Requested one additional checked bag.", context_rules=selected_rules
    )

    await observe(analyzer, run, policies, context)

    model.get_completion_async.assert_awaited_once()
    stored = MemoryFileUtils.read(fs.files[run.trajectories[0].uri])
    assert stored.content == (
        f"# Session Context\n\n{selected_rules}\n\n# Trajectory\n\n"
        "1. Requested one additional checked bag."
    )


@pytest.mark.asyncio
async def test_historical_success_accepts_unstructured_content(setup_observation):
    fs, ctx, model, analyzer, context, policies = setup_observation
    model.get_completion_async.return_value = (
        "sdk.create_trajectories(trajectory_name='model', case_name='model', outcome='unknown', "
        "content='The requested change succeeded.')\nsdk.commit()"
    )
    historical = analysis(session="viking://session/historical/archives/001", passed=True)
    await observe(analyzer, historical, policies, context)
    memory = historical.metadata["trajectory_memory"]
    memory.extra_fields["experience_update"]["state"] = "applied"
    fs.files[memory.uri] = MemoryFileUtils.write(memory)
    current = analysis(session="viking://session/current/archives/001", passed=False)
    signals = await observe(analyzer, current, policies, context)
    estimator = SimpleNamespace(estimate=AsyncMock(return_value=[]))

    await TrajectoryExperienceOptimizer(fs, estimator).plan(
        signals, policies, PatchMergePolicyOptimizerContext(ctx)
    )

    selected = estimator.estimate.await_args.args[0].trajectories
    assert [trajectory.uri for trajectory in selected] == [current.trajectories[0].uri, memory.uri]
    assert all(trajectory.content == "The requested change succeeded." for trajectory in selected)


@pytest.mark.asyncio
async def test_pending_scan_does_not_reinterpret_old_trajectory_format(
    setup_observation, monkeypatch
):
    fs, ctx, _model, analyzer, context, policies = setup_observation
    run = analysis()
    await observe(analyzer, run, policies, context)
    uri = run.trajectories[0].uri
    memory = MemoryFileUtils.read(fs.files[uri], uri=uri)
    memory.extra_fields["trajectory_format"] = "ordered_markdown_v1"
    memory.content = "# Trajectory\n\n1. `user` legacy observation"
    fs.files[uri] = MemoryFileUtils.write(memory)

    monkeypatch.setattr("openviking.session.compressor_v3.get_viking_fs", lambda: fs)
    memories = await SessionCompressorV3(vikingdb=None)._load_pending_trajectory_memories(
        viking_fs=fs,
        ctx=ctx,
        registry=get_default_registry(),
        trajectory_uris=[uri],
        case_names=None,
        retry_failed=False,
    )

    assert memories == []
    assert MemoryFileUtils.read(fs.files[uri]).content == memory.content


def test_source_steps_keep_every_record_within_shared_budget():
    messages = [
        Message(id=str(index), role="user", parts=[TextPart(text="x" * 2_000)])
        for index in range(100)
    ]

    steps = trajectory_source_steps(messages)

    assert len(steps) == 100
    assert all(step["type"] == "user" for step in steps)
    assert sum(len(step["content"]) for step in steps) <= 64_000


def test_experience_input_keeps_unstructured_content_and_inline_tool_json():
    trajectory = analysis().trajectories[0]
    trajectory.content = 'flights=[{"flight_number":"HAT072","date":"2024-05-19"}], payment_id=gift'
    assert trajectory_record(trajectory)["content"] == trajectory.content


@pytest.mark.asyncio
async def test_unknown_evaluation_and_same_archive_different_cases(setup_observation):
    fs, ctx, model, analyzer, context, policies = setup_observation
    first, second = analysis(passed=None), analysis(name="other", passed=None)
    await observe(analyzer, first, policies, context)
    await observe(analyzer, second, policies, context)
    assert first.trajectories[0].outcome == "unknown"
    assert first.trajectories[0].metadata.get("evaluation") is None
    assert first.trajectories[0].uri != second.trajectories[0].uri


def test_tool_facts_beyond_500_chars_are_retained_and_cited():
    output = json.dumps(
        {"flights": "x" * 700, "payment_history": [{"amount": 402}], "insurance": "yes"}
    )
    evidence = observation_evidence(
        [
            Message(
                id="m",
                role="user",
                parts=[
                    ToolPart(
                        tool_id="t",
                        tool_name="get_reservation_details",
                        tool_output=output,
                        tool_status="completed",
                    )
                ],
            )
        ],
        None,
    )
    assert "402" in evidence[0]["summary"]
    assert "insurance" in evidence[0]["summary"]
    assert json.loads(evidence[0]["summary"])["tool_output"] == output


@pytest.mark.asyncio
async def test_step_count_and_numbering_mismatch_are_kept_without_repair(setup_observation):
    fs, _ctx, model, analyzer, context, policies = setup_observation
    model.get_completion_async.return_value = trajectory_program("2. first\n5. second")

    run = analysis()
    await observe(analyzer, run, policies, context)
    await observe(analyzer, run, policies, context)

    model.get_completion_async.assert_awaited_once()
    stored = MemoryFileUtils.read(next(iter(fs.files.values())))
    assert stored.content.endswith("# Trajectory\n\n2. first\n5. second")


def test_criterion_evidence_keeps_required_info_without_justification_payload():
    from openviking.session.train import RubricEvaluation

    evaluation = RubricEvaluation(
        passed=True,
        score=1.0,
        criterion_results=[],
        feedback=[],
        metadata={
            "evaluation_result": {
                "communicate_checks": [
                    {
                        "info": "1628",
                        "justification": "very large evaluator explanation",
                        "met": True,
                    }
                ]
            }
        },
    )
    evidence = observation_evidence([], evaluation)
    criterion = next(item for item in evidence if ".communicate_checks[0].met" in item["id"])

    assert json.loads(criterion["summary"]) == {"info": "1628", "met": True}


@pytest.mark.asyncio
async def test_concurrent_sessions_batch_once_and_load_one_historical_success(
    setup_observation, monkeypatch
):
    fs, ctx, model, analyzer, context, policies = setup_observation
    older = analysis(session="viking://user/u/sessions/older/history/archive_001", passed=True)
    historical = analysis(session="viking://user/u/sessions/old/history/archive_001", passed=True)
    failed = analysis(session="viking://user/u/sessions/failed/history/archive_001", passed=False)
    unknown = analysis(session="viking://user/u/sessions/unknown/history/archive_001", passed=None)
    foreign = analysis(name="other", session="viking://user/u/sessions/other/history/archive_001")
    pending = analysis(session="viking://user/u/sessions/pending/history/archive_001", passed=True)
    history_runs = [older, historical, failed, unknown, foreign, pending]
    for day, run in enumerate(history_runs, start=1):
        await observe(analyzer, run, policies, context)
        memory = run.metadata["trajectory_memory"]
        memory.extra_fields["created_at"] = f"2024-01-{day:02d}T00:00:00+00:00"
        memory.extra_fields["experience_update"]["state"] = (
            "pending" if run is pending else "no_op" if run is historical else "applied"
        )
        fs.files[memory.uri] = MemoryFileUtils.write(memory)
    runs = [
        analysis(session=f"viking://user/u/sessions/s{i}/history/archive_001", passed=False)
        for i in range(2)
    ]
    signals = await asyncio.gather(*(observe(analyzer, run, policies, context) for run in runs))
    captured = []

    async def synthesize(self, provider, context):
        assert not fs.policy_lock.locked()
        provider.read_file = AsyncMock(return_value=None)
        payload = await presented_trajectories(provider)
        captured.append(payload)
        return operations(
            content='dag = workflow("cancel")\nc = check("duplicate requested")\nr = tell("verify duplicate identity")\nc.then(r)'
        )

    monkeypatch.setattr(ExperienceGradientEstimator, "_run_extract_loop", synthesize)
    trainer = StreamingPolicyTrainer(
        policy_set=policies,
        rollout_analyzer=SessionRolloutAnalyzer(),
        gradient_estimator=ExperienceGradientEstimator(viking_fs=fs),
        policy_optimizer=TrajectoryExperienceOptimizer(
            fs, ExperienceGradientEstimator(viking_fs=fs)
        ),
        policy_updater=MemoryFilePolicyUpdater(viking_fs=fs),
        context=PipelineContext(
            analysis_context=SessionAnalyzerContext(ctx),
            gradient_context=context,
            optimization_context=PatchMergePolicyOptimizerContext(ctx),
            apply_context=ctx,
        ),
        config=StreamingPolicyTrainerConfig(max_gradients_per_update=2, max_wait_seconds=1),
    )
    try:
        results = await asyncio.gather(
            *(
                trainer.submit_gradients(signal, analysis=run, rollout=run.rollout)
                for signal, run in zip(signals, runs, strict=True)
            )
        )
    finally:
        await trainer.close()
    assert len(captured) == 1
    payload = captured[0]
    assert [record["outcome"] for record in payload] == ["success", "failure", "failure"]
    assert payload[0]["trajectory_uri"] == historical.trajectories[0].uri
    assert all(record["case_name"] == "case_a" for record in payload)
    assert results[0].batch_result is results[1].batch_result
    assert all(result.apply_result.written_uris == [f"{ROOT}/case_a.md"] for result in results)
    stored = MemoryFileUtils.read(fs.files[f"{ROOT}/case_a.md"])
    assert len(stored.extra_fields["source_sessions"]) == 3
    assert {t.uri for run in [historical, *runs] for t in run.trajectories} == (
        {link["to_uri"] for link in stored.links}
    )
    assert not any("/sessions/" in link["to_uri"] for link in stored.links)
    assert len([uri for uri in fs.files if "/trajectories/" in uri]) == len(history_runs) + len(
        runs
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("outcomes", [(False, True), (True, False), (None, True)])
async def test_any_current_success_skips_history_lookup(setup_observation, monkeypatch, outcomes):
    fs, ctx, _model, analyzer, context, policies = setup_observation
    signals, runs = [], []
    for index, passed in enumerate(outcomes):
        run = analysis(session=f"viking://session/current{index}/archives/001", passed=passed)
        runs.append(run)
        signals.extend(await observe(analyzer, run, policies, context))
    lookup = AsyncMock(side_effect=AssertionError("A successful current batch needs no history"))
    monkeypatch.setattr(fs, "ls", lookup)
    estimator = SimpleNamespace(estimate=AsyncMock(return_value=[]))

    await TrajectoryExperienceOptimizer(fs, estimator).plan(
        signals, policies, PatchMergePolicyOptimizerContext(ctx)
    )

    lookup.assert_not_awaited()
    estimator.estimate.assert_awaited_once()
    selected = estimator.estimate.await_args.args[0]
    expected = [trajectory.uri for run in runs for trajectory in run.trajectories]
    assert [trajectory.uri for trajectory in selected.trajectories] == expected
    assert selected.metadata["current_trajectory_uris"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("passed", [False, None])
async def test_no_historical_success_does_not_use_other_outcomes(
    setup_observation, monkeypatch, passed
):
    fs, ctx, _model, analyzer, context, policies = setup_observation
    for outcome in ("failure", "partial", "unfinished", "unknown"):
        historical = analysis(session=f"viking://session/{outcome}/archives/001", passed=False)
        await observe(analyzer, historical, policies, context)
        memory = historical.metadata["trajectory_memory"]
        memory.extra_fields["outcome"] = outcome
        memory.extra_fields["experience_update"]["state"] = "applied"
        # Ineligible historical outcomes are excluded regardless of their content.
        memory.content = "irrelevant history"
        fs.files[memory.uri] = MemoryFileUtils.write(memory)
    run = analysis(session="viking://session/current/archives/001", passed=passed)
    signals = await observe(analyzer, run, policies, context)
    lookup = AsyncMock(wraps=fs.ls)
    monkeypatch.setattr(fs, "ls", lookup)
    estimator = SimpleNamespace(estimate=AsyncMock(return_value=[]))

    await TrajectoryExperienceOptimizer(fs, estimator).plan(
        signals, policies, PatchMergePolicyOptimizerContext(ctx)
    )

    lookup.assert_awaited_once()
    selected = estimator.estimate.await_args.args[0]
    assert [trajectory.uri for trajectory in selected.trajectories] == [run.trajectories[0].uri]


@pytest.mark.asyncio
async def test_failure_only_batch_can_produce_noop_without_losing_trajectory(
    setup_observation, monkeypatch
):
    fs, ctx, model, analyzer, context, policies = setup_observation
    run = analysis(passed=False)
    signals = await observe(analyzer, run, policies, context)

    async def no_change(self, provider, context):
        provider.read_file = AsyncMock(return_value=None)
        payload = await presented_trajectories(provider)
        assert len(payload) == 1
        assert payload[0]["outcome"] == "failure"
        return None

    monkeypatch.setattr(ExperienceGradientEstimator, "_run_extract_loop", no_change)
    plan = await TrajectoryExperienceOptimizer(fs, ExperienceGradientEstimator(viking_fs=fs)).plan(
        signals, policies, PatchMergePolicyOptimizerContext(ctx)
    )
    assert plan.items == []
    assert plan.read_policy_uris == [f"{ROOT}/case_a.md"]
    assert run.trajectories[0].uri in fs.files


@pytest.mark.asyncio
async def test_optional_gate_rejects_dag_but_keeps_observation(setup_observation, monkeypatch):
    fs, ctx, model, analyzer, context, policies = setup_observation
    signals = await observe(analyzer, analysis(passed=False), policies, context)

    async def candidate(self, provider, context):
        return operations()

    monkeypatch.setattr(ExperienceGradientEstimator, "_run_extract_loop", candidate)
    gate = SimpleNamespace(
        validate_proposals=AsyncMock(
            return_value=(
                [],
                {
                    "experience_improvement_gate": {
                        "passed": False,
                        "accepted_count": 0,
                        "rejected_count": 1,
                        "candidates": [
                            {
                                "target_uri": ROOT + "/case_a.md",
                                "target_name": "case_a",
                                "passed": False,
                                "reason": "unsupported correction",
                            }
                        ],
                    }
                },
            )
        )
    )
    plan = await TrajectoryExperienceOptimizer(
        fs, ExperienceGradientEstimator(viking_fs=fs), proposal_gate=gate
    ).plan(signals, policies, PatchMergePolicyOptimizerContext(ctx))
    assert not plan.items
    assert plan.read_policy_uris is None
    gate.validate_proposals.assert_awaited_once()
    assert signals[0].after_file.uri in fs.files
    assert plan.metadata["proposal_gates"][0]["experience_improvement_gate"]["rejected_count"] == 1


@pytest.mark.asyncio
async def test_deferred_experience_learning_claims_and_applies_pending_trajectory(
    setup_observation,
    monkeypatch,
):
    fs, ctx, _model, analyzer, context, policies = setup_observation
    run = analysis(passed=False)
    (signal,) = await observe(analyzer, run, policies, context)
    case = run.rollout.case
    case_uri = run.metadata["case_uri"]
    fs.files[case_uri] = MemoryFileUtils.write(
        MemoryFile(
            uri=case_uri,
            memory_type="cases",
            content="case",
            extra_fields={
                "case_name": case.name,
                "task_signature": case.task_signature,
                "input": json.dumps(case.input),
                "rubric": json.dumps(
                    {
                        "name": case.rubric.name,
                        "description": case.rubric.description,
                        "criteria": [],
                    }
                ),
            },
        )
    )
    legacy_uri = signal.after_file.uri.rsplit("/", 1)[0] + "/legacy.md"
    fs.files[legacy_uri] = MemoryFileUtils.write(
        MemoryFile(
            uri=legacy_uri,
            memory_type="trajectories",
            content="legacy procedure-style Trajectory",
            extra_fields={
                "trajectory_name": "legacy",
                "case_name": case.name,
                "outcome": "failure",
            },
        )
    )

    async def synthesize(self, provider, synth_context):
        provider.read_file = AsyncMock(return_value=None)
        await provider.prefetch()
        return operations(
            content=(
                'dag = workflow("cancel")\n'
                'c = check("duplicate requested")\n'
                'r = tell("verify duplicate identity")\n'
                "c.then(r)"
            )
        )

    async def registry_snapshot(viking_fs, account_id, registry):
        return registry

    monkeypatch.setattr(ExperienceGradientEstimator, "_run_extract_loop", synthesize)
    monkeypatch.setattr("openviking.session.compressor_v3.get_viking_fs", lambda: fs)
    monkeypatch.setattr(
        "openviking.session.memory.account_templates.resolve_account_memory_registry",
        registry_snapshot,
    )
    monkeypatch.setattr(
        "openviking.session.compressor_v3.get_openviking_config",
        lambda: SimpleNamespace(
            agent_evolution=SimpleNamespace(
                experience_gate_enabled=False,
                dag_decider=None,
            ),
            jev=None,
        ),
    )
    compressor = SessionCompressorV3(vikingdb=None)
    compressor._link_case_to_training_outputs = AsyncMock()

    result = await compressor.learn_pending_experiences(
        ctx=ctx,
        trajectory_uris=[signal.after_file.uri],
    )

    assert result["applied_count"] == 1
    assert result["written_experience_uris"] == [f"{ROOT}/case_a.md"]
    stored = MemoryFileUtils.read(fs.files[signal.after_file.uri], uri=signal.after_file.uri)
    assert stored.extra_fields["experience_update"]["state"] == "applied"
    assert stored.extra_fields["experience_update"]["attempts"] == 1
    assert all(
        not uri.endswith("/legacy.md")
        for uri in result["results"][0]["plan_metadata"]["comparisons"][0]["trajectory_uris"]
    )

    repeated = await compressor.learn_pending_experiences(
        ctx=ctx,
        trajectory_uris=[signal.after_file.uri],
    )
    assert repeated["trajectory_count"] == 0


@pytest.mark.asyncio
async def test_deferred_experience_filters_form_a_union(setup_observation):
    fs, ctx, _model, analyzer, context, policies = setup_observation
    explicit_run = analysis(passed=False)
    scanned_run = analysis(name="other", passed=True)
    (explicit_signal,) = await observe(analyzer, explicit_run, policies, context)
    (scanned_signal,) = await observe(analyzer, scanned_run, policies, context)
    legacy_uri = scanned_signal.after_file.uri.rsplit("/", 1)[0] + "/legacy.md"
    fs.files[legacy_uri] = MemoryFileUtils.write(
        MemoryFile(
            uri=legacy_uri,
            memory_type="trajectories",
            content="legacy procedure-style Trajectory",
            extra_fields={
                "trajectory_name": "legacy",
                "case_name": scanned_run.rollout.case.name,
                "outcome": "success",
            },
        )
    )
    compressor = SessionCompressorV3(vikingdb=None, rollout_analyzer=SimpleNamespace())

    memories = await compressor._load_pending_trajectory_memories(
        viking_fs=fs,
        ctx=ctx,
        registry=get_default_registry(),
        trajectory_uris=[explicit_signal.after_file.uri],
        case_names=[scanned_run.rollout.case.name],
        retry_failed=False,
    )

    assert {memory.uri for memory in memories} == {
        explicit_signal.after_file.uri,
        scanned_signal.after_file.uri,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("learn_result", "expected_state"),
    [
        (
            {
                "case_name": "case_a",
                "trajectory_uris": ["trajectory-uri"],
                "experience_uri": f"{ROOT}/case_a.md",
                "written_experience_uris": [],
                "plan_metadata": {"reason": "no useful change"},
            },
            "no_op",
        ),
        (RuntimeError("synthesis failed"), "failed"),
    ],
)
async def test_deferred_experience_learning_records_terminal_state(
    setup_observation,
    monkeypatch,
    learn_result,
    expected_state,
):
    fs, ctx, _model, analyzer, context, policies = setup_observation
    run = analysis(passed=False)
    (signal,) = await observe(analyzer, run, policies, context)

    async def registry_snapshot(viking_fs, account_id, registry):
        return registry

    monkeypatch.setattr("openviking.session.compressor_v3.get_viking_fs", lambda: fs)
    monkeypatch.setattr(
        "openviking.session.memory.account_templates.resolve_account_memory_registry",
        registry_snapshot,
    )
    compressor = SessionCompressorV3(vikingdb=None)
    compressor._load_pending_trajectory_memories = AsyncMock(return_value=[signal.after_file])
    if isinstance(learn_result, Exception):
        compressor._learn_one_trajectory_case = AsyncMock(side_effect=learn_result)
    else:
        compressor._learn_one_trajectory_case = AsyncMock(return_value=learn_result)

    result = await compressor.learn_pending_experiences(
        ctx=ctx,
        trajectory_uris=[signal.after_file.uri],
    )

    assert result[f"{expected_state}_count"] == 1
    stored = MemoryFileUtils.read(fs.files[signal.after_file.uri], uri=signal.after_file.uri)
    state = stored.extra_fields["experience_update"]
    assert state["state"] == expected_state
    assert state["attempts"] == 1
    assert (state["error"] is not None) is (expected_state == "failed")


@pytest.mark.asyncio
async def test_inserted_tell_reaches_commit_persisted_trajectory_and_experience_input(
    setup_observation, tmp_path, monkeypatch
):
    from vikingbot.config.schema import SessionKey
    from vikingbot.session.manager import SessionManager

    from benchmark.tau2.train.dag_experience_runtime import Tau2DagExperienceRuntime
    from openviking.session.compressor_v3 import _training_outcome_from_messages
    from openviking.session.train.components.session_commit import _evaluation_message_to_request

    fs, ctx, model, analyzer, context, policies = setup_observation
    # Supply the existence check required by the create-only memory guard.
    monkeypatch.setattr(
        fs, "exists", AsyncMock(side_effect=lambda uri, **kwargs: uri in fs.files), raising=False
    )
    uri = f"{ROOT}/case_a.md"
    instruction = {
        "experience_uri": uri,
        "node_id": 2,
        "slot_name": "review_scope",
        "provider": {"type": "TellAgent", "instruction": "Review the cancellation scope"},
        "conditions": [],
    }
    runtime = Tau2DagExperienceRuntime()
    runtime.bind_session(
        SessionManager(tmp_path), SessionKey(type="cli", channel_id="test", chat_id="execution")
    )
    runtime.client = SimpleNamespace(
        ensure_session=AsyncMock(),
        search_advice=AsyncMock(
            return_value={
                "instructions": [instruction],
                "experiences": [
                    {
                        "experience_uri": uri,
                        "state": "running",
                        "node_slots": {1: "cancel_requested", 2: "review_scope"},
                        "slot_values": {"cancel_requested": True},
                        "executed_nodes": [1],
                        "current_nodes": [2],
                        "actions": [instruction],
                    }
                ],
                "errors": [],
            }
        ),
    )
    await runtime.record_injected(await runtime.ask_for_advice("cancel", []))
    run = analysis(passed=True)
    run.rollout.evaluation = run.evaluation
    run.rollout.metadata["dag_runtime"] = {"events": runtime.events}
    envelope = _evaluation_message_to_request(run.rollout)
    text = envelope["parts"][0]["text"]
    assert "delivered_advice" not in text and "action_outcomes" not in text
    run.evaluation, execution = _training_outcome_from_messages(
        [Message(id="outcome", role="user", parts=[TextPart(text=text)])]
    )
    run.metadata["experience_execution"] = execution
    run.trajectories = []
    await observe(analyzer, run, policies, context)
    trajectory = run.trajectories[0]
    stored = MemoryFileUtils.read(fs.files[trajectory.uri], uri=trajectory.uri)
    expected = {
        "case_a": {
            "slot_values": {"cancel_requested": True, "review_scope": True},
            "executed_nodes": ["cancel_requested", "review_scope"],
            "current_nodes": [],
        }
    }
    assert stored.extra_fields["experience_execution"] == expected
    provider = AgentExperienceContextProvider(
        messages=[],
        case=run.rollout.case,
        target_uri=uri,
        source_session_uri=run.metadata["source_session_uri"],
        trajectories=[trajectory],
    )
    provider._eval_memory = AsyncMock(return_value={"experience": {"error": "File not found"}})
    record = (await presented_trajectories(provider))[0]
    assert record["experience_execution"] == expected
    assert record["evaluation"] == {"passed": True, "score": 1.0}
