from __future__ import annotations

import asyncio
import threading

import httpx
import pytest
from test_dataset_service_cancellation import app_for, payload, terminal

from openviking.session.train.domain import Rollout


def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def wait_event(event):
    async def wait():
        while not event.is_set():
            await asyncio.sleep(0.002)

    await asyncio.wait_for(wait(), 2)


@pytest.mark.asyncio
async def test_hosted_worker_loops_share_a_separate_bounded_inner_executor():
    barrier = threading.Barrier(2)
    observed = []

    def inner():
        barrier.wait(timeout=3)
        return threading.get_ident()

    class Executor:
        async def execute(self, cases, policy_set, context):
            del policy_set
            loop = asyncio.get_running_loop()
            outer_thread = threading.get_ident()
            inner_thread = await asyncio.to_thread(inner)
            observed.append((loop, outer_thread, inner_thread, loop._default_executor))
            return [Rollout(cases[0], [], context.policy_snapshot_id)]

    app = app_for(Executor(), workers=2, concurrency=2)
    async with client_for(app) as client:
        try:
            ids = [
                (await client.post("/v1/rollouts/execute", json=payload(5))).json()["execution_id"]
                for _ in range(2)
            ]
            results = await asyncio.gather(*(terminal(client, ident) for ident in ids))
            assert all(result["status"] == "completed" for result in results)
            assert len({id(item[0]) for item in observed}) == 2
            assert len({item[1] for item in observed}) == 2
            assert {item[1] for item in observed}.isdisjoint(item[2] for item in observed)
            assert len({id(item[3]) for item in observed}) == 1
            assert all(item[3] is app.state.rollout_inner_thread_pool for item in observed)
            assert app.state.rollout_inner_thread_pool is not app.state.rollout_thread_pool
            assert app.state.rollout_inner_thread_pool._max_workers == 2
        finally:
            barrier.abort()
            await app.router.shutdown()


@pytest.mark.asyncio
async def test_inner_fanout_across_worker_loops_respects_one_service_wide_limit():
    release = threading.Event()
    submitted = threading.Event()
    saturated = threading.Event()
    lock = threading.Lock()
    state = {"active": 0, "peak": 0, "submitted": 0, "completed": 0}

    def inner():
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            if state["active"] >= 2:
                saturated.set()
        try:
            assert release.wait(timeout=3)
        finally:
            with lock:
                state["active"] -= 1
                state["completed"] += 1

    class Executor:
        async def execute(self, cases, policy_set, context):
            del policy_set
            tasks = [asyncio.create_task(asyncio.to_thread(inner)) for _ in range(3)]
            # Let every to_thread task submit before declaring this worker ready.
            await asyncio.sleep(0)
            with lock:
                state["submitted"] += 1
                if state["submitted"] == 2:
                    submitted.set()
            await asyncio.gather(*tasks)
            return [Rollout(cases[0], [], context.policy_snapshot_id)]

    app = app_for(Executor(), workers=2, concurrency=2)
    async with client_for(app) as client:
        try:
            ids = [
                (await client.post("/v1/rollouts/execute", json=payload(5))).json()["execution_id"]
                for _ in range(2)
            ]
            await wait_event(submitted)
            await wait_event(saturated)
            release.set()
            results = await asyncio.gather(*(terminal(client, ident) for ident in ids))
            assert all(result["status"] == "completed" for result in results)
            assert state["completed"] == 6
            assert state["peak"] == 2
        finally:
            release.set()
            await app.router.shutdown()


@pytest.mark.asyncio
async def test_worker_loop_and_shared_pool_remain_usable_after_failure():
    observed = []

    class Executor:
        async def execute(self, cases, policy_set, context):
            del policy_set
            await asyncio.to_thread(lambda: None)
            loop = asyncio.get_running_loop()
            observed.append((loop, loop._default_executor))
            if len(observed) == 1:
                raise RuntimeError("deliberate first-run failure")
            return [Rollout(cases[0], [], context.policy_snapshot_id)]

    app = app_for(Executor(), workers=1)
    async with client_for(app) as client:
        try:
            results = []
            for _ in range(2):
                ident = (await client.post("/v1/rollouts/execute", json=payload(5))).json()[
                    "execution_id"
                ]
                results.append(await terminal(client, ident))
            assert [result["status"] for result in results] == ["failed", "completed"]
            assert observed[0][0] is observed[1][0]
            assert observed[0][1] is observed[1][1] is app.state.rollout_inner_thread_pool
        finally:
            await app.router.shutdown()


@pytest.mark.asyncio
async def test_shutdown_allows_worker_cleanup_before_closing_both_pools():
    started = threading.Event()
    cleaned = threading.Event()

    class Executor:
        async def execute(self, cases, policy_set, context):
            del cases, policy_set, context
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.to_thread(cleaned.set)

    app = app_for(Executor(), workers=1)
    async with client_for(app) as client:
        try:
            ident = (await client.post("/v1/rollouts/execute", json=payload(5))).json()[
                "execution_id"
            ]
            await wait_event(started)
            await asyncio.wait_for(app.router.shutdown(), 2)
            assert cleaned.is_set()
            assert app.state.rollout_thread_pool._shutdown
            assert app.state.rollout_inner_thread_pool._shutdown
            assert not app.state.rollout_tasks and not app.state.rollout_controls
            assert (await terminal(client, ident))["status"] == "failed"
        finally:
            await app.router.shutdown()


@pytest.mark.asyncio
async def test_shutdown_cancels_queued_inner_work_without_claiming_to_kill_running_io():
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    queued_ran = threading.Event()

    def blocking():
        started.set()
        try:
            assert release.wait(timeout=3)
        finally:
            finished.set()

    class Executor:
        async def execute(self, cases, policy_set, context):
            del cases, policy_set, context
            await asyncio.gather(asyncio.to_thread(blocking), asyncio.to_thread(queued_ran.set))

    app = app_for(Executor(), workers=1)
    async with client_for(app) as client:
        try:
            await client.post("/v1/rollouts/execute", json=payload(5))
            await wait_event(started)
            await asyncio.wait_for(app.router.shutdown(), 2)
            assert not queued_ran.is_set()
            assert not finished.is_set()  # Cancellation cannot terminate running sync I/O.
            assert app.state.rollout_inner_thread_pool._shutdown
            release.set()
            await wait_event(finished)
            assert not queued_ran.is_set()
        finally:
            release.set()
            await app.router.shutdown()


@pytest.mark.asyncio
async def test_non_threaded_service_preserves_the_callers_default_executor():
    loop = asyncio.get_running_loop()
    await asyncio.to_thread(lambda: None)
    original = loop._default_executor

    class Executor:
        async def execute(self, cases, policy_set, context):
            del policy_set
            await asyncio.to_thread(lambda: None)
            assert asyncio.get_running_loop() is loop
            return [Rollout(cases[0], [], context.policy_snapshot_id)]

    app = app_for(Executor(), workers=None)
    async with client_for(app) as client:
        try:
            assert app.state.rollout_thread_pool is None
            assert app.state.rollout_inner_thread_pool is None
            ident = (await client.post("/v1/rollouts/execute", json=payload(5))).json()[
                "execution_id"
            ]
            assert (await terminal(client, ident))["status"] == "completed"
        finally:
            await app.router.shutdown()
    assert loop._default_executor is original
    assert not original._shutdown


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_cancelled_worker_waits_for_cleanup_queued_behind_running_inner_io(cancel):
    started = threading.Event()
    release = threading.Event()
    cleanup_waiting = threading.Event()
    cleaned = threading.Event()

    def blocking():
        started.set()
        assert release.wait(timeout=3)

    class Executor:
        async def execute(self, cases, policy_set, context):
            del cases, policy_set, context
            try:
                await asyncio.to_thread(blocking)
            finally:
                cleanup_waiting.set()
                await asyncio.to_thread(cleaned.set)

    app = app_for(Executor(), workers=1)
    async with client_for(app) as client:
        try:
            ident = (
                await client.post("/v1/rollouts/execute", json=payload(5 if cancel else 0.06))
            ).json()["execution_id"]
            await wait_event(started)
            if cancel:
                await client.post(f"/v1/rollouts/executions/{ident}/cancel")
            await wait_event(cleanup_waiting)
            assert not cleaned.is_set()
            # Do not report terminal state before the queued cleanup actually finishes.
            assert (await client.get(f"/v1/rollouts/executions/{ident}")).json()[
                "status"
            ] == "running"
            release.set()
            result = await terminal(client, ident)
            assert result["status"] == "failed"
            assert ("CancelledError" if cancel else "TimeoutError") in result["error"]
            assert cleaned.is_set()
        finally:
            release.set()
            await app.router.shutdown()


@pytest.mark.asyncio
async def test_shutting_down_one_service_does_not_close_another_services_inner_pool():
    class Executor:
        async def execute(self, cases, policy_set, context):
            del policy_set
            await asyncio.to_thread(lambda: None)
            return [Rollout(cases[0], [], context.policy_snapshot_id)]

    first = app_for(Executor(), workers=1)
    second = app_for(Executor(), workers=1)
    try:
        assert first.state.rollout_inner_thread_pool is not second.state.rollout_inner_thread_pool
        await first.router.shutdown()
        async with client_for(second) as client:
            ident = (await client.post("/v1/rollouts/execute", json=payload(5))).json()[
                "execution_id"
            ]
            assert (await terminal(client, ident))["status"] == "completed"
        assert not second.state.rollout_inner_thread_pool._shutdown
    finally:
        await first.router.shutdown()
        await second.router.shutdown()
