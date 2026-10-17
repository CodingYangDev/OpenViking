from __future__ import annotations

import copy
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_case_experience_pipeline import observe, presented_trajectories
from test_case_experience_pipeline import setup_observation as setup_observation
from test_gradient_estimator_component import ROOT, analysis, operations, validated_operations

from openviking.session.memory.dataclass import MemoryFile
from openviking.session.memory.utils.link_renderer import LinkRenderer
from openviking.session.memory.utils.memory_file_utils import MemoryFileUtils
from openviking.session.train import (
    ExperienceGradientEstimator,
    MemoryFilePolicyUpdater,
    PatchMergePolicyOptimizerContext,
)
from openviking.session.train.components.trajectory_optimizer import (
    SUCCESS_TRAJECTORY_REFS,
    TrajectoryExperienceOptimizer,
    _comparison_key,
)

DAG = 'dag = workflow("cancel")\nc = check("duplicate requested")\nr = tell("verify duplicate identity")\nc.then(r)'
TARGET = f"{ROOT}/case_a.md"


async def make_run(setup, *, task="a", passed=False, session="current", state="applied"):
    fs, _ctx, _model, analyzer, context, policies = setup
    run = analysis(session=f"viking://session/{session}/archives/001", passed=passed)
    spec = asdict(run.rollout.case)
    spec["task_signature"] = f"task:{task}"
    spec["input"] = {"user_query": f"request {task}", "initial_state": {"version": 1}}
    run.metadata["task_spec"] = spec
    signals = await observe(analyzer, run, policies, context)
    memory = run.metadata["trajectory_memory"]
    memory.extra_fields["experience_update"]["state"] = state
    fs.files[memory.uri] = MemoryFileUtils.write(memory)
    return run, signals


def references(*runs):
    return {
        _comparison_key(run.trajectories[0].metadata): LinkRenderer.relative_path(
            TARGET, run.trajectories[0].uri
        )
        for run in runs
    }


async def existing_policy(setup, refs=None):
    fs, _ctx, _model, _analyzer, _context, policies = setup
    fs.files[TARGET] = MemoryFileUtils.write(
        MemoryFile(
            uri=TARGET,
            content=DAG,
            memory_type="experiences",
            extra_fields={
                "experience_name": "case_a",
                "case_name": "case_a",
                "version": 1,
                "source_sessions": [],
                SUCCESS_TRAJECTORY_REFS: refs or {},
            },
        )
    )
    return await policies.reload()


async def make_plan(setup, signals, policies):
    fs, ctx, *_ = setup
    estimator = SimpleNamespace(estimate=AsyncMock(return_value=[]))
    plan = await TrajectoryExperienceOptimizer(fs, estimator).plan(
        signals, policies, PatchMergePolicyOptimizerContext(ctx)
    )
    estimator.estimate.assert_awaited_once()
    return plan, estimator.estimate.await_args.args[0]


@pytest.mark.asyncio
async def test_unrelated_current_success_does_not_hide_missing_task_comparison(setup_observation):
    setup = setup_observation
    fs = setup[0]
    history_b, _ = await make_run(setup, task="b", session="history-b", passed=True)
    history_c, _ = await make_run(setup, task="c", session="history-c", passed=True)
    policies = await existing_policy(setup, references(history_b, history_c))
    success_a, a = await make_run(setup, task="a", session="success-a", passed=True)
    failed_b, b = await make_run(setup, task="b", session="failed-b")
    fs.ls = AsyncMock(side_effect=AssertionError("Do not scan historical trajectories"))
    fs.read_file = AsyncMock(wraps=fs.read_file)

    _plan, selected = await make_plan(setup, [*a, *b], policies)

    assert [t.uri for t in selected.trajectories] == [
        success_a.trajectories[0].uri,
        failed_b.trajectories[0].uri,
        history_b.trajectories[0].uri,
    ]
    assert selected.metadata["current_trajectory_uris"] == [
        success_a.trajectories[0].uri,
        failed_b.trajectories[0].uri,
    ]
    fs.read_file.assert_awaited_once_with(history_b.trajectories[0].uri, ctx=setup[1])
    fs.ls.assert_not_awaited()


@pytest.mark.asyncio
async def test_matching_current_success_needs_no_historical_read(setup_observation):
    setup = setup_observation
    history, _ = await make_run(setup, session="history", passed=True)
    policies = await existing_policy(setup, references(history))
    success, a = await make_run(setup, session="success", passed=True)
    failure, b = await make_run(setup, session="failure")
    setup[0].read_file = AsyncMock(side_effect=AssertionError("Current success is sufficient"))

    _plan, selected = await make_plan(setup, [*a, *b], policies)

    assert [t.uri for t in selected.trajectories] == [
        success.trajectories[0].uri,
        failure.trajectories[0].uri,
    ]
    setup[0].read_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_noop_saves_success_reference_without_changing_dag_or_source(setup_observation):
    setup = setup_observation
    fs, ctx, *_ = setup
    prior, _ = await make_run(setup, task="other", session="prior", passed=True)
    policies = await existing_policy(setup, references(prior))
    success, signals = await make_run(setup, session="success", passed=True)
    source_before = {
        run.trajectories[0].uri: fs.files[run.trajectories[0].uri] for run in [prior, success]
    }

    plan, _selected = await make_plan(setup, signals, policies)
    assert len(plan.items) == 1
    assert plan.items[0].metadata["success_reference_update_only"] is True
    assert plan.items[0].before_content == plan.items[0].after_content == DAG
    async with policies.lock() as lease:
        result = await MemoryFilePolicyUpdater(viking_fs=fs).apply(
            plan, policies, ctx, transaction_handle=lease
        )
    assert result.errors == []
    stored = MemoryFileUtils.read(fs.files[TARGET], uri=TARGET)
    assert stored.content == DAG
    assert stored.extra_fields[SUCCESS_TRAJECTORY_REFS] == references(prior, success)
    assert all(value.startswith("../trajectories/") for value in references(success).values())
    assert {uri: fs.files[uri] for uri in source_before} == source_before

    # The reference survives a restart/reload and supplies a later failed batch.
    policies = await policies.reload()
    failure, signals = await make_run(setup, session="next-failure")
    plan, selected = await make_plan(setup, signals, policies)
    assert plan.items == []
    assert [t.uri for t in selected.trajectories] == [
        failure.trajectories[0].uri,
        success.trajectories[0].uri,
    ]


@pytest.mark.asyncio
async def test_new_experience_saves_references_but_hides_them_from_model(
    setup_observation, monkeypatch
):
    setup = setup_observation
    fs, ctx, _model, _analyzer, _context, policies = setup
    success, signals = await make_run(setup, session="success", passed=True)

    async def synthesize(self, provider, context):
        provider.read_file = AsyncMock(return_value=None)
        payload = await presented_trajectories(provider)
        assert len(payload) == 1 and payload[0]["outcome"] == "success"
        return await validated_operations(provider, operations(content=DAG))

    monkeypatch.setattr(ExperienceGradientEstimator, "_run_extract_loop", synthesize)
    plan = await TrajectoryExperienceOptimizer(fs, ExperienceGradientEstimator(viking_fs=fs)).plan(
        signals, policies, PatchMergePolicyOptimizerContext(ctx)
    )
    async with policies.lock() as lease:
        result = await MemoryFilePolicyUpdater(viking_fs=fs).apply(
            plan, policies, ctx, transaction_handle=lease
        )
    assert result.errors == []
    stored = MemoryFileUtils.read(fs.files[TARGET], uri=TARGET)
    assert stored.extra_fields[SUCCESS_TRAJECTORY_REFS] == references(success)
    assert SUCCESS_TRAJECTORY_REFS not in stored.content

    # Actual prefetch presentation must hide the bookkeeping, not just the DAG body.
    from openviking.session.memory.experience.experience_context import (
        AgentExperienceContextProvider,
    )

    provider = AgentExperienceContextProvider(
        messages=[],
        case=success.rollout.case,
        target_uri=TARGET,
        source_session_uri=success.metadata["source_session_uri"],
        trajectories=success.trajectories,
    )
    provider._ctx, provider._viking_fs = ctx, fs
    messages = provider.format_prefetch_messages(await provider.prefetch())
    assert not any(SUCCESS_TRAJECTORY_REFS in message["content"] for message in messages)


@pytest.mark.asyncio
async def test_absent_reference_does_not_scan_or_create_empty_experience(setup_observation):
    setup = setup_observation
    success, signals = await make_run(setup, passed=True)
    setup[0].ls = AsyncMock(side_effect=AssertionError("No directory scans"))
    plan, selected = await make_plan(setup, signals, setup[-1])
    assert plan.items == []
    assert selected.trajectories == success.trajectories
    assert TARGET not in setup[0].files


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "defect",
    [
        "missing",
        "failure",
        "different_task",
        "different_input",
        "different_rules",
        "different_state",
        "wrong_case",
        "wrong_format",
        "pending",
        "failed_update",
        "missing_update_state",
        "bad_source",
        "cross_user",
        "cross_case",
        "absolute",
        "malformed_reference",
    ],
)
async def test_unusable_reference_does_not_enter_comparison(setup_observation, defect):
    setup = setup_observation
    fs = setup[0]
    history, _ = await make_run(setup, session="history", passed=True)
    refs = references(history)
    key = next(iter(refs))
    memory = history.metadata["trajectory_memory"]
    if defect == "missing":
        del fs.files[memory.uri]
    elif defect == "cross_user":
        refs[key] = "../../other/memories/trajectories/case_a/run.md"
    elif defect == "cross_case":
        refs[key] = "../trajectories/other/run.md"
    elif defect == "absolute":
        refs[key] = memory.uri
    elif defect == "malformed_reference":
        refs[key] = {"uri": memory.uri}
    else:
        if defect == "failure":
            memory.extra_fields["outcome"] = "failure"
        elif defect == "different_task":
            memory.extra_fields["task_spec"]["task_signature"] = "other"
        elif defect == "different_input":
            memory.extra_fields["task_spec"]["input"]["user_query"] = "different request"
        elif defect == "different_rules":
            memory.extra_fields["task_spec"]["rubric"]["description"] = "different rules"
        elif defect == "different_state":
            memory.extra_fields["task_spec"]["input"]["initial_state"] = {"version": 2}
        elif defect == "wrong_case":
            memory.extra_fields["case_name"] = "other"
        elif defect == "wrong_format":
            memory.extra_fields["trajectory_format"] = "unsupported"
        elif defect == "pending":
            memory.extra_fields["experience_update"]["state"] = "pending"
        elif defect == "failed_update":
            memory.extra_fields["experience_update"]["state"] = "failed"
        elif defect == "missing_update_state":
            memory.extra_fields.pop("experience_update")
        elif defect == "bad_source":
            memory.links = []
        fs.files[memory.uri] = MemoryFileUtils.write(memory)
    policies = await existing_policy(setup, refs)
    failure, signals = await make_run(setup, session="failure")
    _plan, selected = await make_plan(setup, signals, policies)
    assert [t.uri for t in selected.trajectories] == [failure.trajectories[0].uri]


@pytest.mark.asyncio
async def test_reference_read_errors_are_not_hidden(setup_observation):
    setup = setup_observation
    history, _ = await make_run(setup, session="history", passed=True)
    policies = await existing_policy(setup, references(history))
    _failure, signals = await make_run(setup, session="failure")
    setup[0].read_file = AsyncMock(side_effect=PermissionError("denied"))
    with pytest.raises(PermissionError, match="denied"):
        await make_plan(setup, signals, policies)


@pytest.mark.asyncio
async def test_unknown_task_context_is_not_treated_as_comparable(setup_observation):
    setup = setup_observation
    history, _ = await make_run(setup, session="history", passed=True)
    policies = await existing_policy(setup, references(history))
    _failure, signals = await make_run(setup, session="failure")
    signals[0].analysis.trajectories[0].metadata.pop("task_spec")
    setup[0].read_file = AsyncMock(side_effect=AssertionError("Do not guess task identity"))
    _plan, selected = await make_plan(setup, signals, policies)
    assert len(selected.trajectories) == 1
    setup[0].read_file.assert_not_awaited()


def test_comparison_key_ignores_trial_identity_but_preserves_context():
    task = {
        "task_signature": "task",
        "input": {"request": "help"},
        "rubric": {},
        "metadata": {"initial_state": 1},
    }
    before = {"task_spec": task}
    after = copy.deepcopy(before)
    after["task_spec"]["metadata"].update(
        {
            "trial": 7,
            "trial_count": 8,
            "rollout_case_name": "task_t7",
            "rollout_task_signature": "task:trial:7",
        }
    )
    assert _comparison_key(before) == _comparison_key(after)
    after["task_spec"]["metadata"]["initial_state"] = 2
    assert _comparison_key(before) != _comparison_key(after)
    assert _comparison_key({}) is None


@pytest.mark.asyncio
async def test_retains_one_current_success_per_task(setup_observation):
    setup = setup_observation
    policies = await existing_policy(setup)
    older, a = await make_run(setup, session="older", passed=True)
    newer, b = await make_run(setup, session="newer", passed=True)
    other, c = await make_run(setup, task="b", session="other", passed=True)
    older.trajectories[0].metadata["created_at"] = "2026-01-01T00:00:00+00:00"
    newer.trajectories[0].metadata["created_at"] = "2026-01-02T00:00:00+00:00"
    plan, _selected = await make_plan(setup, [*b, *c, *a], policies)
    assert plan.items[0].metadata["patch_metadata"][SUCCESS_TRAJECTORY_REFS] == references(
        newer, other
    )


@pytest.mark.asyncio
async def test_loads_one_success_for_each_uncovered_task(setup_observation):
    setup = setup_observation
    first, _ = await make_run(setup, task="a", session="success-a", passed=True)
    second, _ = await make_run(setup, task="b", session="success-b", passed=True)
    policies = await existing_policy(setup, references(first, second))
    failure_a, a = await make_run(setup, task="a", session="failure-a")
    failure_b, b = await make_run(setup, task="b", session="failure-b")
    again_a, again = await make_run(setup, task="a", session="another-a")
    _plan, selected = await make_plan(setup, [*a, *b, *again], policies)
    assert [t.uri for t in selected.trajectories] == [
        failure_a.trajectories[0].uri,
        failure_b.trajectories[0].uri,
        again_a.trajectories[0].uri,
        first.trajectories[0].uri,
        second.trajectories[0].uri,
    ]


def test_comparison_key_matches_actual_trial_commit_payloads():
    from openviking.session.train.components.case_loader import TrialCaseLoader
    from openviking.session.train.components.session_commit import _case_spec_payload

    source = analysis().rollout
    source.case.input = {"domain": "example", "user_query": "cancel duplicate"}
    source.case.metadata = {"initial_state": {"status": "active"}}
    loader = TrialCaseLoader(base_loader=None, trial_count=8)
    keys = []
    for trial in [0, 7]:
        rollout = copy.deepcopy(source)
        rollout.case = loader._trial_case(source.case, trial)
        payload = _case_spec_payload(rollout)
        keys.append(_comparison_key({"task_spec": payload["case"]}))
    assert keys[0] is not None and keys[0] == keys[1]
