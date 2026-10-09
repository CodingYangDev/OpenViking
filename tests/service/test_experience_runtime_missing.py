# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from test_experience_runtime import EXPERIENCE, request
from test_experience_runtime import env as env

from openviking.service.experience_runtime import (
    ExperienceRuntime,
    SearchExperienceRequest,
)
from openviking_cli.exceptions import NotFoundError, PermissionDeniedError
from openviking_cli.utils.config.agent_evolution_config import DagDeciderConfig

SESSION = "viking://session/s1"
MISSING = "viking://user/u/memories/experiences/not_generated.md"


@pytest.fixture(params=[False, True], ids=["single", "batch"])
def runtime(env, request):
    ctx, fs, model, resolver = env
    decider = SimpleNamespace(
        decide=AsyncMock(
            side_effect=lambda instances, **kwargs: {
                instance.experience_uri: {} for instance in instances
            }
        )
    )
    return ExperienceRuntime(
        fs,
        resolver,
        dag_decider_config=DagDeciderConfig(provider="jev") if request.param else None,
        dag_decider=decider,
    )


def fixed(*names):
    return SearchExperienceRequest(
        query="Order 123",
        state=[{"role": "user", "content": "Order 123"}],
        experience_names=list(names),
    )


@pytest.mark.asyncio
async def test_missing_fixed_name_is_empty_and_can_appear_later(env, runtime):
    ctx, fs, model, resolver = env
    for _ in range(2):
        result = await runtime.search(fixed("not_generated"), ctx)
        assert result.errors == []
        assert result.matched_experience_uris == []
        assert result.experiences == []
        assert result.instructions == []
    resolver.get_vlm.assert_not_awaited()
    runtime.dag_decider.decide.assert_not_awaited()
    assert fs.find_calls == []
    fs.files[fs._uri_to_path(MISSING, ctx)] = fs.files[fs._uri_to_path(EXPERIENCE, ctx)]
    result = await runtime.search(fixed("not_generated"), ctx)
    assert result.errors == []
    assert result.matched_experience_uris == [MISSING]
    assert result.experiences[0].experience_uri == MISSING


@pytest.mark.asyncio
async def test_missing_fixed_name_does_not_hide_valid_sibling(env, runtime):
    ctx, fs, model, resolver = env
    result = await runtime.search(fixed("not_generated", "order"), ctx)
    assert result.errors == []
    assert result.matched_experience_uris == [EXPERIENCE]
    assert [item.experience_uri for item in result.experiences] == [EXPERIENCE]


@pytest.mark.asyncio
async def test_vector_hit_with_missing_file_remains_an_error(env, runtime):
    ctx, fs, model, resolver = env
    fs.find_memories = [SimpleNamespace(uri=MISSING), SimpleNamespace(uri=EXPERIENCE)]
    result = await runtime.search(
        SearchExperienceRequest(
            query="Order 123", state=[{"role": "user", "content": "Order 123"}]
        ),
        ctx,
    )
    assert len(result.errors) == 1
    assert result.errors[0].experience_uri == MISSING
    assert [item.experience_uri for item in result.experiences] == [EXPERIENCE]


@pytest.mark.asyncio
async def test_fixed_name_can_disappear_without_remembered_active_state(env, runtime):
    ctx, fs, model, resolver = env
    await runtime.search(fixed("order"), ctx)
    del fs.files[fs._uri_to_path(EXPERIENCE, ctx)]
    result = await runtime.search(fixed("order"), ctx)
    assert result.instructions == [] and result.errors == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        httpx.ReadError("connection reset"),
        httpx.ReadTimeout("read timed out"),
        PermissionDeniedError("denied"),
        RuntimeError("not found in a broken service"),
    ],
)
async def test_other_file_errors_are_not_swallowed(env, runtime, monkeypatch, error):
    ctx, fs, model, resolver = env
    read = fs.read_file

    async def fail(uri, ctx):
        if uri == EXPERIENCE:
            raise error
        return await read(uri, ctx)

    monkeypatch.setattr(fs, "read_file", fail)
    result = await runtime.search(fixed("order"), ctx)
    assert len(result.errors) == 1
    assert result.errors[0].experience_uri == EXPERIENCE
    assert result.errors[0].retryable is (not isinstance(error, PermissionDeniedError))


@pytest.mark.asyncio
async def test_not_found_wrapping_read_failure_is_not_normal_absence(env, runtime, monkeypatch):
    ctx, fs, model, resolver = env
    read = fs.read_file

    async def fail(uri, ctx):
        if uri == EXPERIENCE:
            raise NotFoundError(uri, "file") from httpx.ReadError("transport failed")
        return await read(uri, ctx)

    monkeypatch.setattr(fs, "read_file", fail)
    result = await runtime.search(fixed("order"), ctx)
    assert len(result.errors) == 1


@pytest.mark.asyncio
async def test_search_does_not_require_or_stat_a_session(env, runtime, monkeypatch):
    ctx, fs, _, _ = env
    monkeypatch.setattr(fs, "stat", AsyncMock(side_effect=AssertionError("no session stat")))
    result = await runtime.search(fixed("order"), ctx)
    assert result.matched_experience_uris == [EXPERIENCE]
    assert not result.errors
    fs.stat.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_dag_is_not_normal_absence(env, runtime):
    ctx, fs, model, resolver = env
    fs.files[fs._uri_to_path(EXPERIENCE, ctx)] = "not a valid DAG program"
    result = await runtime.search(fixed("order"), ctx)
    assert len(result.errors) == 1
    assert result.errors[0].retryable is False


@pytest.mark.asyncio
async def test_direct_evaluation_still_requires_existing_file(env):
    ctx, fs, model, resolver = env
    del fs.files[fs._uri_to_path(EXPERIENCE, ctx)]
    with pytest.raises(FileNotFoundError):
        await ExperienceRuntime(fs, resolver).advance(SESSION, request(), ctx)


@pytest.mark.asyncio
async def test_absent_experience_does_not_probe_a_session(env, runtime, monkeypatch):
    ctx, fs, _, _ = env
    monkeypatch.setattr(fs, "stat", AsyncMock(side_effect=AssertionError("no session needed")))
    result = await runtime.search(fixed("not_generated"), ctx)
    assert not result.instructions and not result.errors
    fs.stat.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_is_not_normal_absence(env, runtime, monkeypatch):
    import asyncio

    ctx, fs, model, resolver = env
    read = fs.read_file

    async def cancel(uri, ctx):
        if uri == EXPERIENCE:
            raise asyncio.CancelledError()
        return await read(uri, ctx)

    monkeypatch.setattr(fs, "read_file", cancel)
    with pytest.raises(asyncio.CancelledError):
        await runtime.search(fixed("order"), ctx)
