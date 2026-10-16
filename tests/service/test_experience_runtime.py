from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from openviking.server.auth import get_request_context
from openviking.server.identity import RequestContext, Role
from openviking.server.routers import search as search_router
from openviking.server.routers import sessions
from openviking.service.experience_runtime import (
    AdvanceExperienceRequest,
    ExperienceRuntime,
    SearchExperienceRequest,
)
from openviking.service.search_service import SearchService
from openviking.session.memory.dataclass import MemoryFile
from openviking.session.memory.experience.compiler import compile_dag
from openviking.session.memory.utils.memory_file_utils import MemoryFileUtils
from openviking_cli.exceptions import InvalidArgumentError, PermissionDeniedError
from openviking_cli.session.user_id import UserIdentifier
from openviking_cli.utils.config.agent_evolution_config import DagDeciderConfig

EXPERIENCE = "viking://user/u/memories/experiences/order.md"
EXPERIENCE_TWO = "viking://user/u/memories/experiences/order-two.md"
PROGRAM = """dag = workflow("Check an order")
known = ask("Order ID?")
done = call("query_order")
known.then(done)"""
TELL_PROGRAM = """dag = workflow("Remind about an order")
relevant = check("Is the user asking about an order?")
reminder = tell("Verify the complete order total before acting")
relevant.then(reminder)"""


class MemoryFS:
    def __init__(self):
        self.files = {}
        self.find_calls = []
        self.find_memories = [SimpleNamespace(uri=EXPERIENCE)]
        self.lock = asyncio.Lock()
        self._async_agfs = SimpleNamespace(
            pathlock_acquire_exact=self.acquire,
            pathlock_release=self.release,
        )

    def _uri_to_path(self, uri, ctx):
        return f"{ctx.account_id}/{uri}"

    async def acquire(self, path, **kwargs):
        await self.lock.acquire()
        return {"lease_ref": path}

    async def release(self, lease):
        self.lock.release()

    async def read_file(self, uri, ctx):
        key = self._uri_to_path(uri, ctx)
        if key not in self.files:
            raise FileNotFoundError(uri)
        return self.files[key]

    async def write_file(self, uri, content, ctx, **kwargs):
        self.files[self._uri_to_path(uri, ctx)] = content

    async def exists(self, uri, ctx):
        return self._uri_to_path(uri, ctx) in self.files

    async def stat(self, uri, **kwargs):
        return {}

    async def find(self, **kwargs):
        self.find_calls.append(kwargs)
        return SimpleNamespace(memories=list(self.find_memories))


@pytest.fixture
def env():
    ctx = RequestContext(user=UserIdentifier(account_id="acc", user_id="u"), role=Role.USER)
    fs = MemoryFS()
    raw = MemoryFileUtils.write(MemoryFile(content=PROGRAM, memory_type="experiences"))
    fs.files[fs._uri_to_path(EXPERIENCE, ctx)] = raw
    model = SimpleNamespace(
        get_completion_async=AsyncMock(
            return_value='{"answers":{"experience_0_relevance":{"type": "score", "score": 4.0}}}'
        )
    )
    resolver = SimpleNamespace(get_vlm=AsyncMock(return_value=model))
    return ctx, fs, model, resolver


def request(context="User asks about an order"):
    return AdvanceExperienceRequest(
        experience_uri=EXPERIENCE, state=[{"role": "user", "content": context}]
    )


@pytest.mark.asyncio
async def test_each_request_uses_current_template_and_context(env):
    ctx, fs, model, resolver = env
    model.get_completion_async.return_value = '{"slot_values":{}}'
    service = ExperienceRuntime(fs, resolver)
    first = await service.advance("viking://session/s1", request(), ctx)
    assert first.actions[0].provider.question == "Order ID?"
    await fs.write_file(
        EXPERIENCE,
        MemoryFileUtils.write(
            MemoryFile(
                content=PROGRAM.replace("Order ID?", "New question"), memory_type="experiences"
            )
        ),
        ctx,
    )
    second = await ExperienceRuntime(fs, resolver).advance("viking://session/s1", request(), ctx)
    assert second.actions[0].provider.question == "New question"
    model.get_completion_async.return_value = '{"slot_values":{"known":true,"done":true}}'
    assert (await service.advance("viking://session/s1", request(), ctx)).state == "completed"
    model.get_completion_async.return_value = '{"slot_values":{}}'
    assert (await service.advance("viking://session/s1", request(), ctx)).actions
    assert len(fs.files) == 1


@pytest.mark.asyncio
async def test_search_exp_selects_experience_and_returns_current_instructions(env):
    ctx, fs, model, resolver = env
    result = await ExperienceRuntime(fs, resolver).search(
        SearchExperienceRequest(
            query="Review order eligibility",
            state=[{"role": "user", "content": "system text\nOrder 123"}],
        ),
        ctx,
    )

    assert result.query == "Review order eligibility"
    assert fs.find_calls[0]["query"] == "Review order eligibility"
    assert result.matched_experience_uris == [EXPERIENCE]
    assert result.instructions[0].experience_uri == EXPERIENCE
    assert result.instructions[0].slot_name == "known"
    assert result.experiences[0].state == "running"
    assert fs.find_calls[0]["target_uri"] == "viking://user/u/memories/experiences"
    assert fs.find_calls[0]["score_threshold"] == 0.15
    assert fs.find_calls[0]["level"] == [2]

    fs.find_memories = []
    model.get_completion_async.return_value = '{"slot_values":{"known":true}}'
    resumed = await ExperienceRuntime(fs, resolver).search(
        SearchExperienceRequest(
            query="The lookup can continue",
            state=[{"role": "user", "content": "The lookup can continue"}],
        ),
        ctx,
    )
    assert resumed.matched_experience_uris == []
    assert resumed.instructions == []
    assert resumed.experiences == []


@pytest.mark.asyncio
async def test_search_exp_uses_explicit_experience_names_without_vector_search(env):
    ctx, fs, model, resolver = env

    result = await ExperienceRuntime(fs, resolver).search(
        SearchExperienceRequest(
            query="Order 123",
            state=[{"role": "user", "content": "Order 123"}],
            experience_names=["order"],
        ),
        ctx,
    )

    assert result.matched_experience_uris == [EXPERIENCE]
    assert result.instructions[0].slot_name == "known"
    assert fs.find_calls == []


@pytest.mark.asyncio
async def test_direct_slot_filling_cannot_complete_tell_agent_action(env):
    ctx, fs, model, resolver = env
    fs.files[fs._uri_to_path(EXPERIENCE, ctx)] = MemoryFileUtils.write(
        MemoryFile(content=TELL_PROGRAM, memory_type="experiences")
    )
    model.get_completion_async.return_value = json.dumps(
        {"slot_values": {"relevant": True, "reminder": True}}
    )

    result = await ExperienceRuntime(fs, resolver).advance(
        "viking://session/s1",
        AdvanceExperienceRequest(
            experience_uri=EXPERIENCE, state=[{"role": "user", "content": "Check order 123"}]
        ),
        ctx,
    )

    assert result.state == "running"
    assert result.executed_nodes == [1]
    assert result.actions[0].slot_name == "reminder"
    assert "reminder" not in result.slot_values


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "a\\b"])
def test_search_exp_rejects_invalid_explicit_experience_name(name):
    with pytest.raises(ValueError, match="simple non-empty names"):
        SearchExperienceRequest(
            query="Order 123",
            state=[{"role": "user", "content": "Order 123"}],
            experience_names=[name],
        )


@pytest.mark.asyncio
async def test_search_exp_batches_current_dags_through_one_decider_call(env):
    ctx, fs, model, resolver = env
    fs.files[fs._uri_to_path(EXPERIENCE_TWO, ctx)] = fs.files[fs._uri_to_path(EXPERIENCE, ctx)]
    fs.find_memories = [SimpleNamespace(uri=EXPERIENCE), SimpleNamespace(uri=EXPERIENCE_TWO)]
    decider = SimpleNamespace(
        decide=AsyncMock(
            return_value={
                EXPERIENCE: {"known": True, "done": True},
                EXPERIENCE_TWO: {"known": True, "done": True},
            }
        )
    )
    runtime = ExperienceRuntime(
        fs,
        resolver,
        dag_decider_config=DagDeciderConfig(provider="jev"),
        dag_decider=decider,
    )

    result = await runtime.search(
        SearchExperienceRequest(
            query="Order 123",
            state=[{"role": "user", "content": "Order 123"}],
        ),
        ctx,
    )

    assert {item.experience_uri for item in result.experiences} == {
        EXPERIENCE,
        EXPERIENCE_TWO,
    }
    assert all(item.state == "completed" for item in result.experiences)
    assert result.errors == []
    assert decider.decide.await_count == 1
    assert len(decider.decide.await_args.args[0]) == 2
    resolver.get_vlm.assert_not_awaited()


@pytest.mark.asyncio
async def test_search_exp_batches_current_dags_through_one_vlm_call(env):
    ctx, fs, model, resolver = env
    fs.files[fs._uri_to_path(EXPERIENCE_TWO, ctx)] = fs.files[fs._uri_to_path(EXPERIENCE, ctx)]
    fs.find_memories = [SimpleNamespace(uri=EXPERIENCE), SimpleNamespace(uri=EXPERIENCE_TWO)]
    model.get_completion_async.return_value = json.dumps(
        {
            "answers": {
                "slot_0": {"type": "noul", "noul": 0.99},
                "slot_1": {"type": "noul", "noul": 0.99},
                "slot_2": {"type": "noul", "noul": 0.99},
                "slot_3": {"type": "noul", "noul": 0.99},
                "experience_0_relevance": {"type": "score", "score": 3.96},
                "experience_1_relevance": {"type": "score", "score": 3.96},
            }
        }
    )
    runtime = ExperienceRuntime(
        fs,
        resolver,
        dag_decider_config=DagDeciderConfig(provider="vlm"),
    )

    result = await runtime.search(
        SearchExperienceRequest(
            query="Order 123",
            state=[{"role": "user", "content": "Order 123"}],
        ),
        ctx,
    )

    assert {item.experience_uri for item in result.experiences} == {
        EXPERIENCE,
        EXPERIENCE_TWO,
    }
    assert all(item.state == "completed" for item in result.experiences)
    assert result.errors == []
    resolver.get_vlm.assert_awaited_once_with(ctx.account_id)
    model.get_completion_async.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_jev_config_returns_error_without_vlm_fallback(env):
    ctx, fs, model, resolver = env
    runtime = ExperienceRuntime(
        fs,
        resolver,
        dag_decider_config=DagDeciderConfig(provider="jev"),
    )

    result = await runtime.search(
        SearchExperienceRequest(
            query="Order 123",
            state=[{"role": "user", "content": "Order 123"}],
        ),
        ctx,
    )

    assert result.experiences == []
    assert result.instructions == []
    assert "configuration is missing" in result.errors[0].error
    resolver.get_vlm.assert_not_awaited()


@pytest.mark.asyncio
async def test_transient_jev_failure_returns_error_without_advancing(env):
    ctx, fs, model, resolver = env
    decider = SimpleNamespace(decide=AsyncMock(side_effect=RuntimeError("temporary outage")))
    runtime = ExperienceRuntime(
        fs,
        resolver,
        dag_decider_config=DagDeciderConfig(provider="jev"),
        dag_decider=decider,
    )

    result = await runtime.search(
        SearchExperienceRequest(
            query="Order 123",
            state=[{"role": "user", "content": "Order 123"}],
        ),
        ctx,
    )

    assert result.experiences == []
    assert result.instructions == []
    assert result.errors[0].error == "temporary outage"
    resolver.get_vlm.assert_not_awaited()


@pytest.mark.asyncio
async def test_session_and_account_isolation(env):
    ctx, fs, model, resolver = env
    service = ExperienceRuntime(fs, resolver)
    model.get_completion_async.return_value = '{"slot_values":{"known":true}}'
    await service.advance("viking://session/s1", request(), ctx)
    model.get_completion_async.return_value = '{"slot_values":{}}'
    fresh = await service.advance("viking://session/s2", request(), ctx)
    assert fresh.actions[0].node_id == 1
    other = RequestContext(user=UserIdentifier(account_id="other", user_id="u"), role=Role.USER)
    fs.files[fs._uri_to_path(EXPERIENCE, other)] = fs.files[fs._uri_to_path(EXPERIENCE, ctx)]
    assert (await service.advance("viking://session/s1", request(), other)).actions[0].node_id == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "uri",
    [
        "viking://user/other/memories/experiences/x.md",
        "viking://user/u/memories/experiences/../x.md",
        "viking://user/u/memories/experiences/.abstract.md",
        "viking://resources/a.md",
    ],
)
async def test_foreign_or_invalid_experience_is_rejected(env, uri):
    ctx, fs, model, resolver = env
    with pytest.raises(PermissionDeniedError):
        await ExperienceRuntime(fs, resolver).advance(
            "viking://session/s1", request().model_copy(update={"experience_uri": uri}), ctx
        )
    model.get_completion_async.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        '{"slot_values":{"wrong":true}}',
        '{"slot_values":{"known":"order-123"}}',
        '{"slot_values":{"known":{"reservation_id":"DF89BM"}}}',
        "not json",
    ],
)
async def test_invalid_llm_result_does_not_overwrite_state(env, response):
    ctx, fs, model, resolver = env
    model.get_completion_async.return_value = '{"slot_values":{}}'
    runtime = ExperienceRuntime(fs, resolver)
    await runtime.advance("viking://session/s1", request(), ctx)
    old = dict(fs.files)
    model.get_completion_async.return_value = response
    with pytest.raises(InvalidArgumentError):
        await runtime.advance("viking://session/s1", request(), ctx)
    assert fs.files == old


@pytest.mark.asyncio
async def test_completed_checks_are_returned_without_synthetic_evidence(env):
    ctx, fs, model, resolver = env
    runtime = ExperienceRuntime(fs, resolver)
    model.get_completion_async.return_value = json.dumps({"slot_values": {"known": True}})

    result = await runtime.advance(
        "viking://session/s1",
        AdvanceExperienceRequest(
            experience_uri=EXPERIENCE,
            state=[{"role": "user", "content": "Order 123"}],
        ),
        ctx,
    )

    assert "slot_evidence" not in result.model_dump()
    assert result.node_slots == {1: "known", 2: "done"}
    assert result.completed_nodes[0].model_dump() == {
        "node_id": 1,
        "slot_name": "known",
        "slot_value": True,
        "evidence_refs": [],
    }


@pytest.mark.asyncio
async def test_synthetic_evidence_output_is_not_part_of_the_model_contract(env):
    ctx, fs, model, resolver = env
    model.get_completion_async.return_value = '{"slot_values":{}}'
    runtime = ExperienceRuntime(fs, resolver)
    await runtime.advance("viking://session/s1", request(), ctx)
    old = dict(fs.files)
    model.get_completion_async.return_value = (
        '{"slot_values":{"known":true},"slot_evidence":{"known":["invented"]}}'
    )

    with pytest.raises(InvalidArgumentError, match="Extra inputs"):
        await runtime.advance(
            "viking://session/s1",
            AdvanceExperienceRequest(
                experience_uri=EXPERIENCE,
                state=[{"role": "user", "content": "Order 123"}],
            ),
            ctx,
        )
    assert fs.files == old


@pytest.mark.asyncio
async def test_concurrent_evaluation_has_no_session_state_or_lock(env):
    ctx, fs, model, resolver = env
    model.get_completion_async.return_value = '{"slot_values":{"known":true}}'
    before = dict(fs.files)
    fs._async_agfs.pathlock_acquire_exact = AsyncMock(side_effect=AssertionError("no lock"))
    results = await asyncio.gather(
        *(
            ExperienceRuntime(fs, resolver).advance("viking://session/s1", request(), ctx)
            for _ in range(2)
        )
    )
    assert all(result.actions[0].slot_name == "done" for result in results)
    assert fs.files == before
    fs._async_agfs.pathlock_acquire_exact.assert_not_awaited()


@pytest.mark.asyncio
async def test_runtime_rejects_legacy_text_without_migration(env):
    ctx, fs, model, resolver = env
    await fs.write_file(EXPERIENCE, "## Situation\nAn old memory", ctx)
    with pytest.raises(InvalidArgumentError):
        await ExperienceRuntime(fs, resolver).advance("viking://session/s1", request(), ctx)
    assert len(fs.files) == 1
    model.get_completion_async.assert_not_awaited()


@pytest.mark.asyncio
async def test_advice_search_endpoint_uses_authenticated_user_without_session(env, monkeypatch):
    ctx, fs, model, resolver = env
    service = SearchService(viking_fs=fs)
    service.set_advice_dependencies(vlm_resolver=resolver, jev_config=None)
    monkeypatch.setattr(search_router, "get_service", lambda: SimpleNamespace(search=service))
    app = FastAPI()
    app.include_router(search_router.router)
    app.dependency_overrides[get_request_context] = lambda: ctx
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/search/search",
            json={
                "mode": "advice",
                "query": "Review order eligibility",
                "state": [{"role": "user", "content": "Order 123"}],
            },
        )
    assert response.status_code == 200, response.text
    assert response.json()["result"]["instructions"][0]["slot_name"] == "known"


@pytest.mark.asyncio
async def test_ack_endpoint_is_removed(env, monkeypatch):
    app = FastAPI()
    app.include_router(sessions.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        result = await client.post("/api/v1/sessions/s1/experiences/actions/ack", json={})
    assert result.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source,slot_values",
    [
        (PROGRAM, {"known": True}),
        (
            'dag = workflow("Alternative evidence")\n'
            'a = check("A holds")\n'
            'b = check("B holds")\n'
            "either = any_of(a, b)\n"
            'order = call("query_order", "Use the current order ID")\n'
            "either.then(order)",
            {"a": True},
        ),
    ],
    ids=["ordinary", "any-of"],
)
async def test_python_extraction_to_storage_to_runtime(env, monkeypatch, source, slot_values):
    from openviking.session.memory.experience.experience_context import (
        AgentExperienceContextProvider,
    )
    from openviking.session.memory.extract_loop import ExtractLoop
    from openviking.session.memory.memory_isolation_handler import MemoryIsolationHandler
    from openviking.session.memory.memory_type_registry import MemoryTypeRegistry
    from openviking.session.memory.memory_updater import MemoryUpdater

    ctx, fs, model, resolver = env
    registry = MemoryTypeRegistry()
    provider = AgentExperienceContextProvider(
        messages=[],
        case=SimpleNamespace(name="fresh"),
        target_uri="viking://user/u/memories/experiences/fresh.md",
        source_session_uri="viking://user/u/sessions/s/history/archive_001",
    )
    provider._registry = registry
    provider._ctx = ctx
    provider._viking_fs = fs
    provider.prefetch = AsyncMock(return_value=[])
    isolation = MemoryIsolationHandler(
        ctx, provider.get_extract_context(), allowed_memory_types={"experiences"}
    )
    isolation.prepare_messages()
    provider._isolation_handler = isolation
    configuration = SimpleNamespace(
        memory=SimpleNamespace(link_enabled=False, extraction_output_format="python")
    )
    monkeypatch.setattr(
        "openviking.session.memory.extract_loop.get_openviking_config", lambda: configuration
    )
    monkeypatch.setattr("openviking_cli.utils.config.get_openviking_config", lambda: configuration)
    # Exercise both the ordinary SDK program parser and the nested DAG program
    # interpreter, including the existing one-retry extraction repair path.
    bad = 'sdk.create_experiences(experience_name="fresh", content="dag.delete_node(999)", supersedes="")\nsdk.commit()'
    good = f'sdk.create_experiences(experience_name="fresh", content={source!r}, supersedes="")\nsdk.commit()'
    model.model = "mock"
    model.get_completion_async.side_effect = [bad, good]
    loop = ExtractLoop(
        vlm=model,
        viking_fs=fs,
        ctx=ctx,
        context_provider=provider,
        isolation_handler=isolation,
        max_iterations=1,
    )
    loop._check_unread_existing_files = AsyncMock(return_value={})
    resolved, _ = await loop.run()
    assert model.get_completion_async.await_count == 2
    assert len(resolved.upsert_operations) == 1
    op = resolved.upsert_operations[0]
    assert any(
        node.get("slot_provider", {}).get("tool_name") == "query_order"
        for node in json.loads(compile_dag(op.memory_fields["content"]))["nodes"].values()
    )
    retry_messages = model.get_completion_async.call_args.kwargs["messages"]
    assert any(
        "Memory content validation failed after applying the proposed edits" in message["content"]
        for message in retry_messages
    )
    assert any("Unsupported DAG relation" in message["content"] for message in retry_messages)

    updater = MemoryUpdater(registry=registry)
    updater._viking_fs = fs
    lease = await fs.acquire(fs._uri_to_path(op.uris[0], ctx))
    try:
        await updater._apply_upsert(op, ctx, lease_ref=lease["lease_ref"])
    finally:
        await fs.release(lease)
    stored = await fs.read_file(op.uris[0], ctx)
    assert MemoryFileUtils.read(stored).memory_type == "experiences"
    model.get_completion_async.side_effect = None
    model.get_completion_async.return_value = json.dumps({"slot_values": slot_values})
    result = await ExperienceRuntime(fs, resolver).advance(
        "viking://session/s1",
        AdvanceExperienceRequest(
            experience_uri=op.uris[0], state=[{"role": "user", "content": "The order ID is 123"}]
        ),
        ctx,
    )
    assert result.actions[0].provider.tool_name == "query_order"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "options, expected_threshold",
    [
        ({}, 0.15),
        ({"score_threshold": 0.2}, 0.2),
        ({"score_threshold": None}, None),
        ({"score_threshold": 0.0}, 0.0),
    ],
)
async def test_http_client_advice_mode_sends_native_state_without_session(
    options, expected_threshold
):
    from openviking_cli.client._http_compat import AsyncHTTPClient

    client = AsyncHTTPClient(url="http://test")
    client._request = AsyncMock(
        return_value=httpx.Response(200, json={"status": "ok", "result": {"instructions": []}})
    )
    state = [{"role": "user", "content": "Order 123"}]
    result = await client.search(
        "Review order eligibility",
        limit=2,
        options={"mode": "advice", "state": state, "score_threshold": 0.15, **options},
    )
    assert result == {"instructions": []}
    client._request.assert_awaited_once_with(
        "POST",
        "/api/v1/search/search",
        json={
            "query": "Review order eligibility",
            "mode": "advice",
            "state": state,
            "limit": 2,
            "score_threshold": expected_threshold,
        },
    )
    assert not hasattr(client, "search_exp")


@pytest.mark.parametrize("query", ["", "  ", "\n\t"])
def test_search_exp_rejects_blank_explicit_query(query):
    with pytest.raises(ValueError):
        SearchExperienceRequest(query=query, state=[{"role": "user", "content": "real evidence"}])


def test_search_exp_requires_query_even_when_context_is_available():
    with pytest.raises(ValueError):
        SearchExperienceRequest(
            state=[{"role": "user", "content": "Do not silently turn this into a query"}]
        )
    request = SearchExperienceRequest(
        query="  review eligibility  ", state=[{"role": "user", "content": "actual facts"}]
    )
    assert request.query == "review eligibility"
    assert request.state == [{"role": "user", "content": "actual facts"}]


@pytest.mark.parametrize(
    "options, expected_threshold",
    [
        ({}, 0.15),
        ({"score_threshold": 0.2}, 0.2),
        ({"score_threshold": None}, None),
        ({"score_threshold": 0.0}, 0.0),
    ],
)
def test_sync_http_client_forwards_explicit_advice_query(options, expected_threshold):
    from openviking_cli.client._http_compat import SyncHTTPClient

    request = AsyncMock(return_value={"instructions": []})
    client = SimpleNamespace(_async_client=SimpleNamespace(search=request))
    state = [{"role": "user", "content": "actual facts"}]
    result = SyncHTTPClient.search(
        client,
        "review eligibility",
        limit=3,
        options={
            "mode": "advice",
            "state": state,
            "score_threshold": 0.15,
            **options,
        },
    )
    assert result == {"instructions": []}
    assert request.await_args.args == ("review eligibility",)
    assert request.await_args.kwargs["options"] == {
        "mode": "advice",
        "state": state,
        "score_threshold": expected_threshold,
    }
    assert request.await_args.kwargs["limit"] == 3


@pytest.mark.asyncio
async def test_direct_slot_filling_cannot_override_local_or_logic(env):
    ctx, fs, model, resolver = env
    program = """dag = workflow("Alternative conditions")
a = check("A")
b = check("B")
either = any_of(a, b)
r = tell("Act")
either.then(r)"""
    fs.files[fs._uri_to_path(EXPERIENCE, ctx)] = MemoryFileUtils.write(
        MemoryFile(content=program, memory_type="experiences")
    )
    runtime = ExperienceRuntime(fs, resolver)
    for values, expected in [
        ({"either": True, "r": True}, []),
        ({"a": True, "either": False}, ["r"]),
        ({"a": False, "b": False, "either": True}, []),
    ]:
        model.get_completion_async.return_value = json.dumps({"slot_values": values})
        model.get_completion_async.reset_mock()
        result = await runtime.advance(
            "viking://session/s1",
            AdvanceExperienceRequest(
                experience_uri=EXPERIENCE, state=[{"role": "user", "content": "Current facts"}]
            ),
            ctx,
        )
        assert [action.slot_name for action in result.actions] == expected
        assert "either" not in result.slot_values
        assert 3 not in result.executed_nodes
        model.get_completion_async.assert_awaited_once()
        assert (
            "Never emit values for AnyOfNode"
            in model.get_completion_async.await_args.kwargs["messages"][0]["content"]
        )
