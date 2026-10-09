# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

from __future__ import annotations

import asyncio
import random
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest

from openviking.pyagfs import AsyncAGFSClient, get_binding_client
from openviking.pyagfs import async_client as async_client_module
from openviking.storage.errors import LockAcquisitionError
from openviking.utils.agfs_utils import RagfsBindingConfig, mount_agfs_backend
from openviking_cli.utils.config.agfs_config import AGFSConfig

_PATH = "/local/acct/cases/one.md"
_METHODS = [
    ("pathlock_acquire_exact", (_PATH,)),
    ("pathlock_acquire_exact_batch", ([_PATH, "/local/acct/cases/.overview.md"],)),
    ("pathlock_acquire_tree", ("/local/acct/cases",)),
    ("pathlock_acquire_tree_batch", (["/local/acct/cases"],)),
    ("pathlock_acquire_exact_tree_batch", ([_PATH], ["/local/acct/other"])),
    ("pathlock_acquire_batch", ([{"path": _PATH, "kind": "exact"}],)),
]


@pytest.fixture
def adapter():
    client = AsyncAGFSClient(SimpleNamespace())
    lease = {"lease_ref": "child-lease", "ownership_ref": "child-capability"}
    client.run = AsyncMock(return_value=lease)
    return SimpleNamespace(client=client, run=client.run, lease=lease)


@pytest.mark.asyncio
@pytest.mark.parametrize("method,args", _METHODS)
@pytest.mark.parametrize("timeout", [0.0, 300.0])
@pytest.mark.parametrize("explicit_context", [False, True])
async def test_lock_acquisition_uses_zero_native_timeout_and_preserves_context(
    adapter, method, args, timeout, explicit_context
):
    owner = {"lease_ref": "parent-lease", "ownership_ref": "parent-capability"}
    ctx = {"account_id": "explicit-account", "user_id": "user"}
    kwargs = {"fs_ctx": ctx} if explicit_context else {}

    result = await getattr(adapter.client, method)(
        *args, timeout_secs=timeout, owner_lease_ref=owner, **kwargs
    )

    assert result is adapter.lease
    adapter.run.assert_awaited_once_with(
        method, ctx if explicit_context else {"account_id": "acct"}, *args, 0.0, owner
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("method,args", _METHODS)
async def test_lock_conflicts_retry_with_capped_jitter_without_logs(
    adapter, method, args, monkeypatch, capsys
):
    adapter.run.side_effect = [LockAcquisitionError("busy") for _ in range(12)] + [adapter.lease]
    sleep = AsyncMock()
    jitter = Mock(side_effect=lambda low, high: high)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    monkeypatch.setattr(random, "uniform", jitter)

    assert await getattr(adapter.client, method)(*args, timeout_secs=300) is adapter.lease

    assert adapter.run.await_count == 13
    assert all(c.args[-2] == 0.0 for c in adapter.run.await_args_list)
    ceilings = [0.1, 0.2, 0.4] + [0.5] * 9
    assert sleep.await_args_list == [call(ceiling) for ceiling in ceilings]
    assert jitter.call_args_list == [call(ceiling / 2, ceiling) for ceiling in ceilings]
    assert capsys.readouterr().out == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("method,args", _METHODS)
async def test_zero_timeout_keeps_single_attempt(adapter, method, args, monkeypatch):
    error = LockAcquisitionError("busy")
    adapter.run.side_effect = error
    sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep)

    with pytest.raises(LockAcquisitionError) as caught:
        await getattr(adapter.client, method)(*args)

    assert caught.value is error
    adapter.run.assert_awaited_once()
    sleep.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("method,args", _METHODS)
@pytest.mark.parametrize("timeout", [-1.0, float("nan"), float("inf"), -float("inf")])
async def test_invalid_timeout_is_rejected_before_native_call(adapter, method, args, timeout):
    with pytest.raises(ValueError, match="finite non-negative"):
        await getattr(adapter.client, method)(*args, timeout_secs=timeout)
    adapter.run.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ValueError("invalid owner"), OSError("storage failed")])
async def test_other_lock_errors_are_not_retried(adapter, error, monkeypatch):
    adapter.run.side_effect = error
    sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep)
    with pytest.raises(type(error)) as caught:
        await adapter.client.pathlock_acquire_exact(_PATH, timeout_secs=300)
    assert caught.value is error
    adapter.run.assert_awaited_once()
    sleep.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("slow_attempt", [False, True])
async def test_lock_retry_obeys_one_deadline(adapter, monkeypatch, slow_attempt):
    real_sleep = asyncio.sleep

    async def fail(*args):
        if slow_attempt:
            await real_sleep(0.03)
        raise LockAcquisitionError("lock acquire timed out after 0ms")

    adapter.run.side_effect = fail
    sleep = AsyncMock(side_effect=real_sleep)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(LockAcquisitionError, match=r"lock acquire timed out after [1-9]\d*ms"):
        await adapter.client.pathlock_acquire_exact(_PATH, timeout_secs=0.02)
    assert loop.time() - started >= 0.02
    adapter.run.assert_awaited_once()
    if slow_attempt:
        sleep.assert_not_awaited()
    else:
        sleep.assert_awaited_once()
        assert 0 < sleep.await_args.args[0] <= 0.02


@pytest.mark.asyncio
async def test_lock_backoff_is_cancellable(adapter, monkeypatch):
    sleeping = asyncio.Event()
    adapter.run.side_effect = LockAcquisitionError("busy")

    async def wait(delay):
        sleeping.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(asyncio, "sleep", wait)
    task = asyncio.create_task(adapter.client.pathlock_acquire_exact(_PATH, timeout_secs=300))
    try:
        await asyncio.wait_for(sleeping.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        adapter.run.assert_awaited_once()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [0.0, 300.0])
@pytest.mark.parametrize("outcome", ["acquired", "conflict", "error", "release_error"])
async def test_cancelled_native_acquisition_drains_and_releases_only_new_lease(timeout, outcome):
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    releasing = asyncio.Event()
    finish_acquire = threading.Event()
    finish_release = threading.Event()
    parent = {"lease_ref": "parent", "ownership_ref": "parent-capability"}
    child = {"lease_ref": "child", "ownership_ref": "child-capability"}
    ctx = {"account_id": "acct", "user_id": "user"}
    released = []

    def acquire(native_ctx, path, native_timeout, owner):
        assert native_ctx == ctx and path == _PATH and owner is parent
        loop.call_soon_threadsafe(entered.set)
        assert finish_acquire.wait(2), "test did not unblock native acquisition"
        if outcome == "conflict":
            raise LockAcquisitionError("busy")
        if outcome == "error":
            raise OSError("storage failed")
        return child

    def release(native_ctx, lease):
        assert native_ctx == ctx and lease is child
        loop.call_soon_threadsafe(releasing.set)
        assert finish_release.wait(2), "test did not unblock native release"
        if outcome == "release_error":
            raise OSError("release failed")
        released.append(lease)

    client = AsyncAGFSClient(
        SimpleNamespace(pathlock_acquire_exact=acquire, pathlock_release=release)
    )
    task = asyncio.create_task(client.pathlock_acquire_exact(_PATH, timeout, parent, fs_ctx=ctx))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "must drain a native call that cannot be interrupted"
        finish_acquire.set()
        if outcome in {"acquired", "release_error"}:
            await asyncio.wait_for(releasing.wait(), timeout=1)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done(), "repeated cancellation must not interrupt lease release"
        finish_release.set()
        expected = OSError if outcome == "release_error" else asyncio.CancelledError
        with pytest.raises(expected):
            await asyncio.wait_for(task, timeout=1)
        assert released == ([child] if outcome == "acquired" else [])
    finally:
        finish_acquire.set()
        finish_release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.fixture
def native_client(tmp_path):
    try:
        client_type, _ = get_binding_client()
    except ImportError:
        pytest.skip("RAGFS native binding is not installed")
    config = RagfsBindingConfig(agfs=AGFSConfig(path=str(tmp_path), backend="local"))
    native = client_type(None, config=config.to_binding_dict())
    try:
        mount_agfs_backend(native, config)
        yield native
    finally:
        native.close()


@pytest.mark.parametrize("workers", [1, 2])
def test_native_lock_waiters_do_not_starve_holder_io_or_release(native_client, workers):
    async def run():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=workers))
        client = AsyncAGFSClient(native_client)
        path = "/local/acct/cases/one.md"
        paths = [path, "/local/acct/cases/.overview.md"]
        ctx = {"account_id": "acct"}
        await client.ensure_parent_dirs(path)
        holder = await client.pathlock_acquire_exact_batch(paths)

        async def contender():
            lease = await client.pathlock_acquire_exact_batch(paths, timeout_secs=0.5)
            try:
                await client.read(path)
            finally:
                await client.pathlock_release(lease, fs_ctx=ctx)

        tasks = [asyncio.create_task(contender()) for _ in range(workers)]
        try:
            # Let the contenders occupy all workers in the old blocking implementation.
            await asyncio.sleep(0.05)
            await client.write(path, b"case", fs_ctx={**ctx, "lease_ref": holder["lease_ref"]})
        finally:
            await client.pathlock_release(holder, fs_ctx=ctx)
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        assert outcomes == [None] * workers
        assert (await client.pathlock_observe(fs_ctx=ctx))["active_locks"] == 0

    asyncio.run(run())


def test_native_batch_conflict_does_not_hold_partial_locks(native_client):
    async def run():
        client = AsyncAGFSClient(native_client)
        ctx = {"account_id": "acct"}
        a, b = "/local/acct/cases/a.md", "/local/acct/cases/b.md"
        await client.ensure_parent_dirs(a)
        holder = await client.pathlock_acquire_exact(b)
        pending = asyncio.create_task(client.pathlock_acquire_exact_batch([a, b], timeout_secs=1))
        try:
            await asyncio.sleep(0.05)
            assert not pending.done()
            probe = await client.pathlock_acquire_exact(a)
            await client.pathlock_release(probe, fs_ctx=ctx)
        finally:
            await client.pathlock_release(holder, fs_ctx=ctx)
        lease = await pending
        await client.pathlock_release(lease, fs_ctx=ctx)
        assert (await client.pathlock_observe(fs_ctx=ctx))["active_locks"] == 0

    asyncio.run(run())


@pytest.mark.asyncio
async def test_retries_do_not_reset_deadline_or_start_after_it(adapter, monkeypatch):
    clock = SimpleNamespace(now=0.0)

    async def advance(delay):
        clock.now += delay

    fake_asyncio = SimpleNamespace(**vars(asyncio))
    fake_asyncio.get_running_loop = lambda: SimpleNamespace(time=lambda: clock.now)
    fake_asyncio.sleep = AsyncMock(side_effect=advance)
    monkeypatch.setattr(async_client_module, "asyncio", fake_asyncio)
    monkeypatch.setattr(random, "uniform", lambda low, high: high)
    adapter.run.side_effect = LockAcquisitionError("busy")

    with pytest.raises(LockAcquisitionError, match="timed out"):
        await adapter.client.pathlock_acquire_exact(_PATH, timeout_secs=0.35)

    assert adapter.run.await_count == 3
    assert [c.args[0] for c in fake_asyncio.sleep.await_args_list] == pytest.approx(
        [0.1, 0.2, 0.05]
    )
    assert clock.now == pytest.approx(0.35)


@pytest.mark.parametrize("method,args", _METHODS)
def test_all_native_lock_interfaces_preserve_acquisition_and_release(native_client, method, args):
    async def run():
        client = AsyncAGFSClient(native_client)
        ctx = {"account_id": "acct"}
        await client.ensure_parent_dirs("/local/acct/cases/one.md")
        await client.mkdir("/local/acct/other")
        lease = await getattr(client, method)(*args, timeout_secs=0.5)
        try:
            assert (await client.pathlock_observe(fs_ctx=ctx))["active_locks"] == 1
        finally:
            await client.pathlock_release(lease, fs_ctx=ctx)
        assert (await client.pathlock_observe(fs_ctx=ctx))["active_locks"] == 0

    asyncio.run(run())


def test_native_cancelled_child_acquisition_preserves_parent_lease(native_client):
    async def run():
        loop = asyncio.get_running_loop()
        client = AsyncAGFSClient(native_client)
        ctx = {"account_id": "acct"}
        a, b = "/local/acct/cases/a.md", "/local/acct/cases/b.md"
        await client.ensure_parent_dirs(a)
        parent = await client.pathlock_acquire_exact(a)
        acquired = asyncio.Event()
        finish = threading.Event()

        def acquire(native_ctx, paths, timeout, owner):
            child = native_client.pathlock_acquire_exact_batch(native_ctx, paths, timeout, owner)
            loop.call_soon_threadsafe(acquired.set)
            assert finish.wait(2)
            return child

        blocked = AsyncAGFSClient(
            SimpleNamespace(
                pathlock_acquire_exact_batch=acquire,
                pathlock_release=native_client.pathlock_release,
            )
        )
        task = asyncio.create_task(blocked.pathlock_acquire_exact_batch([a, b], 1, parent))
        try:
            await asyncio.wait_for(acquired.wait(), timeout=1)
            task.cancel()
            finish.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=1)
            assert await client.pathlock_is_locked(a)
            assert not await client.pathlock_is_locked(b)
            assert (await client.pathlock_observe(fs_ctx=ctx))["active_locks"] == 1
        finally:
            finish.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await client.pathlock_release(parent, fs_ctx=ctx)
        assert (await client.pathlock_observe(fs_ctx=ctx))["active_locks"] == 0

    asyncio.run(run())
