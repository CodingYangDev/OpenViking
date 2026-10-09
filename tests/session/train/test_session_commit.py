# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

from __future__ import annotations

import asyncio
import json
import random
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import httpx
import pytest

from openviking.message import Message, TextPart
from openviking.session.train.components import session_commit
from openviking.session.train.domain import Case, ExperienceSet, Rollout, Rubric
from openviking.session.train.utils import summarize_session_commits
from openviking_cli.client.http import AsyncHTTPClient
from openviking_cli.exceptions import (
    AlreadyExistsError,
    InvalidArgumentError,
    NotFoundError,
    PermissionDeniedError,
    UnauthenticatedError,
)


@pytest.fixture
def commit_retry(monkeypatch):
    sleep = AsyncMock()
    jitter = Mock(side_effect=lambda low, high: high)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    monkeypatch.setattr(random, "uniform", jitter)
    client = SimpleNamespace(
        create_session=AsyncMock(),
        get_session=AsyncMock(side_effect=NotFoundError("session")),
        batch_add_messages=AsyncMock(),
        commit_session=AsyncMock(return_value={"task_id": "task-1", "trace_id": "trace-1"}),
        get_task=AsyncMock(return_value={"status": "completed", "result": {}}),
    )
    events = SimpleNamespace(record=AsyncMock())
    trainer = session_commit.SessionCommitPolicyTrainer(
        client=client, run_id="retry-test", event_recorder=events
    )
    rollout = Rollout(
        case=Case("booking", "booking", {}, Rubric("booking", "Book a flight", [])),
        messages=[Message(id="m1", role="user", parts=[TextPart(text="Book a flight")])],
        policy_snapshot_id="snapshot-1",
    )
    return SimpleNamespace(
        trainer=trainer, client=client, rollout=rollout, sleep=sleep, jitter=jitter, events=events
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("show_progress", [True, False])
async def test_commit_summary_counts_successful_outputs_once_without_changing_errors(
    commit_retry, monkeypatch, capsys, show_progress
):
    state = commit_retry
    results = [
        {"task_status": "completed", "error": None, "trajectory_uris": ["traj-a", "traj-b"]},
        {"task_status": "completed", "error": None, "trajectory_uris": []},
        {"task_status": "completed", "error": None, "trajectory_uris": [""]},
        {"task_status": "failed", "error": "request failed", "trajectory_uris": ["partial"]},
        {"task_status": "skipped", "error": None, "trajectory_uris": []},
        {"task_status": None, "error": None, "trajectory_uris": ["traj-a"]},
    ]
    seen = []

    async def commit_one(trainer, rollout, index, **kwargs):
        seen.append(index)
        return results[index]

    monkeypatch.setattr(session_commit.SessionCommitPolicyTrainer, "_commit_one", commit_one)
    state.trainer.show_progress = show_progress

    result = await state.trainer.train_rollouts(
        [state.rollout] * len(results), ExperienceSet("", [])
    )

    assert sorted(seen) == list(range(len(results)))
    assert result.apply_result.metadata["commit_summary"] == {
        "completed": 4,
        "with_traj": 2,
        "without_traj": 2,
        "errors": 1,
    }
    assert result.apply_result.errors == ["request failed"]
    assert result.apply_result.metadata["committed_rollout_count"] == 5
    assert result.apply_result.metadata["skipped_rollout_count"] == 1
    assert result.apply_result.metadata["commit_results"] == results
    assert result.apply_result.metadata["trajectory_uris"] == ["traj-a", "traj-b", "partial"]
    lines = [
        line for line in capsys.readouterr().out.splitlines() if line.startswith("[TRAIN_COMMIT]")
    ]
    assert lines == (
        ["[TRAIN_COMMIT] completed=4 with_traj=2 without_traj=2 errors=1"] if show_progress else []
    )


@pytest.mark.parametrize(
    ("results", "expected"),
    [
        ([], {"completed": 0, "with_traj": 0, "without_traj": 0, "errors": 0}),
        (
            [{"task_status": "skipped", "trajectory_uris": ["ignored"]}],
            {"completed": 0, "with_traj": 0, "without_traj": 0, "errors": 0},
        ),
        (
            [{"trajectory_uris": None}],
            {"completed": 1, "with_traj": 0, "without_traj": 1, "errors": 0},
        ),
        (
            [{"error": "same error"}, {"error": "same error"}],
            {"completed": 0, "with_traj": 0, "without_traj": 0, "errors": 2},
        ),
        (
            [{"trajectory_uris": ["a", "a"]}, {"trajectory_uris": ["a"]}],
            {"completed": 2, "with_traj": 2, "without_traj": 0, "errors": 0},
        ),
    ],
)
def test_commit_summary_empty_skipped_failed_and_duplicate_uris(results, expected):
    assert summarize_session_commits(results) == expected


@pytest.mark.asyncio
async def test_cancelled_commit_batch_does_not_print_a_completion_summary(
    commit_retry, monkeypatch, capsys
):
    monkeypatch.setattr(
        session_commit.SessionCommitPolicyTrainer,
        "_commit_one",
        AsyncMock(side_effect=asyncio.CancelledError()),
    )
    commit_retry.trainer.show_progress = True
    with pytest.raises(asyncio.CancelledError):
        await commit_retry.trainer.train_rollouts([commit_retry.rollout], ExperienceSet("", []))
    assert "[TRAIN_COMMIT]" not in capsys.readouterr().out


@pytest.mark.asyncio
async def test_create_session_retries_without_attempt_limit_and_caps_backoff(commit_retry, capsys):
    state = commit_retry
    state.client.create_session.side_effect = [httpx.ReadError("") for _ in range(17)] + [None]

    result = await state.trainer._commit_one(state.rollout, 0)

    assert result["error"] is None
    assert result["task_status"] == "completed"
    assert state.client.create_session.await_count == 18
    assert state.client.get_session.await_args_list == [call(result["session_id"])] * 17
    create_calls = state.client.create_session.await_args_list
    assert create_calls == [create_calls[0]] * 18
    assert create_calls[0].kwargs == {
        "session_id": result["session_id"],
        "options": {"memory_policy": session_commit._training_commit_memory_policy()},
    }
    delays = [0.5, 1.0, 2.0, 4.0, 8.0, 16.0] + [30.0] * 11
    assert state.sleep.await_args_list == [call(delay) for delay in delays]
    assert state.jitter.call_args_list == [call(delay / 2, delay) for delay in delays]
    state.client.batch_add_messages.assert_awaited_once()
    state.client.commit_session.assert_awaited_once()
    assert [c.args[0] for c in state.events.record.await_args_list] == [
        "train_commit_submitted",
        "train_commit_done",
    ]
    assert capsys.readouterr().out == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("session_exists", [False, True])
async def test_create_session_checks_uncertain_outcome_before_recreating(
    commit_retry, session_exists
):
    state = commit_retry
    order = []

    async def create(**kwargs):
        order.append("create")
        if len(order) == 1:
            raise httpx.ReadError("")

    async def get(session_id):
        order.append("get")
        if not session_exists:
            raise NotFoundError(session_id)
        return {"session_id": session_id}

    state.client.create_session.side_effect = create
    state.client.get_session.side_effect = get
    result = await state.trainer._commit_one(state.rollout, 0)

    assert result["error"] is None
    assert order == (["create", "get"] if session_exists else ["create", "get", "create"])
    state.client.batch_add_messages.assert_awaited_once()
    state.client.commit_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_session_retries_read_errors_during_reconciliation(commit_retry):
    state = commit_retry
    state.client.create_session.side_effect = httpx.ReadError("")
    state.client.get_session.side_effect = [httpx.ReadError("") for _ in range(4)] + [{}]

    result = await state.trainer._commit_one(state.rollout, 0)

    assert result["error"] is None
    state.client.create_session.assert_awaited_once()
    assert state.client.get_session.await_count == 5
    assert state.sleep.await_count == 5
    state.client.commit_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_session_recovers_when_previous_request_finishes_after_not_found(commit_retry):
    state = commit_retry
    state.client.create_session.side_effect = [httpx.ReadError(""), AlreadyExistsError("session")]
    state.client.get_session.side_effect = [NotFoundError("session"), {}]

    result = await state.trainer._commit_one(state.rollout, 0)

    assert result["error"] is None
    assert state.client.create_session.await_count == 2
    assert state.client.get_session.await_count == 2
    state.client.commit_session.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("during_reconciliation", [False, True])
@pytest.mark.parametrize(
    "error",
    [
        InvalidArgumentError("invalid"),
        PermissionDeniedError("denied"),
        UnauthenticatedError("unauthenticated"),
        AlreadyExistsError("session"),
        httpx.ReadTimeout("timed out"),
    ],
)
async def test_create_session_does_not_retry_other_errors(
    commit_retry, during_reconciliation, error
):
    state = commit_retry
    state.client.create_session.side_effect = (
        httpx.ReadError("") if during_reconciliation else error
    )
    state.client.get_session.side_effect = error

    result = await state.trainer._commit_one(state.rollout, 0)

    assert result["error"] == f"{type(error).__name__}: {error}"
    assert result["stage"] == "create_session"
    assert result["task_status"] == "failed"
    state.client.create_session.assert_awaited_once()
    assert state.sleep.await_count == int(during_reconciliation)
    assert state.client.get_session.await_count == int(during_reconciliation)
    state.client.batch_add_messages.assert_not_awaited()
    state.client.commit_session.assert_not_awaited()
    assert state.events.record.await_args.args == ("train_commit_failed",)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["create_session", "get_session", "sleep"])
async def test_create_session_retry_propagates_cancellation(commit_retry, method):
    state = commit_retry
    state.client.create_session.side_effect = httpx.ReadError("")
    cancelled = asyncio.CancelledError()
    operation = state.sleep if method == "sleep" else getattr(state.client, method)
    operation.side_effect = cancelled

    with pytest.raises(asyncio.CancelledError) as caught:
        await state.trainer._commit_one(state.rollout, 0)

    assert caught.value is cancelled
    state.client.batch_add_messages.assert_not_awaited()
    state.client.commit_session.assert_not_awaited()
    state.events.record.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_session_can_be_cancelled_while_backing_off(commit_retry, monkeypatch):
    state = commit_retry
    state.client.create_session.side_effect = httpx.ReadError("")
    sleeping = asyncio.Event()

    async def wait(delay):
        sleeping.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(asyncio, "sleep", wait)
    task = asyncio.create_task(state.trainer._commit_one(state.rollout, 0))
    try:
        await asyncio.wait_for(sleeping.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    state.client.commit_session.assert_not_awaited()
    state.events.record.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["batch_add_messages", "commit_session"])
async def test_create_session_retry_does_not_replay_later_commit_stages(commit_retry, method):
    state = commit_retry
    getattr(state.client, method).side_effect = httpx.ReadError("")

    result = await state.trainer._commit_one(state.rollout, 0)

    assert result["error"] == "ReadError: "
    assert result["task_status"] == "failed"
    state.client.create_session.assert_awaited_once()
    getattr(state.client, method).assert_awaited_once()
    state.client.get_session.assert_not_awaited()
    state.sleep.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["missing", "created", "late_create"])
async def test_create_session_retry_handles_real_http_client_errors(commit_retry, outcome):
    requests = []
    session_id = "retry-session"

    async def handle(request):
        requests.append(request.method)
        if len(requests) == 1:
            raise httpx.ReadError("", request=request)
        error = None
        if requests == ["POST", "GET"] and outcome != "created":
            error = "NOT_FOUND"
        elif request.method == "POST" and outcome == "late_create":
            error = "ALREADY_EXISTS"
        if error:
            return httpx.Response(
                404 if error == "NOT_FOUND" else 409,
                json={
                    "status": "error",
                    "error": {"code": error, "details": {"resource": session_id}},
                },
            )
        return httpx.Response(200, json={"status": "ok", "result": {"session_id": session_id}})

    client = AsyncHTTPClient(url="http://test", api_key="test", timeout=1)
    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(handle)
    ) as http:
        client._http = http
        commit_retry.trainer.client = client
        await commit_retry.trainer._create_session(session_id)

    expected = {
        "missing": ["POST", "GET", "POST"],
        "created": ["POST", "GET"],
        "late_create": ["POST", "GET", "POST", "GET"],
    }
    assert requests == expected[outcome]


@pytest.mark.asyncio
async def test_wait_task_retries_without_limit_and_never_replays_commit(commit_retry, capsys):
    state = commit_retry
    trajectory = "viking://user/default/memories/trajectories/one.md"
    state.client.get_task.side_effect = [httpx.ReadError("") for _ in range(17)] + [
        {"status": "completed", "result": {"trajectory_uris": [trajectory]}}
    ]

    result = await state.trainer._commit_one(state.rollout, 0)

    assert result["error"] is None
    assert result["task_status"] == "completed"
    assert result["trajectory_uris"] == [trajectory]
    assert state.client.get_task.await_args_list == [call("task-1")] * 18
    state.client.create_session.assert_awaited_once()
    state.client.batch_add_messages.assert_awaited_once()
    state.client.commit_session.assert_awaited_once()
    state.client.get_session.assert_not_awaited()
    delays = [0.5, 1.0, 2.0, 4.0, 8.0, 16.0] + [30.0] * 11
    assert state.sleep.await_args_list == [call(delay) for delay in delays]
    assert state.jitter.call_args_list == [call(delay / 2, delay) for delay in delays]
    assert [c.args[0] for c in state.events.record.await_args_list] == [
        "train_commit_submitted",
        "train_commit_done",
    ]
    assert capsys.readouterr().out == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("pending", [None, {"status": "pending"}, {"status": "running"}])
async def test_wait_task_resets_backoff_after_successful_query(commit_retry, pending):
    state = commit_retry
    state.client.get_task.side_effect = [
        httpx.ReadError(""),
        httpx.ReadError(""),
        pending,
        httpx.ReadError(""),
        {"status": "completed"},
    ]

    result = await state.trainer._wait_task("task-1")

    assert result == {"status": "completed"}
    assert state.sleep.await_args_list == [call(0.5), call(1.0), call(2.0), call(0.5)]
    assert state.jitter.call_args_list == [call(0.25, 0.5), call(0.5, 1.0), call(0.25, 0.5)]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
async def test_wait_task_returns_terminal_status_after_read_error(commit_retry, status):
    state = commit_retry
    terminal = {"status": status, "error": "server failure" if status == "failed" else None}
    state.client.get_task.side_effect = [httpx.ReadError(""), terminal]

    result = await state.trainer._wait_task("task-1")

    assert result is terminal
    assert state.client.get_task.await_count == 2
    state.sleep.assert_awaited_once_with(0.5)


@pytest.mark.asyncio
@pytest.mark.parametrize("after_read_error", [False, True])
@pytest.mark.parametrize(
    "error",
    [
        InvalidArgumentError("invalid"),
        PermissionDeniedError("denied"),
        UnauthenticatedError("unauthenticated"),
        httpx.ReadTimeout("timed out"),
    ],
)
async def test_wait_task_does_not_retry_other_errors(commit_retry, after_read_error, error):
    state = commit_retry
    state.client.get_task.side_effect = ([httpx.ReadError("")] if after_read_error else []) + [
        error
    ]

    result = await state.trainer._commit_one(state.rollout, 0)

    assert result["stage"] == "wait_task"
    assert result["error"] == f"{type(error).__name__}: {error}"
    assert result["task_status"] == "failed"
    assert state.client.get_task.await_count == 1 + int(after_read_error)
    assert state.sleep.await_count == int(after_read_error)
    state.client.create_session.assert_awaited_once()
    state.client.commit_session.assert_awaited_once()
    assert state.events.record.await_args.args == ("train_commit_failed",)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["get_task", "sleep"])
async def test_wait_task_retry_propagates_cancellation(commit_retry, method):
    state = commit_retry
    state.client.get_task.side_effect = httpx.ReadError("")
    cancelled = asyncio.CancelledError()
    operation = state.sleep if method == "sleep" else state.client.get_task
    operation.side_effect = cancelled

    with pytest.raises(asyncio.CancelledError) as caught:
        await state.trainer._commit_one(state.rollout, 0)

    assert caught.value is cancelled
    state.client.commit_session.assert_awaited_once()
    assert [c.args[0] for c in state.events.record.await_args_list] == ["train_commit_submitted"]


@pytest.mark.asyncio
async def test_wait_task_can_be_cancelled_while_backing_off(commit_retry, monkeypatch):
    state = commit_retry
    state.client.get_task.side_effect = httpx.ReadError("")
    sleeping = asyncio.Event()

    async def wait(delay):
        sleeping.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(asyncio, "sleep", wait)
    task = asyncio.create_task(state.trainer._commit_one(state.rollout, 0))
    try:
        await asyncio.wait_for(sleeping.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    state.client.commit_session.assert_awaited_once()
    assert [c.args[0] for c in state.events.record.await_args_list] == ["train_commit_submitted"]


@pytest.mark.asyncio
@pytest.mark.parametrize("query_seconds, delays", [(0.0, [0.5, 0.5]), (0.75, [0.25]), (1.25, [])])
async def test_wait_task_retry_preserves_total_deadline(
    commit_retry, monkeypatch, query_seconds, delays
):
    state = commit_retry
    state.trainer.timeout_seconds = 1.0
    now = 0.0

    async def query(task_id):
        nonlocal now
        now += query_seconds
        raise httpx.ReadError("")

    async def sleep(delay):
        nonlocal now
        now += delay

    state.client.get_task.side_effect = query
    state.sleep.side_effect = sleep
    loop = SimpleNamespace(time=lambda: now)
    monkeypatch.setattr(
        session_commit,
        "asyncio",
        SimpleNamespace(
            get_running_loop=lambda: loop,
            sleep=state.sleep,
        ),
    )

    result = await state.trainer._wait_task("task-1")

    assert result == {"task_id": "task-1", "status": "timeout", "error": "commit task timeout"}
    assert state.sleep.await_args_list == [call(delay) for delay in delays]
    assert state.client.get_task.await_count == max(1, len(delays))
    assert now == max(1.0, query_seconds)


@pytest.mark.asyncio
async def test_wait_task_retry_handles_real_http_client_errors(commit_retry):
    requests = []

    async def handle(request):
        requests.append((request.method, request.url.path))
        if len(requests) == 1:
            raise httpx.ReadError("", request=request)
        return httpx.Response(200, json={"status": "ok", "result": {"status": "completed"}})

    client = AsyncHTTPClient(url="http://test", api_key="test", timeout=1)
    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(handle)
    ) as http:
        client._http = http
        commit_retry.trainer.client = client
        result = await commit_retry.trainer._wait_task("task-1")

    assert result == {"status": "completed"}
    assert requests == [("GET", "/api/v1/tasks/task-1")] * 2


@pytest.mark.asyncio
async def test_commit_and_artifact_export_share_exact_request_messages(commit_retry, tmp_path):
    from openviking.session.train.components.rollout_artifact_recorder import (
        RolloutArtifactRecorder,
    )

    state = commit_retry
    state.rollout.messages[0].created_at = "2026-10-09T00:00:00+00:00"
    expected = session_commit.build_commit_messages(state.rollout)
    result = await state.trainer._commit_one(state.rollout, 0)
    assert result["error"] is None
    state.client.batch_add_messages.assert_awaited_once()
    assert state.client.batch_add_messages.await_args.args[1] == expected

    recorder = RolloutArtifactRecorder(run_dir=tmp_path)
    recorder.record_train_rollouts(epoch=0, rollouts=[state.rollout])
    [export_path] = list(tmp_path.rglob("commit_messages.json"))
    assert json.loads(export_path.read_text()) == expected
