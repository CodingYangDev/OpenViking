# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest
from openviking_sdk import AsyncHTTPClient, SyncHTTPClient


@pytest.mark.asyncio
async def test_async_http_client_exposes_all_mcp_operations():
    client = AsyncHTTPClient(url="http://localhost:1933")
    fake_http = SimpleNamespace(request=AsyncMock(return_value=object()))
    client._http = fake_http
    client._handle_response = lambda _response: {"ok": True}

    assert await client.add_mcp(
        "demo",
        "https://mcp.example.com/api",
        headers={"Authorization": "Bearer secret"},
    ) == {"ok": True}
    await client.remove_mcp("demo/server")
    await client.list_mcps()
    await client.find_mcp_tools("weather forecast", limit=5)
    await client.validate_mcp("demo", "https://mcp.example.com/api")
    await client.get_mcp("demo/server")
    await client.sync_mcp("demo/server")
    await client.get_mcp_status("demo")
    await client.invoke_mcp_tool(
        "agent/tools/demo/current.json",
        arguments={"city": "Beijing"},
    )
    await client.mount_mcp_tool(
        "agent/tools/demo/current.json",
        "resources/demo/weather.md",
        "Beijing Weather",
        description="Current weather in Beijing",
        timeout_seconds=120,
        arguments={"city": "Beijing"},
        tags=["project=demo", "kind=live-context"],
    )
    await client.unmount_mcp_tool("resources/demo/weather.md")
    await client.list_mcp_mounts("demo", tool="current")

    assert fake_http.request.await_args_list == [
        call(
            "POST",
            "/api/v1/mcps",
            json={
                "name": "demo",
                "url": "https://mcp.example.com/api",
                "headers": {"Authorization": "Bearer secret"},
            },
        ),
        call("DELETE", "/api/v1/mcps/demo%2Fserver"),
        call("GET", "/api/v1/mcps"),
        call(
            "POST",
            "/api/v1/mcps/find",
            json={"query": "weather forecast", "limit": 5},
        ),
        call(
            "POST",
            "/api/v1/mcps/validate",
            json={
                "name": "demo",
                "url": "https://mcp.example.com/api",
                "headers": {},
            },
        ),
        call("GET", "/api/v1/mcps/demo%2Fserver"),
        call("POST", "/api/v1/mcps/demo%2Fserver/sync"),
        call("GET", "/api/v1/mcps/status", params={"name": "demo"}),
        call(
            "POST",
            "/api/v1/mcps/invoke",
            json={
                "uri": "viking://agent/tools/demo/current.json",
                "arguments": {"city": "Beijing"},
            },
        ),
        call(
            "POST",
            "/api/v1/mcps/mount",
            json={
                "tool_uri": "viking://agent/tools/demo/current.json",
                "node_uri": "viking://resources/demo/weather.md",
                "title": "Beijing Weather",
                "description": "Current weather in Beijing",
                "timeout_seconds": 120,
                "arguments": {"city": "Beijing"},
                "tags": ["project=demo", "kind=live-context"],
            },
        ),
        call(
            "DELETE",
            "/api/v1/mcps/mount",
            params={"node_uri": "viking://resources/demo/weather.md"},
        ),
        call(
            "GET",
            "/api/v1/mcps/mounts",
            params={"mcp": "demo", "tool": "current"},
        ),
    ]


@pytest.mark.asyncio
async def test_async_http_client_mcp_defaults_match_server_contract():
    client = AsyncHTTPClient(url="http://localhost:1933")
    fake_http = SimpleNamespace(request=AsyncMock(return_value=object()))
    client._http = fake_http
    client._handle_response = lambda _response: {}

    await client.invoke_mcp_tool("viking://agent/tools/demo/current.json")
    await client.mount_mcp_tool(
        "viking://agent/tools/demo/current.json",
        "viking://resources/demo/weather.md",
        "Weather",
    )
    await client.list_mcp_mounts("demo")

    assert fake_http.request.await_args_list == [
        call(
            "POST",
            "/api/v1/mcps/invoke",
            json={
                "uri": "viking://agent/tools/demo/current.json",
                "arguments": {},
            },
        ),
        call(
            "POST",
            "/api/v1/mcps/mount",
            json={
                "tool_uri": "viking://agent/tools/demo/current.json",
                "node_uri": "viking://resources/demo/weather.md",
                "title": "Weather",
                "description": None,
                "timeout_seconds": 60,
                "arguments": {},
            },
        ),
        call("GET", "/api/v1/mcps/mounts", params={"mcp": "demo"}),
    ]


def test_sync_http_client_exposes_and_forwards_mcp_operations():
    method_names = [
        "add_mcp",
        "remove_mcp",
        "list_mcps",
        "find_mcp_tools",
        "validate_mcp",
        "get_mcp",
        "sync_mcp",
        "get_mcp_status",
        "invoke_mcp_tool",
        "mount_mcp_tool",
        "unmount_mcp_tool",
        "list_mcp_mounts",
    ]
    assert all(name in SyncHTTPClient.__dict__ for name in method_names)

    client = SyncHTTPClient(url="http://localhost:1933")
    client._async_client.mount_mcp_tool = AsyncMock(return_value={"mounted": True})

    result = client.mount_mcp_tool(
        "viking://agent/tools/demo/current.json",
        "viking://resources/demo/weather.md",
        "Weather",
        description="Current weather",
        timeout_seconds=90,
        arguments={"city": "Beijing"},
        tags=["project=demo"],
    )

    assert result == {"mounted": True}
    client._async_client.mount_mcp_tool.assert_awaited_once_with(
        "viking://agent/tools/demo/current.json",
        "viking://resources/demo/weather.md",
        "Weather",
        description="Current weather",
        timeout_seconds=90,
        arguments={"city": "Beijing"},
        tags=["project=demo"],
    )
