# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from mcp.types import Tool

from openviking.server.auth import get_request_context
from openviking.server.identity import RequestContext, Role
from openviking.server.routers import mcps as mcps_router_module
from openviking.service.mcp_service import MCPService
from openviking_cli.exceptions import InvalidArgumentError, UnavailableError
from openviking_cli.session.user_id import UserIdentifier

pytestmark = pytest.mark.asyncio


def _ctx() -> RequestContext:
    return RequestContext(
        user=UserIdentifier("account-1", "user-1"),
        role=Role.USER,
    )


async def test_add_stores_headers_only_in_internal_registry(monkeypatch):
    service = MCPService(SimpleNamespace())
    registry = service._empty_document()
    writes: dict[str, dict] = {}

    monkeypatch.setattr(
        service,
        "_discover",
        AsyncMock(
            return_value=(
                SimpleNamespace(protocolVersion="2025-03-26"),
                [
                    Tool(
                        name="weather.current",
                        description="Current weather",
                        inputSchema={"type": "object"},
                    )
                ],
            )
        ),
    )
    monkeypatch.setattr(
        service,
        "_load",
        AsyncMock(return_value=registry),
    )
    monkeypatch.setattr(service, "_publish_catalog", AsyncMock())

    async def capture_write(path, document):
        writes[path] = document

    monkeypatch.setattr(service, "_write_document", capture_write)

    result = await service.add(
        "weather",
        "https://mcp.example.com/api",
        {"Authorization": "Bearer secret"},
        _ctx(),
    )

    assert result["tool_count"] == 1
    assert result["header_names"] == ["Authorization"]
    assert "headers" not in result
    stored = writes["/local/account-1/_system/mcps.json"]["servers"]["weather"]
    assert "tools" not in stored
    assert "header_names" not in stored
    assert stored["headers"]["Authorization"] == "Bearer secret"
    assert list(writes) == ["/local/account-1/_system/mcps.json"]


async def test_url_and_tool_filename_validation(monkeypatch):
    service = MCPService(SimpleNamespace())

    with pytest.raises(InvalidArgumentError):
        service._validate_url("http://mcp.example.com/api")

    monkeypatch.setattr(
        service,
        "_discover",
        AsyncMock(
            return_value=(
                SimpleNamespace(protocolVersion="2025-03-26"),
                [Tool(name="..", inputSchema={"type": "object"})],
            )
        ),
    )
    monkeypatch.setattr(
        service,
        "_load",
        AsyncMock(return_value=service._empty_document()),
    )
    with pytest.raises(InvalidArgumentError, match="filenames"):
        await service.add("unsafe", "https://mcp.example.com/api", {}, _ctx())


async def test_validate_discovers_tools_without_registry_access(monkeypatch):
    service = MCPService(SimpleNamespace())
    server_info = SimpleNamespace(model_dump=lambda **_kwargs: {"name": "demo"})
    discover = AsyncMock(
        return_value=(
            SimpleNamespace(protocolVersion="2025-03-26", serverInfo=server_info),
            [Tool(name="echo", inputSchema={"type": "object"})],
        )
    )
    monkeypatch.setattr(service, "_discover", discover)
    load = AsyncMock()
    monkeypatch.setattr(service, "_load", load)

    result = await service.validate(
        "demo",
        "https://mcp.example.com/api",
        {"Authorization": "Bearer secret"},
    )

    assert result["valid"] is True
    assert result["tool_count"] == 1
    discover.assert_awaited_once_with(
        {"name": "demo", "url": "https://mcp.example.com/api"},
        {"Authorization": "Bearer secret"},
    )
    load.assert_not_awaited()


async def test_invoke_failure_is_not_retried_or_rewrapped(monkeypatch):
    service = MCPService(SimpleNamespace())
    server = {
        "name": "weather",
        "url": "https://mcp.example.com/api",
    }
    monkeypatch.setattr(
        service,
        "_registered",
        AsyncMock(return_value=(server, {})),
    )
    monkeypatch.setattr(
        service,
        "_catalog_names",
        AsyncMock(return_value={"current"}),
    )

    session = SimpleNamespace(
        initialize=AsyncMock(),
        call_tool=AsyncMock(side_effect=RuntimeError("connection dropped")),
    )

    @asynccontextmanager
    async def fake_session(_server, _headers):
        try:
            yield session
        except (InvalidArgumentError, UnavailableError):
            raise

    monkeypatch.setattr(service, "_session", fake_session)

    with pytest.raises(UnavailableError, match="was not retried"):
        await service.invoke(
            "viking://agent/tools/weather/current.json",
            {"city": "Beijing"},
            _ctx(),
        )
    session.call_tool.assert_awaited_once()


async def test_find_reports_total_before_limit(monkeypatch):
    service = MCPService(SimpleNamespace())
    tools = [
        {
            "revision": str(index),
            "tool": {
                "name": f"weather.{index}",
                "description": "Current weather forecast",
            },
        }
        for index in range(3)
    ]
    monkeypatch.setattr(
        service,
        "_load",
        AsyncMock(return_value={"version": 1, "servers": {"weather": {"name": "weather"}}}),
    )
    monkeypatch.setattr(
        service,
        "_catalog",
        AsyncMock(
            return_value=[
                {
                    "uri": f"viking://agent/tools/weather/{item['tool']['name']}.json",
                    **item,
                }
                for item in tools
            ]
        ),
    )

    result = await service.find("weather", 1, _ctx())

    assert result["total"] == 3
    assert len(result["tools"]) == 1


async def test_show_uses_catalog_instead_of_legacy_registry_tools(monkeypatch):
    service = MCPService(SimpleNamespace())
    monkeypatch.setattr(
        service,
        "_load",
        AsyncMock(
            return_value={
                "version": 1,
                "servers": {
                    "weather": {
                        "name": "weather",
                        "url": "https://mcp.example.com/api",
                        "tools": [{"tool": {"name": "legacy"}}],
                    },
                },
            }
        ),
    )
    current = {
        "uri": "viking://agent/tools/weather/current.json",
        "server": "weather",
        "revision": "revision-1",
        "tool": {"name": "current", "description": "Current weather"},
    }
    monkeypatch.setattr(service, "_catalog", AsyncMock(return_value=[current]))

    result = await service.show("weather", _ctx())

    assert result["tool_count"] == 1
    assert result["tools"] == [current]
    assert all(tool["tool"]["name"] != "legacy" for tool in result["tools"])


async def test_router_exposes_and_delegates_only_requested_operations(monkeypatch):
    methods = {
        name: AsyncMock(return_value={"operation": name})
        for name in (
            "add",
            "remove",
            "list",
            "find",
            "validate",
            "show",
            "sync",
            "status",
            "invoke",
        )
    }
    fake_service = SimpleNamespace(mcps=SimpleNamespace(**methods))
    monkeypatch.setattr(mcps_router_module, "get_service", lambda: fake_service)

    app = FastAPI()
    app.include_router(mcps_router_module.router)
    app.dependency_overrides[get_request_context] = _ctx
    transport = httpx.ASGITransport(app=app)

    requests = [
        ("POST", "/api/v1/mcps", {"name": "demo", "url": "https://example.com/mcp"}),
        ("DELETE", "/api/v1/mcps/demo", None),
        ("GET", "/api/v1/mcps", None),
        ("POST", "/api/v1/mcps/find", {"query": "weather"}),
        (
            "POST",
            "/api/v1/mcps/validate",
            {"name": "demo", "url": "https://example.com/mcp"},
        ),
        ("GET", "/api/v1/mcps/demo", None),
        ("POST", "/api/v1/mcps/demo/sync", None),
        ("GET", "/api/v1/mcps/status?name=demo", None),
        (
            "POST",
            "/api/v1/mcps/invoke",
            {
                "uri": "viking://agent/tools/demo/current.json",
                "arguments": {"city": "Beijing"},
            },
        ),
    ]
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for method, path, body in requests:
            response = await client.request(method, path, json=body)
            assert response.status_code == 200, response.text

    assert all(mock.await_count == 1 for mock in methods.values())
