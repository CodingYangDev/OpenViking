from __future__ import annotations

import asyncio
import threading
import time

import httpx
import pytest

from openviking.session.train.components.dataset_service import (
    RolloutExecuteRequest,
    case_to_dict,
    create_dataset_service_app,
    policy_set_to_dict,
)
from openviking.session.train.components.remote import RemoteRolloutExecutor
from openviking.session.train.context import ExecutionContext
from openviking.session.train.domain import Case, ExperienceSet, Rollout, Rubric


def case():
    return Case(
        name="offline",
        task_signature="offline",
        input={},
        rubric=Rubric(name="rubric", description="", criteria=[]),
    )


def policy():
    return ExperienceSet(root_uri="viking://offline", policies=[])


def payload(timeout=1):
    return {
        "case": case_to_dict(case()),
        "policy_set": policy_set_to_dict(policy()),
        "execution_context": {"policy_snapshot_id": "test"},
        "execution_timeout_seconds": timeout,
    }


async def terminal(client, execution_id):
    async def wait():
        while True:
            data = (await client.get(f"/v1/rollouts/executions/{execution_id}")).json()
            if data["status"] != "running":
                return data
            await asyncio.sleep(0.002)

    return await asyncio.wait_for(wait(), 2)


class WaitingExecutor:
    def __init__(self):
        self.started = threading.Event()
        self.cancelled = threading.Event()
        self.cleaned = threading.Event()
        self.calls = 0

    async def execute(self, cases, policy_set, context):
        self.calls += 1
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        finally:
            await asyncio.sleep(0.015)
            self.cleaned.set()


def app_for(executor, workers, concurrency=1):
    return create_dataset_service_app(
        service_name="test",
        make_case_loader=lambda *a: None,
        make_rollout_executor=lambda options: executor,
        rollout_thread_workers=workers,
        max_rollout_concurrency=concurrency,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("workers", [None, 1])
@pytest.mark.parametrize("cancel", [False, True])
async def test_deadline_and_cancel_reach_actual_executor_and_wait_for_cleanup(workers, cancel):
    executor = WaitingExecutor()
    app = app_for(executor, workers)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        try:
            data = (
                await client.post("/v1/rollouts/execute", json=payload(5 if cancel else 0.06))
            ).json()
            execution_id = data["execution_id"]
            while not executor.started.is_set():
                await asyncio.sleep(0.002)
            if cancel:
                await client.post(f"/v1/rollouts/executions/{execution_id}/cancel")
            result = await terminal(client, execution_id)
            assert result["status"] == "failed"
            assert ("CancelledError" if cancel else "TimeoutError") in result["error"]
            assert executor.cancelled.is_set() and executor.cleaned.is_set()
            assert not app.state.rollout_tasks and not app.state.rollout_controls

            class SuccessExecutor:
                async def execute(self, cases, policy_set, context):
                    return [
                        Rollout(
                            case=cases[0],
                            messages=[],
                            policy_snapshot_id=context.policy_snapshot_id,
                        )
                    ]

            app.state.make_rollout_executor = lambda options: SuccessExecutor()
            next_id = (await client.post("/v1/rollouts/execute", json=payload())).json()[
                "execution_id"
            ]
            assert (await terminal(client, next_id))["status"] == "completed"
            # A terminal cancellation is idempotent.
            assert (await client.post(f"/v1/rollouts/executions/{execution_id}/cancel")).json()[
                "status"
            ] == "failed"
        finally:
            await app.router.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("workers", [None, 1])
async def test_immediate_cancel_before_task_start_does_not_leave_running_record(workers):
    executor = WaitingExecutor()
    app = app_for(executor, workers)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        try:
            data = (await client.post("/v1/rollouts/execute", json=payload())).json()
            await client.post(f"/v1/rollouts/executions/{data['execution_id']}/cancel")
            await client.post(f"/v1/rollouts/executions/{data['execution_id']}/cancel")
            assert (await terminal(client, data["execution_id"]))["status"] == "failed"
            assert not app.state.rollout_tasks
        finally:
            await app.router.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrency", [1, None])
async def test_cancel_queued_rollout_does_not_execute_it(concurrency):
    executor = WaitingExecutor()
    app = app_for(executor, 1, concurrency)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        try:
            first = (await client.post("/v1/rollouts/execute", json=payload(5))).json()[
                "execution_id"
            ]
            while not executor.started.is_set():
                await asyncio.sleep(0.002)
            second = (await client.post("/v1/rollouts/execute", json=payload(0.03))).json()[
                "execution_id"
            ]
            result = await terminal(client, second)
            assert result["status"] == "failed" and "TimeoutError" in result["error"]
            assert executor.calls == 1
            await client.post(f"/v1/rollouts/executions/{first}/cancel")
            await terminal(client, first)
            assert executor.calls == 1
        finally:
            await app.router.shutdown()


@pytest.mark.asyncio
async def test_worker_deadline_is_enforced_even_if_service_loop_is_busy():
    executor = WaitingExecutor()
    app = app_for(executor, 1)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        try:
            data = (await client.post("/v1/rollouts/execute", json=payload(0.05))).json()
            while not executor.started.is_set():
                await asyncio.sleep(0.002)
            time.sleep(0.1)  # Deliberately starve only the ASGI loop, not the worker.
            assert executor.cancelled.is_set() and executor.cleaned.is_set()
            assert (await terminal(client, data["execution_id"]))["status"] == "failed"
        finally:
            await app.router.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_remote_client_notifies_server_on_local_timeout_or_cancellation(cancel):
    methods = []
    posted = None
    polling = asyncio.Event()

    async def handle(request):
        nonlocal posted
        methods.append((request.method, request.url.path))
        if request.url.path == "/v1/rollouts/execute":
            import json

            posted = json.loads(request.content)
            return httpx.Response(200, json={"execution_id": "exec"})
        if request.url.path.endswith("/cancel"):
            return httpx.Response(200, json={"status": "running"})
        polling.set()
        await asyncio.Event().wait()

    remote = RemoteRolloutExecutor("http://test", execution_timeout_seconds=10 if cancel else 0.02)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://test"
    ) as client:
        task = asyncio.create_task(
            remote._execute_one(client, case(), policy(), ExecutionContext("test"))
        )
        await polling.wait()
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
            await task
    assert posted["execution_timeout_seconds"] == remote.execution_timeout_seconds
    assert ("POST", "/v1/rollouts/executions/exec/cancel") in methods


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_rejects_invalid_server_deadlines(timeout):
    with pytest.raises(ValueError):
        RolloutExecuteRequest(**payload(timeout))
