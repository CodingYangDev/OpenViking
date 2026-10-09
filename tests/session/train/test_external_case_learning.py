# Copyright (c) 2026, Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_case_experience_pipeline import observe, presented_trajectories
from test_case_experience_pipeline import setup_observation as setup_observation
from test_gradient_estimator_component import ROOT, analysis, operations

from openviking.session.compressor_v3 import SessionCompressorV3
from openviking.session.memory.dataclass import MemoryFile
from openviking.session.memory.experience.dag import DagEvidenceRef
from openviking.session.memory.utils.memory_file_utils import MemoryFileUtils
from openviking.session.train import (
    ExperienceGradientEstimator,
    ExperienceSet,
    PolicyApplyResult,
    PolicyUpdatePlan,
    StreamingPolicyTrainer,
    StreamingPolicyTrainerConfig,
)


@pytest.fixture
def trainer():
    policies = ExperienceSet(ROOT, [])
    result = PolicyApplyResult(updated_policy_set=policies)
    plan = PolicyUpdatePlan(metadata={"optimizer": "explicit-test"})
    trainer = StreamingPolicyTrainer(
        policy_set=policies,
        rollout_analyzer=SimpleNamespace(
            analyze=AsyncMock(side_effect=AssertionError("must not analyze"))
        ),
        gradient_estimator=SimpleNamespace(
            estimate=AsyncMock(side_effect=AssertionError("must not estimate"))
        ),
        policy_optimizer=SimpleNamespace(),
        policy_updater=SimpleNamespace(),
        config=StreamingPolicyTrainerConfig(max_gradients_per_update=2, max_wait_seconds=3600),
    )
    trainer._core = SimpleNamespace(plan_and_apply=AsyncMock(return_value=(plan, result)))
    return trainer


@pytest.mark.asyncio
async def test_explicit_gradient_batch_skips_window_and_preserves_whole_batch(trainer):
    gradients = [SimpleNamespace() for _ in range(17)]
    analyses = [analysis(name="one"), analysis(name="two")]
    finalize = AsyncMock()
    result = await asyncio.wait_for(
        trainer.train_gradients(gradients, analyses=analyses, batch_finalizer=finalize), timeout=1
    )
    trainer._core.plan_and_apply.assert_awaited_once()
    assert trainer._core.plan_and_apply.await_args.kwargs["gradients"] == gradients
    assert result.plan.metadata == {"optimizer": "explicit-test"}
    assert result.analyses == analyses
    assert result.metadata["chunk_gradient_counts"] == [17]
    assert result.metadata["analysis_count"] == 2
    assert result.metadata["rollout_count"] == 2
    assert result.metadata["flush_reason"] == "explicit"
    finalize.assert_awaited_once_with(result)
    assert trainer._batcher._timer_task is None
    assert await trainer._batcher.get_buffered_item_count() == 0
    await trainer.close()
    with pytest.raises(RuntimeError, match="closed"):
        await trainer.train_gradients(gradients)


@pytest.mark.asyncio
async def test_explicit_trainer_close_waits_for_active_batch(trainer):
    entered = asyncio.Event()
    finish = asyncio.Event()
    original_result = trainer._core.plan_and_apply.return_value

    async def plan_and_apply(**kwargs):
        entered.set()
        await finish.wait()
        return original_result

    trainer._core.plan_and_apply.side_effect = plan_and_apply
    task = asyncio.create_task(trainer.train_gradients([SimpleNamespace()]))
    closing = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        closing = asyncio.create_task(trainer.close())
        await asyncio.sleep(0)
        assert not closing.done()
        finish.set()
        await task
        await closing
    finally:
        finish.set()
        await asyncio.gather(
            task, *([closing] if closing is not None else []), return_exceptions=True
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("failure"), asyncio.CancelledError()])
@pytest.mark.parametrize("stage", ["core", "finalizer"])
async def test_explicit_batch_failure_releases_execution_lock(trainer, failure, stage):
    original_result = trainer._core.plan_and_apply.return_value
    finalizer = AsyncMock(side_effect=failure) if stage == "finalizer" else None
    if stage == "core":
        trainer._core.plan_and_apply.side_effect = failure
    with pytest.raises(type(failure)):
        await trainer.train_gradients([SimpleNamespace()], batch_finalizer=finalizer)
    trainer._core.plan_and_apply.side_effect = None
    trainer._core.plan_and_apply.return_value = original_result
    await asyncio.wait_for(trainer.train_gradients([SimpleNamespace()]), timeout=1)
    await trainer.close()


def test_case_concurrency_default_and_validation():
    assert StreamingPolicyTrainerConfig().max_concurrent_cases == 10
    with pytest.raises(ValueError, match="max_concurrent_cases"):
        StreamingPolicyTrainerConfig(max_concurrent_cases=0)


@pytest.fixture
async def cases(setup_observation, monkeypatch, request):
    fs, ctx, model, analyzer, context, policies = setup_observation
    records = []
    for index in range(getattr(request, "param", 6)):
        name = f"case_{index}"
        for trial in range(2):
            run = analysis(
                name=name,
                session=f"viking://user/u/sessions/{name}_{trial}/history/archive_001",
                passed=trial == 0,
            )
            case = run.rollout.case
            uri = run.metadata["case_uri"]
            fs.files[uri] = MemoryFileUtils.write(
                MemoryFile(
                    uri=uri,
                    memory_type="cases",
                    content="case",
                    extra_fields={
                        "case_name": name,
                        "task_signature": case.task_signature,
                        "input": "{}",
                        "rubric": json.dumps({"name": "done", "description": "", "criteria": []}),
                    },
                )
            )
            (signal,) = await observe(analyzer, run, policies, context)
            records.append(signal.after_file)

    async def registry_snapshot(fs, account_id, registry):
        return registry

    monkeypatch.setattr("openviking.session.compressor_v3.get_viking_fs", lambda: fs)
    monkeypatch.setattr(
        "openviking.session.memory.account_templates.resolve_account_memory_registry",
        registry_snapshot,
    )
    monkeypatch.setattr(
        "openviking.session.compressor_v3.get_openviking_config",
        lambda: SimpleNamespace(
            agent_evolution=SimpleNamespace(experience_gate_enabled=False, dag_decider=None),
            jev=None,
        ),
    )
    compressor = SessionCompressorV3(
        vikingdb=None,
        streaming_trainer_config=StreamingPolicyTrainerConfig(
            max_gradients_per_update=1,
            max_wait_seconds=3600,
            max_concurrent_cases=4,
        ),
    )
    compressor._link_case_to_training_outputs = AsyncMock()
    fs.commit = AsyncMock()
    return SimpleNamespace(fs=fs, ctx=ctx, compressor=compressor, records=records)


def states(cases):
    return [
        MemoryFileUtils.read(cases.fs.files[r.uri], uri=r.uri).extra_fields["experience_update"]
        for r in cases.records
    ]


async def learn(cases, **kwargs):
    return await cases.compressor.learn_pending_experiences(
        ctx=cases.ctx, trajectory_uris=[r.uri for r in cases.records], **kwargs
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("cases", [1], indirect=True)
async def test_deferred_learning_reads_opaque_content_without_losing_gate_evidence(
    cases, monkeypatch
):
    expected = {}
    for index, record in enumerate(cases.records):
        memory = MemoryFileUtils.read(cases.fs.files[record.uri], uri=record.uri)
        memory.content = (
            f"Execution {index}: unnumbered facts and relevant rules.\n" * 500
        ).strip()
        assert len(memory.content) > 16_384
        expected[record.uri] = memory.content
        cases.fs.files[record.uri] = MemoryFileUtils.write(memory)
    calls = []

    async def synthesize(estimator, provider, context):
        calls.append(provider)
        for trajectory in provider.trajectories:
            assert trajectory.content == expected[trajectory.uri]
            evidence = [
                DagEvidenceRef.model_validate(item) for item in trajectory.metadata["gate_evidence"]
            ]
            assert "".join(item.summary for item in evidence) == trajectory.content
        presented = await presented_trajectories(provider)
        assert {item["trajectory_uri"]: item["content"] for item in presented} == expected
        return None

    monkeypatch.setattr(ExperienceGradientEstimator, "_run_extract_loop", synthesize)

    result = await learn(cases)

    assert len(calls) == 1
    assert result["failed_count"] == 0
    assert result["no_op_count"] == 1
    assert result["trajectory_count"] == 2


def noop_result(kwargs):
    return {
        "case_name": kwargs["case_name"],
        "trajectory_uris": [m.uri for m in kwargs["memories"]],
        "experience_uri": f"{ROOT}/{kwargs['case_name']}.md",
        "written_experience_uris": [],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cases,concurrency", [(6, 1), (6, 2), (6, 4), (12, None)], indirect=["cases"]
)
async def test_external_learning_is_bounded_and_claims_only_admitted_cases(cases, concurrency):
    if concurrency is None:
        cases.compressor.streaming_trainer_config = StreamingPolicyTrainerConfig()
        concurrency = 10
    else:
        cases.compressor.streaming_trainer_config.max_concurrent_cases = concurrency
    case_count = len(cases.records) // 2
    workers_started = asyncio.Event()
    finish = asyncio.Event()
    started = []
    active = peak = 0

    async def work(**kwargs):
        nonlocal active, peak
        started.append(kwargs["case_name"])
        assert len(kwargs["memories"]) == 2
        active += 1
        peak = max(peak, active)
        if len(started) == concurrency:
            workers_started.set()
        try:
            await finish.wait()
            return noop_result(kwargs)
        finally:
            active -= 1

    cases.compressor._learn_one_trajectory_case = AsyncMock(side_effect=work)
    task = asyncio.create_task(learn(cases))
    try:
        await asyncio.wait_for(workers_started.wait(), timeout=1)
        assert len(started) == concurrency
        assert sum(s["state"] == "processing" for s in states(cases)) == concurrency * 2
        assert (
            sum(s["state"] == "pending" for s in states(cases))
            == len(cases.records) - concurrency * 2
        )
        finish.set()
        result = await task
        assert peak == concurrency and active == 0
        assert result["no_op_count"] == case_count
        assert [r["case_name"] for r in result["results"]] == sorted(
            f"case_{i}" for i in range(case_count)
        )
        assert all(s["state"] == "no_op" and s["attempts"] == 1 for s in states(cases))
        assert (await learn(cases))["case_count"] == 0
    finally:
        finish.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_one_case_failure_does_not_stop_other_cases(cases):
    async def work(**kwargs):
        if kwargs["case_name"] == "case_2":
            raise RuntimeError("bad Case")
        return noop_result(kwargs)

    cases.compressor._learn_one_trajectory_case = AsyncMock(side_effect=work)
    result = await learn(cases)
    assert result["failed_count"] == 1 and result["no_op_count"] == 5
    assert sum(s["state"] == "failed" for s in states(cases)) == 2
    assert (await learn(cases))["case_count"] == 0
    cases.compressor._learn_one_trajectory_case.side_effect = lambda **kwargs: noop_result(kwargs)
    retried = await learn(cases, retry_failed=True)
    assert retried["case_count"] == retried["no_op_count"] == 1
    assert sum(s["attempts"] == 2 for s in states(cases)) == 2


@pytest.mark.asyncio
async def test_cancelled_learning_joins_workers_and_leaves_recoverable_leases(cases):
    ready = asyncio.Event()
    active = 0
    started = []

    async def work(**kwargs):
        nonlocal active
        started.append(kwargs["case_name"])
        active += 1
        if active == 4:
            ready.set()
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1

    cases.compressor._learn_one_trajectory_case = AsyncMock(side_effect=work)
    task = asyncio.create_task(learn(cases))
    try:
        await asyncio.wait_for(ready.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert active == 0 and len(started) == 4
        assert not cases.compressor._experience_learning_lock.locked()
        assert not cases.fs.policy_lock.locked()
        for s in states(cases):
            assert s["state"] in {"pending", "processing"}
            if s["state"] == "processing":
                assert s["lease_until"] is not None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_external_learning_reuses_explicit_trainer_and_serializes_finalization(
    cases, monkeypatch
):
    original = StreamingPolicyTrainer.train_gradients
    batches = []
    active = peak = 0
    ready = asyncio.Event()
    finalizing = finalizer_peak = 0

    async def train(trainer, gradients, **kwargs):
        batches.append((len(gradients), {g.target_name for g in gradients}))
        result = await original(trainer, gradients, **kwargs)
        assert trainer._batcher._timer_task is None
        return result

    async def synthesize(estimator, provider, context):
        nonlocal active, peak
        assert not cases.fs.policy_lock.locked()
        payload = await presented_trajectories(provider)
        assert [record["outcome"] for record in payload] == ["success", "failure"]
        active += 1
        peak = max(peak, active)
        if active == 4:
            ready.set()
        try:
            await asyncio.wait_for(ready.wait(), timeout=1)
        finally:
            active -= 1
        return operations(
            name=provider.case.name,
            content='dag = workflow("case")\nc = check("user asks")\nr = tell("verify")\nc.then(r)',
        )

    async def commit(**kwargs):
        nonlocal finalizing, finalizer_peak
        finalizing += 1
        finalizer_peak = max(finalizer_peak, finalizing)
        await asyncio.sleep(0.01)
        finalizing -= 1

    monkeypatch.setattr(StreamingPolicyTrainer, "train_gradients", train)
    monkeypatch.setattr(ExperienceGradientEstimator, "_run_extract_loop", synthesize)
    cases.fs.commit.side_effect = commit
    result = await asyncio.wait_for(learn(cases), timeout=3)
    assert result["applied_count"] == 6 and result["failed_count"] == 0
    assert peak == 4 and finalizer_peak == 1
    assert len(batches) == 6 and all(size == 2 and len(names) == 1 for size, names in batches)
    assert all(
        r["plan_metadata"]["optimizer"] == "trajectory_comparison" for r in result["results"]
    )
    for name in [f"case_{i}" for i in range(6)]:
        uri = f"{ROOT}/{name}.md"
        memory = MemoryFileUtils.read(cases.fs.files[uri], uri=uri)
        assert len(memory.links) == 2
        assert all(f"/trajectories/{name}/" in link["to_uri"] for link in memory.links)
    assert all(s["state"] == "applied" for s in states(cases))


@pytest.mark.asyncio
async def test_overlapping_external_requests_do_not_reclaim_same_trajectories(cases):
    entered = asyncio.Event()
    finish = asyncio.Event()

    async def work(**kwargs):
        entered.set()
        await finish.wait()
        return noop_result(kwargs)

    cases.compressor._learn_one_trajectory_case = AsyncMock(side_effect=work)
    first = asyncio.create_task(learn(cases))
    second = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        second = asyncio.create_task(learn(cases))
        await asyncio.sleep(0)
        assert not second.done()
        finish.set()
        first_result, second_result = await asyncio.gather(first, second)
        assert first_result["case_count"] == 6
        assert second_result["case_count"] == 0
        assert cases.compressor._learn_one_trajectory_case.await_count == 6
        assert all(s["attempts"] == 1 for s in states(cases))
    finally:
        finish.set()
        await asyncio.gather(
            first, *([second] if second is not None else []), return_exceptions=True
        )


@pytest.mark.asyncio
async def test_explicit_trainer_native_apply_preserves_forward_provenance(tmp_path, monkeypatch):
    from openviking.pyagfs import get_binding_client
    from openviking.server.identity import RequestContext, Role
    from openviking.session.memory.dataclass import StoredLink
    from openviking.session.memory.memory_updater import MemoryUpdater
    from openviking.session.train import MemoryFilePolicyUpdater, PipelineContext, PolicyPlanItem
    from openviking.storage.viking_fs import VikingFS
    from openviking.utils.agfs_utils import RagfsBindingConfig, create_agfs_client
    from openviking_cli.session.user_id import UserIdentifier
    from openviking_cli.utils.config.agfs_config import AGFSConfig

    try:
        get_binding_client()
    except ImportError:
        pytest.skip("RAGFS native binding is not installed")
    native = create_agfs_client(RagfsBindingConfig(agfs=AGFSConfig(path=str(tmp_path))))
    fs = VikingFS(agfs=native)
    ctx = RequestContext(user=UserIdentifier("default", "u"), role=Role.ROOT)
    target = ROOT + "/case_a.md"
    source = "viking://user/u/memories/trajectories/case_a/one.md"
    original = MemoryFileUtils.write(
        MemoryFile(
            uri=source,
            memory_type="trajectories",
            content="# Trajectory\n\n1. user asked",
            extra_fields={"trajectory_name": "one", "case_name": "case_a", "outcome": "success"},
        )
    )
    monkeypatch.setattr(MemoryUpdater, "_vectorize_memories", AsyncMock())
    monkeypatch.setattr(
        "openviking.utils.embedding_utils.vectorize_directory_meta", AsyncMock(return_value=set())
    )
    trainer = None
    try:
        await fs.write_file(source, original, ctx=ctx)
        await fs.mkdir(ROOT, ctx=ctx)
        proposed = PolicyUpdatePlan(
            items=[
                PolicyPlanItem(
                    kind="upsert",
                    memory_type="experiences",
                    target_name="case_a",
                    target_uri=target,
                    before_content=None,
                    after_content='dag = workflow("case")\nr = tell("verify")',
                    links=[StoredLink(from_uri=target, to_uri=source, link_type="derived_from")],
                )
            ],
            read_policy_uris=[target],
        )

        async def model(*args):
            assert not await fs._async_agfs.pathlock_is_locked(fs._uri_to_path(ROOT, ctx=ctx))
            return proposed

        trainer = StreamingPolicyTrainer(
            policy_set=ExperienceSet(ROOT, [], viking_fs=fs, request_context=ctx),
            rollout_analyzer=SimpleNamespace(),
            gradient_estimator=SimpleNamespace(),
            policy_optimizer=SimpleNamespace(plan=model),
            policy_updater=MemoryFilePolicyUpdater(viking_fs=fs),
            context=PipelineContext(apply_context=ctx),
        )
        result = await trainer.train_gradients([SimpleNamespace()])
        assert result.apply_result.errors == []
        assert result.plan is proposed
        stored = MemoryFileUtils.read(await fs.read_file(target, ctx=ctx), uri=target)
        assert {link["to_uri"] for link in stored.links} == {source}
        assert await fs.read_file(source, ctx=ctx) == original
        assert (await fs._async_agfs.pathlock_observe())["active_locks"] == 0
    finally:
        if trainer is not None:
            await trainer.close()
        native.close()


@pytest.mark.asyncio
async def test_case_limit_leaves_other_groups_pending(cases):
    cases.compressor._learn_one_trajectory_case = AsyncMock(
        side_effect=lambda **kwargs: noop_result(kwargs)
    )
    result = await learn(cases, max_cases=2)
    assert result["case_count"] == 2
    assert result["trajectory_count"] == 4
    assert [r["case_name"] for r in result["results"]] == ["case_0", "case_1"]
    assert sum(s["state"] == "pending" for s in states(cases)) == 8
