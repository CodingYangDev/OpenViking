# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""MCP server registry and tool invocation endpoints."""

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from openviking.server.auth import get_request_context
from openviking.server.dependencies import get_service
from openviking.server.identity import RequestContext
from openviking.server.models import Response

router = APIRouter(prefix="/api/v1/mcps", tags=["mcps"])


class AddMCPRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    url: str
    headers: dict[str, str] = Field(default_factory=dict)


class FindMCPToolsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    limit: int = Field(default=10, ge=1, le=100)


class InvokeMCPToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    uri: str
    arguments: dict[str, Any] = Field(default_factory=dict)


@router.post("")
async def add_mcp(
    body: AddMCPRequest,
    ctx: RequestContext = Depends(get_request_context),
):
    result = await get_service().mcps.add(body.name, body.url, body.headers, ctx)
    return Response(status="ok", result=result)


@router.delete("/{name}")
async def remove_mcp(
    name: str,
    ctx: RequestContext = Depends(get_request_context),
):
    result = await get_service().mcps.remove(name, ctx)
    return Response(status="ok", result=result)


@router.get("")
async def list_mcps(ctx: RequestContext = Depends(get_request_context)):
    result = await get_service().mcps.list(ctx)
    return Response(status="ok", result=result)


@router.post("/find")
async def find_mcp_tools(
    body: FindMCPToolsRequest,
    ctx: RequestContext = Depends(get_request_context),
):
    result = await get_service().mcps.find(body.query, body.limit, ctx)
    return Response(status="ok", result=result)


@router.post("/invoke")
async def invoke_mcp_tool(
    body: InvokeMCPToolRequest,
    ctx: RequestContext = Depends(get_request_context),
):
    result = await get_service().mcps.invoke(body.uri, body.arguments, ctx)
    return Response(status="ok", result=result)


@router.get("/status")
async def mcp_status(
    name: str = Query(),
    ctx: RequestContext = Depends(get_request_context),
):
    result = await get_service().mcps.status(name, ctx)
    return Response(status="ok", result=result)


@router.get("/{name}")
async def show_mcp(
    name: str,
    ctx: RequestContext = Depends(get_request_context),
):
    result = await get_service().mcps.show(name, ctx)
    return Response(status="ok", result=result)


@router.post("/validate")
async def validate_mcp(
    body: AddMCPRequest,
    _ctx: RequestContext = Depends(get_request_context),
):
    result = await get_service().mcps.validate(body.name, body.url, body.headers)
    return Response(status="ok", result=result)


@router.post("/{name}/sync")
async def sync_mcp(
    name: str,
    ctx: RequestContext = Depends(get_request_context),
):
    result = await get_service().mcps.sync(name, ctx)
    return Response(status="ok", result=result)
