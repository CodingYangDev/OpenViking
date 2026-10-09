from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError
from test_experience_runtime import EXPERIENCE, TELL_PROGRAM
from test_experience_runtime import env as env

from benchmark.tau2.train.dag_experience_runtime import _session_state
from openviking.server.auth import get_request_context
from openviking.server.routers import search as search_router
from openviking.service.experience_runtime import ExperienceRuntime, SearchExperienceRequest
from openviking.session.memory.dataclass import MemoryFile
from openviking.session.memory.experience.state import MAX_SESSION_STATE_CHARS, serialize_state
from openviking.session.memory.utils.memory_file_utils import MemoryFileUtils
from openviking_cli.utils.config.agent_evolution_config import DagDeciderConfig


def test_native_state_excludes_advice_control_tools_and_private_reasoning():
    messages = [
        {"role": "system", "content": "Current business policy"},
        {"role": "user", "content": "My current goal"},
        {"role": "user", "content": "[Experience Reminder] old advice"},
        {"role": "user", "content": "Reflect on the results and decide next steps."},
        {
            "role": "assistant",
            "content": "Checking",
            "reasoning_content": "private",
            "tool_calls": [
                {"id": "r1", "function": {"name": "read_experience", "arguments": "{}"}},
                {"id": "q1", "function": {"name": "lookup", "arguments": '{"id":"1"}'}},
            ],
        },
        {"role": "tool", "tool_call_id": "r1", "content": "retrieved old advice"},
        {"role": "tool", "name": "lookup", "tool_call_id": "q1", "content": "actual fact"},
    ]
    original = copy.deepcopy(messages)
    state = _session_state(messages)
    assert state[:2] == messages[:2]
    assert len(state) == 4
    assert state[2]["tool_calls"][0]["id"] == "q1"
    assert state[3] == messages[-1]
    text = serialize_state(state)
    assert all(
        value not in text for value in ("private", "old advice", "read_experience", "Reflect on")
    )
    assert all("summary" not in item and "evidence_id" not in item for item in state)
    assert messages == original


@pytest.mark.parametrize("legacy", [{"context": "old"}, {"evidence": []}])
def test_old_runtime_payload_fields_are_rejected(legacy):
    with pytest.raises(ValidationError, match="Extra inputs"):
        SearchExperienceRequest(query="q", state=[{"role": "user", "content": "q"}], **legacy)


def test_runtime_state_still_has_a_serialized_size_budget():
    with pytest.raises(ValidationError, match="serialized characters"):
        SearchExperienceRequest(
            query="q", state=[{"role": "user", "content": "x" * MAX_SESSION_STATE_CHARS}]
        )
    with pytest.raises(ValidationError, match="valid role"):
        SearchExperienceRequest(query="q", state=[{"content": "missing role"}])


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", [None, "vlm", "jev"])
async def test_current_session_state_reaches_model_once_without_evidence_envelope(env, provider):
    ctx, fs, model, resolver = env
    fs.files[fs._uri_to_path(EXPERIENCE, ctx)] = MemoryFileUtils.write(
        MemoryFile(content=TELL_PROGRAM, memory_type="experiences")
    )
    state = [{"role": "user", "content": f"message {i}"} for i in range(300)]
    answers = {
        "answers": {
            "slot_0": {"type": "noul", "noul": 0.95},
            "experience_0_relevance": {"type": "score", "score": 3.8},
        }
    }
    model.get_completion_async.return_value = json.dumps(answers)
    runtime = ExperienceRuntime(
        fs, resolver, dag_decider_config=DagDeciderConfig(provider=provider) if provider else None
    )
    jev = None
    if provider == "jev":
        from openviking.service.experience_dag_decider import ExperienceDagDecider

        jev = SimpleNamespace(evaluate=AsyncMock(return_value=answers["answers"]))
        runtime.dag_decider = ExperienceDagDecider(runtime.dag_decider_config, jev)
    result = await runtime.search(SearchExperienceRequest(query="order", state=state), ctx)
    assert not result.errors and len(result.instructions) == 1
    payload = (
        jev.evaluate.await_args.kwargs
        if jev
        else json.loads(model.get_completion_async.await_args.kwargs["messages"][1]["content"])
    )
    assert payload["state"] == state
    assert "context" not in payload and "evidence" not in payload
    assert "questions" in payload
    assert fs.find_calls[0]["query"] == "order"


@pytest.mark.asyncio
async def test_client_generated_300_message_state_passes_real_http_ingress(env, monkeypatch):
    ctx, fs, model, resolver = env
    service = SimpleNamespace(
        search=SimpleNamespace(
            advice=AsyncMock(
                return_value=SimpleNamespace(
                    model_dump=lambda **kw: {"matched_experience_uris": []}
                )
            )
        )
    )
    monkeypatch.setattr(search_router, "get_service", lambda: service)
    app = FastAPI()
    app.include_router(search_router.router)
    app.dependency_overrides[get_request_context] = lambda: ctx
    state = _session_state([{"role": "user", "content": str(i)} for i in range(300)])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/search/search", json={"mode": "advice", "query": "q", "state": state}
        )
    assert response.status_code == 200, response.text
    assert service.search.advice.await_args.args[0].state == state


@pytest.mark.parametrize("role", [None, [], {}, 2, "unknown"])
def test_invalid_role_is_a_validation_error_not_an_internal_error(role):
    with pytest.raises(ValidationError, match="valid role"):
        SearchExperienceRequest(query="q", state=[{"role": role, "content": "q"}])


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", [None, "vlm"])
async def test_both_model_paths_bound_native_state_without_losing_tool_identity(env, provider):
    ctx, fs, model, resolver = env
    model.get_completion_async.return_value = json.dumps({"answers": {}})
    config = DagDeciderConfig(provider=provider, max_state_chars=1024) if provider else None
    runtime = ExperienceRuntime(fs, resolver, dag_decider_config=config)
    state = [{"role": "tool", "name": "lookup", "tool_call_id": "c1", "content": '"\\\n尾' * 20000}]
    from openviking.session.memory.experience.state import bound_session_state

    state = bound_session_state(state, MAX_SESSION_STATE_CHARS)
    await runtime.search(SearchExperienceRequest(query="q", state=state), ctx)
    payload = json.loads(model.get_completion_async.await_args.kwargs["messages"][1]["content"])
    assert len(serialize_state(payload["state"])) <= (1024 if provider else 32768)
    assert payload["state"][0]["name"] == "lookup"
    assert payload["state"][0]["tool_call_id"] == "c1"
    assert "...[truncated]..." in payload["state"][0]["content"]


@pytest.mark.asyncio
async def test_runtime_client_to_jev_uses_one_session_state_through_http(env, monkeypatch):
    from benchmark.tau2.train.dag_experience_runtime import Tau2DagExperienceRuntime
    from openviking.models.jev import JevClient
    from openviking.service.experience_dag_decider import ExperienceDagDecider
    from openviking_cli.client._http_compat import AsyncHTTPClient
    from openviking_cli.utils.config.jev_config import JevConfig

    ctx, fs, _, resolver = env
    fs.files[fs._uri_to_path(EXPERIENCE, ctx)] = MemoryFileUtils.write(
        MemoryFile(content=TELL_PROGRAM, memory_type="experiences")
    )
    requests = []

    def judge(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "answers": {
                    "slot_0": {"type": "noul", "noul": 0.95},
                    "experience_0_relevance": {"type": "score", "score": 3.8},
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(judge)) as model_http:
        config = DagDeciderConfig(provider="jev")
        jev = JevClient(
            JevConfig(api_url="http://mock-jev/v1/systemone", api_key="test"), client=model_http
        )
        runtime = ExperienceRuntime(
            fs, resolver, dag_decider_config=config, dag_decider=ExperienceDagDecider(config, jev)
        )

        async def advice(request, context):
            return await runtime.search(request, context)

        monkeypatch.setattr(
            search_router,
            "get_service",
            lambda: SimpleNamespace(search=SimpleNamespace(advice=advice)),
        )
        app = FastAPI()
        app.include_router(search_router.router)
        app.dependency_overrides[get_request_context] = lambda: ctx
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as http:
            bodies = []

            async def post(method, path, **kwargs):
                bodies.append(kwargs["json"])
                response = await http.request(method, path, **kwargs)
                response.raise_for_status()
                return response

            from vikingbot.openviking_mount.ov_server import VikingClient

            sdk = AsyncHTTPClient(url="http://test")
            sdk._request = post
            bridge = SimpleNamespace(_session_client_for_user=AsyncMock(return_value=sdk))
            bridge.search_advice = lambda *args, **kwargs: VikingClient.search_advice(
                bridge, *args, **kwargs
            )
            caller = Tau2DagExperienceRuntime()
            caller.client = bridge
            messages = [{"role": "user", "content": str(i)} for i in range(300)]
            messages += [{"role": "user", "content": "[Experience Reminder] not an observed fact"}]
            result = await caller.ask_for_advice("order eligibility", messages)

    assert '"status": "advice"' in result
    assert len(requests) == 1 and len(bodies) == 1
    assert requests[0]["state"] == messages[:300]
    assert set(requests[0]) == {"state", "questions"}
    assert "context" not in bodies[0] and "evidence" not in bodies[0]
    assert bodies[0]["query"] == "order eligibility"
    assert bodies[0]["mode"] == "advice" and "session_id" not in bodies[0]
    assert "[Experience Reminder]" not in serialize_state(requests[0])
