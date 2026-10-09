# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openviking.session.train.context import PipelineContext
from openviking.session.train.domain import (
    Policy,
    PolicyApplyResult,
    PolicyPlanItem,
    PolicySet,
    PolicyUpdatePlan,
)
from openviking.session.train.engine import PolicyTrainingEngine
from openviking.storage.errors import ResourceBusyError

_ROOT = "viking://user/u/memories/experiences"
_A = f"{_ROOT}/a.md"
_B = f"{_ROOT}/b.md"


def policy(uri=_A, version=1, content="original"):
    return Policy(
        name=uri.rsplit("/", 1)[-1][:-3],
        uri=uri,
        version=version,
        status="production",
        content=content,
    )


def plan(snapshot, *, target=_A, scope=None, kind="upsert"):
    before = next((p for p in snapshot.policies if p.uri == target), None)
    item = PolicyPlanItem(
        kind=kind,
        memory_type="experiences",
        target_name=target.rsplit("/", 1)[-1][:-3],
        target_uri=target,
        before_content=before.content if before else None,
        base_version=before.version if before else None,
        after_content=f"planned from {before.content if before else 'missing'}"
        if kind == "upsert"
        else None,
    )
    kwargs = {} if scope is None else {"read_policy_uris": scope}
    return PolicyUpdatePlan(items=[item], **kwargs)


class Store:
    root_uri = _ROOT

    def __init__(self):
        self.policies = {_A: policy(), _B: policy(_B)}
        self.mutex = asyncio.Lock()
        self.locked = False
        self.lock_count = 0
        self.reload_count = 0
        self.applied = []
        self.active_lease = None

    @asynccontextmanager
    async def lock(self):
        async with self.mutex:
            self.lock_count += 1
            self.locked = True
            self.active_lease = f"lease-{self.lock_count}"
            try:
                yield self.active_lease
            finally:
                self.active_lease = None
                self.locked = False

    async def reload(self):
        assert self.locked
        self.reload_count += 1
        return self.snapshot()

    def snapshot(self):
        return PolicySet(root_uri=self.root_uri, policies=deepcopy(list(self.policies.values())))

    async def apply(self, proposed, snapshot, context, *, transaction_handle):
        assert self.locked
        assert transaction_handle == self.active_lease
        assert context == "apply"
        self.applied.append((proposed, snapshot))
        for item in proposed.items:
            uri = item.target_uri or f"{snapshot.root_uri}/{item.target_name}.md"
            if item.kind == "delete":
                self.policies.pop(uri, None)
            else:
                current = self.policies.get(uri)
                self.policies[uri] = policy(
                    uri, (current.version if current else 0) + 1, item.after_content
                )
        return PolicyApplyResult(updated_policy_set=self.snapshot())


@pytest.fixture
def state():
    store = Store()

    async def propose(gradients, snapshot, context):
        assert not store.locked, "model planning must run outside the directory lock"
        return plan(snapshot)

    optimizer = SimpleNamespace(plan=AsyncMock(side_effect=propose))
    updater = SimpleNamespace(apply=AsyncMock(side_effect=store.apply))
    engine = PolicyTrainingEngine(
        rollout_analyzer=SimpleNamespace(),
        gradient_estimator=SimpleNamespace(),
        policy_optimizer=optimizer,
        policy_updater=updater,
    )
    ctx = PipelineContext(
        optimization_context="optimizer", gradient_context="gate", apply_context="apply"
    )
    return SimpleNamespace(
        store=store, optimizer=optimizer, updater=updater, engine=engine, ctx=ctx
    )


async def execute(state):
    return await state.engine.plan_and_apply(gradients=[], policy_set=state.store, ctx=state.ctx)


@pytest.mark.asyncio
async def test_plan_and_gate_are_unlocked_but_reads_and_apply_are_locked(state):
    async def validate(proposed, gradients, snapshot, context):
        assert not state.store.locked, "LLM evaluation must run outside the directory lock"
        assert context == "gate"
        return proposed

    gate = SimpleNamespace(validate=AsyncMock(side_effect=validate))
    state.engine.policy_update_gate = gate
    proposed, result = await execute(state)
    assert result.updated_policy_set.policies[0].version == 2
    assert state.store.lock_count == state.store.reload_count == 2
    assert state.store.applied[0][0] is proposed
    state.optimizer.plan.assert_awaited_once()
    gate.validate.assert_awaited_once()
    assert not state.store.locked


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation", ["update", "version_only", "metadata", "links", "status", "delete", "create"]
)
async def test_target_changes_replan_from_fresh_state_without_applying_stale_plan(state, mutation):
    if mutation == "create":
        state.store.policies.pop(_A)
    calls = 0

    async def propose(gradients, snapshot, context):
        nonlocal calls
        assert not state.store.locked
        calls += 1
        proposed = plan(snapshot, scope=[_A])
        if calls == 1:
            async with state.store.lock():
                if mutation == "update":
                    state.store.policies[_A] = policy(version=2, content="concurrent")
                elif mutation == "version_only":
                    state.store.policies[_A].version += 1
                elif mutation == "metadata":
                    state.store.policies[_A].metadata["other_writer"] = True
                elif mutation == "links":
                    state.store.policies[_A].links.append({"to_uri": "viking://other/evidence"})
                elif mutation == "status":
                    state.store.policies[_A].status = "archived"
                elif mutation == "delete":
                    del state.store.policies[_A]
                else:
                    state.store.policies[_A] = policy(content="concurrent creation")
        return proposed

    state.optimizer.plan.side_effect = propose
    proposed, _ = await execute(state)
    assert calls == 2
    state.updater.apply.assert_awaited_once()
    applied_snapshot = state.store.applied[0][1]
    expected = next((p for p in applied_snapshot.policies if p.uri == _A), None)
    assert proposed.items[0].before_content == (expected.content if expected else None)
    assert not state.store.locked


@pytest.mark.asyncio
@pytest.mark.parametrize("scope,expected_calls", [(None, 2), ([_A, _B], 2), ([_A], 1)])
async def test_read_dependencies_are_validated_without_invalidating_unrelated_cases(
    state, scope, expected_calls
):
    calls = 0

    async def propose(gradients, snapshot, context):
        nonlocal calls
        assert not state.store.locked
        calls += 1
        if calls == 1:
            state.store.policies[_B] = policy(_B, version=2, content="other Case")
        return plan(snapshot, scope=scope)

    state.optimizer.plan.side_effect = propose
    _, result = await execute(state)
    assert calls == expected_calls
    assert (
        next(p for p in result.updated_policy_set.policies if p.uri == _B).content == "other Case"
    )


@pytest.mark.asyncio
async def test_generic_gate_checks_the_full_snapshot_even_for_a_scoped_optimizer(state):
    state.optimizer.plan.side_effect = lambda _, snapshot, __: plan(snapshot, scope=[_A])
    calls = 0

    async def validate(proposed, gradients, snapshot, context):
        nonlocal calls
        assert not state.store.locked
        calls += 1
        if calls == 1:
            state.store.policies[_B].version += 1
        return proposed

    state.engine.policy_update_gate = SimpleNamespace(validate=AsyncMock(side_effect=validate))
    await execute(state)
    assert calls == 2
    state.updater.apply.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("stale_field", ["base_version", "before_content"])
async def test_stale_plan_guards_are_checked_even_without_a_concurrent_change(state, stale_field):
    async def propose(gradients, snapshot, context):
        proposed = plan(snapshot, scope=[_A])
        setattr(proposed.items[0], stale_field, 0 if stale_field == "base_version" else "stale")
        return proposed

    state.optimizer.plan.side_effect = propose
    with pytest.raises(ResourceBusyError, match="planning"):
        await execute(state)
    assert state.optimizer.plan.await_count == 3
    state.updater.apply.assert_not_awaited()
    assert state.store.policies[_A].version == 1
    assert not state.store.locked


@pytest.mark.asyncio
async def test_conflict_aborts_whole_plan_and_never_replans_inside_lock(state):
    async def propose(gradients, snapshot, context):
        assert not state.store.locked
        proposed = plan(snapshot, scope=[])
        proposed.items += plan(snapshot, target=_B).items
        # The written URI must be protected even if omitted from the declared read set.
        state.store.policies[_B].version += 1
        return proposed

    state.optimizer.plan.side_effect = propose
    with pytest.raises(ResourceBusyError):
        await execute(state)
    assert state.optimizer.plan.await_count == 3
    state.updater.apply.assert_not_awaited()
    assert state.store.policies[_A].version == 1
    assert not state.store.locked


@pytest.mark.asyncio
async def test_noop_plan_still_validates_its_read_dependencies(state):
    calls = 0

    async def propose(gradients, snapshot, context):
        nonlocal calls
        calls += 1
        if calls == 1:
            state.store.policies[_A].version += 1
        return PolicyUpdatePlan(read_policy_uris=[_A])

    state.optimizer.plan.side_effect = propose
    await execute(state)
    assert calls == 2
    assert state.store.policies[_A].version == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["plan", "gate", "apply"])
@pytest.mark.parametrize("error", [RuntimeError("failed"), asyncio.CancelledError()])
async def test_failure_or_cancellation_releases_locks_without_replaying_work(state, stage, error):
    async def fail(*args, **kwargs):
        assert state.store.locked == (stage == "apply")
        raise error

    if stage == "gate":
        operation = AsyncMock(side_effect=fail)
        state.engine.policy_update_gate = SimpleNamespace(validate=operation)
    else:
        operation = state.optimizer.plan if stage == "plan" else state.updater.apply
        operation.side_effect = fail
    with pytest.raises(type(error)) as caught:
        await execute(state)
    assert caught.value is error
    operation.assert_awaited_once()
    assert not state.store.locked
    if stage != "apply":
        state.updater.apply.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelling_pending_model_does_not_hold_directory_lock(state):
    entered = asyncio.Event()

    async def model(*args):
        entered.set()
        await asyncio.Event().wait()

    state.optimizer.plan.side_effect = model
    task = asyncio.create_task(execute(state))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert not state.store.locked
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert not state.store.locked
    state.updater.apply.assert_not_awaited()


def test_native_writer_can_finish_while_model_is_planning(tmp_path):
    import json

    from openviking.pyagfs import AsyncAGFSClient, get_binding_client
    from openviking.utils.agfs_utils import RagfsBindingConfig, mount_agfs_backend
    from openviking_cli.utils.config.agfs_config import AGFSConfig

    try:
        client_type, _ = get_binding_client()
    except ImportError:
        pytest.skip("RAGFS native binding is not installed")
    config = RagfsBindingConfig(agfs=AGFSConfig(path=str(tmp_path), backend="local"))
    native = client_type(None, config=config.to_binding_dict())
    client = AsyncAGFSClient(native)
    native_root = "/local/acct/experiences"
    native_path = native_root + "/state.json"
    disk_path = tmp_path / "viking/acct/experiences/state.json"
    fs_ctx = {"account_id": "acct"}

    class NativeStore(Store):
        @asynccontextmanager
        async def lock(self):
            lease = await client.pathlock_acquire_tree(native_root, timeout_secs=0.2)
            self.locked = True
            self.active_lease = lease
            try:
                yield lease
            finally:
                self.locked = False
                self.active_lease = None
                await client.pathlock_release(lease, fs_ctx=fs_ctx)

        async def reload(self):
            assert self.locked
            self.policies = {
                uri: Policy(**data) for uri, data in json.loads(disk_path.read_text()).items()
            }
            return self.snapshot()

        async def persist(self, lease):
            payload = json.dumps({uri: asdict(p) for uri, p in self.policies.items()}).encode()
            await client.write(
                native_path, payload, fs_ctx={**fs_ctx, "lease_ref": lease["lease_ref"]}
            )

        async def apply(self, *args, transaction_handle, **kwargs):
            result = await super().apply(*args, transaction_handle=transaction_handle, **kwargs)
            await self.persist(transaction_handle)
            return result

    async def run():
        store = NativeStore()
        await client.ensure_parent_dirs(native_path)
        async with store.lock() as lease:
            await store.persist(lease)
        calls = 0

        async def propose(gradients, snapshot, context):
            nonlocal calls
            calls += 1
            if calls == 1:
                async with store.lock() as lease:
                    store.policies[_A] = policy(version=2, content="concurrent writer")
                    await store.persist(lease)
            return plan(snapshot, scope=[_A])

        engine = PolicyTrainingEngine(
            rollout_analyzer=SimpleNamespace(),
            gradient_estimator=SimpleNamespace(),
            policy_optimizer=SimpleNamespace(plan=propose),
            policy_updater=store,
        )
        proposed, _ = await engine.plan_and_apply(
            gradients=[], policy_set=store, ctx=PipelineContext(apply_context="apply")
        )
        assert calls == 2
        assert proposed.items[0].base_version == 2
        assert store.policies[_A].content == "planned from concurrent writer"
        assert (await client.pathlock_observe(fs_ctx=fs_ctx))["active_locks"] == 0

    try:
        mount_agfs_backend(native, config)
        asyncio.run(run())
    finally:
        native.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("same_case", [False, True])
async def test_concurrent_planners_keep_updates_without_unnecessary_replanning(state, same_case):
    both_started = asyncio.Event()
    calls = 0

    async def propose(gradients, snapshot, context):
        nonlocal calls
        assert not state.store.locked
        calls += 1
        if calls >= 2:
            both_started.set()
        await both_started.wait()
        return plan(snapshot, target=gradients[0], scope=[gradients[0]])

    state.optimizer.plan.side_effect = propose
    results = await asyncio.wait_for(
        asyncio.gather(
            *(
                state.engine.plan_and_apply(
                    gradients=[target], policy_set=state.store, ctx=state.ctx
                )
                for target in (_A, _A if same_case else _B)
            )
        ),
        timeout=1,
    )
    assert len(results) == 2
    assert calls == (3 if same_case else 2)
    assert state.store.policies[_A].version == (3 if same_case else 2)
    assert state.store.policies[_B].version == (1 if same_case else 2)
    assert state.updater.apply.await_count == 2
    assert not state.store.locked


@pytest.mark.asyncio
async def test_creation_race_checks_implicit_write_target_even_with_empty_read_set(state):
    state.store.policies.pop(_B)
    calls = 0

    async def propose(gradients, snapshot, context):
        nonlocal calls
        calls += 1
        proposed = plan(snapshot, target=_B, scope=[])
        proposed.items[0].target_uri = None
        if calls == 1:
            state.store.policies[_B] = policy(_B, content="created concurrently")
        return proposed

    state.optimizer.plan.side_effect = propose
    proposed, _ = await execute(state)
    assert calls == 2
    assert proposed.items[0].before_content == "created concurrently"
    state.updater.apply.assert_awaited_once()


@pytest.mark.asyncio
async def test_snapshot_guard_is_independent_of_planner_mutation(state):
    calls = 0

    async def propose(gradients, snapshot, context):
        nonlocal calls
        calls += 1
        proposed = plan(snapshot, scope=[_A])
        if calls == 1:
            snapshot.policies[0].metadata["other_writer"] = True
            state.store.policies[_A].metadata["other_writer"] = True
        return proposed

    state.optimizer.plan.side_effect = propose
    await execute(state)
    assert calls == 2, "mutating the planner snapshot must not change the validation baseline"


@pytest.mark.asyncio
async def test_before_content_guard_preserves_existing_whitespace_normalization(state):
    async def propose(gradients, snapshot, context):
        proposed = plan(snapshot, scope=[_A])
        proposed.items[0].before_content = "\noriginal\n"
        return proposed

    state.optimizer.plan.side_effect = propose
    await execute(state)
    state.optimizer.plan.assert_awaited_once()
    state.updater.apply.assert_awaited_once()
