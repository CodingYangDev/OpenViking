# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

import httpx


async def test_learn_experiences_delegates_explicit_trajectory_batch(
    client: httpx.AsyncClient,
    service,
    monkeypatch,
):
    captured = {}

    async def fake_learn(**kwargs):
        captured.update(kwargs)
        return {
            "case_count": 1,
            "trajectory_count": 2,
            "applied_count": 1,
            "no_op_count": 0,
            "failed_count": 0,
            "written_experience_uris": ["viking://user/default/memories/experiences/case_a.md"],
            "results": [],
        }

    monkeypatch.setattr(service.agent_evolution, "learn_experiences", fake_learn)
    uris = [
        "viking://user/default/memories/trajectories/case_a/a.md",
        "viking://user/default/memories/trajectories/case_a/b.md",
    ]

    response = await client.post(
        "/api/v1/agent-evolution/experiences/learn",
        json={"trajectory_uris": uris, "lease_seconds": 120},
    )

    assert response.status_code == 200
    assert response.json()["result"]["applied_count"] == 1
    assert captured["trajectory_uris"] == uris
    assert captured["case_names"] is None
    assert captured["lease_seconds"] == 120


async def test_list_experience_trajectories_uses_default_pagination(
    client: httpx.AsyncClient,
    service,
    monkeypatch,
):
    captured = {}

    async def fake_list(*, experience_uri, ctx, limit, offset, start_date, end_date):
        captured.update(
            experience_uri=experience_uri,
            ctx=ctx,
            limit=limit,
            offset=offset,
            start_date=start_date,
            end_date=end_date,
        )
        return {
            "experience_uri": experience_uri,
            "items": [],
            "total": 0,
            "limit": limit,
            "offset": offset,
            "has_more": False,
        }

    monkeypatch.setattr(
        service.agent_evolution,
        "list_trajectories_by_experience",
        fake_list,
    )
    uri = "viking://user/default/memories/experiences/exchange.md"

    response = await client.get(
        "/api/v1/agent-evolution/experiences/trajectories",
        params={"experience_uri": uri},
    )

    assert response.status_code == 200
    assert response.json()["result"]["limit"] == 50
    assert captured["experience_uri"] == uri
    assert captured["limit"] == 50
    assert captured["offset"] == 0
    assert captured["start_date"] is None
    assert captured["end_date"] is None


async def test_list_experience_trajectories_passes_date_range(
    client: httpx.AsyncClient,
    service,
    monkeypatch,
):
    captured = {}

    async def fake_list(**kwargs):
        captured.update(kwargs)
        return {
            "experience_uri": kwargs["experience_uri"],
            "items": [],
            "total": 0,
            "limit": kwargs["limit"],
            "offset": kwargs["offset"],
            "has_more": False,
        }

    monkeypatch.setattr(service.agent_evolution, "list_trajectories_by_experience", fake_list)
    response = await client.get(
        "/api/v1/agent-evolution/experiences/trajectories",
        params={
            "experience_uri": "viking://user/default/memories/experiences/exchange.md",
            "start_date": "2026-08-01",
            "end_date": "2026-08-10",
        },
    )

    assert response.status_code == 200
    assert captured["start_date"] == "2026-08-01"
    assert captured["end_date"] == "2026-08-10"


async def test_list_experience_trajectories_rejects_limit_above_1000(
    client: httpx.AsyncClient,
):
    response = await client.get(
        "/api/v1/agent-evolution/experiences/trajectories",
        params={
            "experience_uri": "viking://user/default/memories/experiences/exchange.md",
            "limit": 1001,
        },
    )

    assert response.status_code == 400


async def test_get_experience_outcome_distribution(
    client: httpx.AsyncClient,
    service,
    monkeypatch,
):
    captured = {}

    async def fake_get(*, experience_uri, ctx, start_date, end_date):
        captured.update(
            experience_uri=experience_uri,
            ctx=ctx,
            start_date=start_date,
            end_date=end_date,
        )
        return {
            "experience_uri": experience_uri,
            "outcome_distribution": [{"outcome": "success", "count": 2}],
        }

    monkeypatch.setattr(
        service.agent_evolution,
        "get_experience_outcome_distribution",
        fake_get,
    )
    uri = "viking://user/default/memories/experiences/exchange.md"

    response = await client.get(
        "/api/v1/agent-evolution/experiences/outcomes",
        params={"experience_uri": uri},
    )

    assert response.status_code == 200
    assert response.json()["result"] == {
        "experience_uri": uri,
        "outcome_distribution": [{"outcome": "success", "count": 2}],
    }
    assert captured["experience_uri"] == uri
    assert captured["start_date"] is None
    assert captured["end_date"] is None


async def test_get_experience_outcome_distribution_passes_date_range(
    client: httpx.AsyncClient,
    service,
    monkeypatch,
):
    captured = {}

    async def fake_get(**kwargs):
        captured.update(kwargs)
        return {
            "experience_uri": kwargs["experience_uri"],
            "outcome_distribution": [],
        }

    monkeypatch.setattr(
        service.agent_evolution,
        "get_experience_outcome_distribution",
        fake_get,
    )
    response = await client.get(
        "/api/v1/agent-evolution/experiences/outcomes",
        params={
            "experience_uri": "viking://user/default/memories/experiences/exchange.md",
            "start_date": "2026-08-01",
            "end_date": "2026-08-10",
        },
    )

    assert response.status_code == 200
    assert captured["start_date"] == "2026-08-01"
    assert captured["end_date"] == "2026-08-10"


async def test_learn_experiences_background_task_exposes_live_progress(
    client, service, monkeypatch
):
    import asyncio

    entered, finish = asyncio.Event(), asyncio.Event()
    uri = "viking://user/default/memories/trajectories/case_a/a.md"
    partial = {"total": 2, "completed": 1, "running": 1, "applied": 1, "no_op": 0, "failed": 0}
    final = {**partial, "completed": 2, "running": 0, "no_op": 1}

    async def learn(**kwargs):
        assert kwargs["trajectory_uris"] == [uri]
        await kwargs["on_progress"](partial)
        entered.set()
        await finish.wait()
        await kwargs["on_progress"](final)
        return {"case_count": 2, "applied_count": 1, "no_op_count": 1, "failed_count": 0}

    monkeypatch.setattr(
        service.agent_evolution._session_compressor, "learn_pending_experiences", learn
    )
    response = await client.post(
        "/api/v1/agent-evolution/experiences/learn",
        json={"trajectory_uris": [uri], "wait": False},
    )
    assert response.status_code == 200
    task_id = response.json()["result"]["task_id"]
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task = (await client.get(f"/api/v1/tasks/{task_id}")).json()["result"]
        assert task["status"] == "running"
        assert task["meta"]["progress"] == partial
        finish.set()
        await asyncio.wait_for(asyncio.gather(*service.agent_evolution._learning_tasks), 2)
        task = (await client.get(f"/api/v1/tasks/{task_id}")).json()["result"]
        assert task["status"] == "completed"
        assert task["meta"]["progress"] == final
        assert task["result"]["case_count"] == 2
    finally:
        finish.set()
        await service.agent_evolution.close_background_tasks()


async def test_background_learning_failure_is_queryable(client, service, monkeypatch):
    import asyncio

    async def learn(**kwargs):
        raise RuntimeError("planner unavailable")

    monkeypatch.setattr(
        service.agent_evolution._session_compressor, "learn_pending_experiences", learn
    )
    response = await client.post("/api/v1/agent-evolution/experiences/learn", json={"wait": False})
    task_id = response.json()["result"]["task_id"]
    await asyncio.wait_for(asyncio.gather(*service.agent_evolution._learning_tasks), 2)
    task = (await client.get(f"/api/v1/tasks/{task_id}")).json()["result"]
    assert task["status"] == "failed"
    assert task["error"] == "RuntimeError: planner unavailable"


async def test_shutdown_cancels_learning_before_storage_close(client, service, monkeypatch):
    import asyncio

    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def learn(**kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(
        service.agent_evolution._session_compressor, "learn_pending_experiences", learn
    )
    response = await client.post("/api/v1/agent-evolution/experiences/learn", json={"wait": False})
    task_id = response.json()["result"]["task_id"]
    await asyncio.wait_for(entered.wait(), 2)
    await asyncio.wait_for(service.agent_evolution.close_background_tasks(), 2)
    assert cancelled.is_set()
    assert not service.agent_evolution._learning_tasks
    task = (await client.get(f"/api/v1/tasks/{task_id}")).json()["result"]
    assert task["status"] == "cancelled"


async def test_pipeline_sdk_task_progress_roundtrip(client, service, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from openviking.session.train import (
        ExperienceSet,
        OfflinePolicyOptimizationPipeline,
        PipelineContext,
        PolicyApplyResult,
        PolicyUpdatePlan,
        RemoteExperienceLearner,
        RolloutTrainingResult,
    )
    from openviking.session.train.components import experience_learner as learner_module
    from openviking_cli.client.http import AsyncHTTPClient

    progress_seen, finish = asyncio.Event(), asyncio.Event()
    updates = []
    uris = ["viking://user/default/memories/trajectories/case_a/a.md"]

    class Printer:
        def __init__(self, **kwargs):
            assert kwargs["total"] == 1

        def update_counts(self, **kwargs):
            updates.append(kwargs)
            progress_seen.set()

        def finish(self):
            pass

    async def learn(**kwargs):
        assert kwargs["trajectory_uris"] == uris
        assert kwargs["case_names"] is None  # No broad scan of other pending records.
        progress = {"total": 1, "completed": 0, "running": 1, "applied": 0, "no_op": 0, "failed": 0}
        await kwargs["on_progress"](progress)
        await finish.wait()
        await kwargs["on_progress"]({**progress, "completed": 1, "running": 0, "no_op": 1})
        return {
            "case_count": 1,
            "trajectory_count": 1,
            "applied_count": 0,
            "no_op_count": 1,
            "failed_count": 0,
            "results": [],
            "written_experience_uris": [],
        }

    monkeypatch.setattr(
        service.agent_evolution._session_compressor, "learn_pending_experiences", learn
    )
    monkeypatch.setattr(learner_module, "ProgressPrinter", Printer)
    sdk = AsyncHTTPClient(url="http://test")
    sdk._http = client  # Exercise the real SDK over the test app's ASGI transport.
    policies = ExperienceSet("viking://user/default/memories/experiences", [])
    phase_one = RolloutTrainingResult(
        analyses=[],
        gradients=[],
        plan=PolicyUpdatePlan(),
        apply_result=PolicyApplyResult(
            updated_policy_set=policies,
            metadata={
                "commit_results": [{"trajectory_uris": uris}],
            },
        ),
    )
    pipeline = OfflinePolicyOptimizationPipeline(
        snapshotter=None,
        rollout_executor=None,
        policy_trainer=SimpleNamespace(train_rollouts=AsyncMock(return_value=phase_one)),
        experience_learner=RemoteExperienceLearner(sdk, poll_interval_seconds=0.001),
    )
    task = asyncio.create_task(pipeline.train_from_rollouts([], policies, PipelineContext()))
    try:
        await asyncio.wait_for(progress_seen.wait(), 2)
        assert updates == [{"completed": 0, "failed": 0, "running": 1}]
        assert not task.done()
        finish.set()
        result = await asyncio.wait_for(task, 2)
        assert result.apply_result.errors == []
        assert result.metadata["experience_learning"]["no_op_count"] == 1
        assert updates[-1] == {"completed": 1, "failed": 0, "running": 0}
    finally:
        finish.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await service.agent_evolution.close_background_tasks()


async def test_commit_completes_after_lenient_trajectory_failure_and_keeps_archive(
    service,
    monkeypatch,
):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from openviking.message import TextPart
    from openviking.server.identity import RequestContext, Role
    from openviking.service.task_tracker import get_task_tracker
    from openviking.session.compressor_v3 import ExperienceTrainingError
    from openviking.session.train import Case, Rubric
    from openviking_cli.session.user_id import UserIdentifier

    ctx = RequestContext(user=UserIdentifier.the_default_user("test_user"), role=Role.USER)
    session = await service.sessions.create(ctx, session_id="lenient-commit")
    session.add_message("user", [TextPart("Hello, this is a test message.")])
    session._agent_evolution_enabled_provider = AsyncMock(return_value=True)
    compressor = session._session_compressor
    monkeypatch.setattr(
        compressor,
        "_extract_user_memories",
        AsyncMock(
            return_value=SimpleNamespace(
                contexts=[],
                cases=[Case("case", "signature", {}, Rubric("rubric", "", []))],
                case_uri_by_name={},
                memory_diff=None,
            )
        ),
    )
    training = AsyncMock(side_effect=ExperienceTrainingError("Trajectory parse failed"))
    monkeypatch.setattr(compressor, "train_from_extracted_cases", training)
    submission = await session.commit_async(
        memory_policy={
            "memory_types": ["cases", "trajectories"],
            "working_memory": {"enabled": False},
        }
    )

    async def terminal_task():
        while True:
            task = await get_task_tracker().get(submission["task_id"])
            if task.status.value in {"completed", "failed", "cancelled"}:
                return task
            await asyncio.sleep(0.01)

    task = await asyncio.wait_for(terminal_task(), 5)
    training.assert_awaited_once()
    assert task.status.value == "completed"
    assert task.result["memories_extracted"] == {}
    assert task.result["trajectory_uris"] == []
    assert await session._archive_file_exists(submission["archive_uri"], ".done")
    assert not await session._archive_file_exists(submission["archive_uri"], ".failed.json")
    archived = await session._viking_fs.read_file(
        f"{submission['archive_uri']}/messages.jsonl",
        ctx=session.ctx,
    )
    assert "Hello, this is a test message." in archived
