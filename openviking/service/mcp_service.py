# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Account-scoped MCP server registry and tool catalog management."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, AsyncIterator
from urllib.parse import urlsplit

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, InitializeResult, Tool

from openviking.pyagfs import AGFSNotFoundError, AsyncAGFSClient
from openviking.server.identity import RequestContext
from openviking_cli.exceptions import (
    AlreadyExistsError,
    InvalidArgumentError,
    NotFoundError,
    UnavailableError,
)

_SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_TOOL_URI_RE = re.compile(
    r"^viking://agent/tools/(?P<server>[A-Za-z0-9_-]{1,64})/"
    r"(?P<tool>[A-Za-z0-9_.-]{1,128})\.json$"
)
_TOOLS_ROOT = "viking://agent/tools"
_REGISTRY_VERSION = 1
_DEFAULT_TIMEOUT_SECONDS = 30.0


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _decode_read_result(result: Any) -> bytes:
    if isinstance(result, bytes):
        return result
    content = getattr(result, "content", None)
    if isinstance(content, bytes):
        return content
    if isinstance(content, str):
        return content.encode("utf-8")
    if isinstance(result, str):
        return result.encode("utf-8")
    raise ValueError("MCP registry content must be bytes or JSON text")


class MCPService:
    """Manage remote Streamable HTTP MCP servers for one OpenViking account."""

    def __init__(self, service: Any) -> None:
        self._service = service
        self._async_agfs: AsyncAGFSClient | None = None
        self._locks: dict[str, asyncio.Lock] = {}

    def _agfs(self) -> AsyncAGFSClient:
        if self._async_agfs is None:
            agfs = self._service._agfs
            if agfs is None:
                raise RuntimeError("AGFS is not initialized")
            self._async_agfs = agfs if isinstance(agfs, AsyncAGFSClient) else AsyncAGFSClient(agfs)
        return self._async_agfs

    def _lock(self, account_id: str) -> asyncio.Lock:
        return self._locks.setdefault(account_id, asyncio.Lock())

    @staticmethod
    def _registry_path(account_id: str) -> str:
        return f"/local/{account_id}/_system/mcps.json"

    @staticmethod
    def _empty_document() -> dict[str, Any]:
        return {"version": _REGISTRY_VERSION, "servers": {}}

    async def _read_document(self, path: str) -> dict[str, Any]:
        try:
            raw = _decode_read_result(await self._agfs().read(path))
        except (AGFSNotFoundError, FileNotFoundError):
            return self._empty_document()
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Invalid MCP registry document: {path}") from exc
        if not isinstance(document, dict) or not isinstance(document.get("servers"), dict):
            raise RuntimeError(f"Invalid MCP registry document: {path}")
        return document

    async def _write_document(self, path: str, document: dict[str, Any]) -> None:
        await self._agfs().ensure_parent_dirs(path)
        payload = json.dumps(
            document,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        await self._agfs().write(path, payload)

    async def _load(self, ctx: RequestContext) -> dict[str, Any]:
        registry = await self._read_document(self._registry_path(ctx.account_id))
        for server in registry["servers"].values():
            if isinstance(server, dict):
                server.pop("tools", None)
        return registry

    @staticmethod
    def _validate_server_name(name: str) -> str:
        if not _SERVER_NAME_RE.fullmatch(name):
            raise InvalidArgumentError(
                "MCP server name must contain only letters, numbers, '_' or '-' (max 64)"
            )
        return name

    @staticmethod
    def _validate_url(url: str) -> str:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        if parsed.username or parsed.password or parsed.fragment:
            raise InvalidArgumentError("MCP URL must not contain credentials or a fragment")
        if parsed.scheme == "https" and host:
            return url
        if parsed.scheme == "http" and host in {"localhost", "127.0.0.1", "::1"}:
            return url
        raise InvalidArgumentError("MCP URL must use HTTPS; HTTP is allowed only for localhost")

    @staticmethod
    def _validate_headers(headers: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for raw_name, raw_value in headers.items():
            name = raw_name.strip()
            value = raw_value.strip()
            if (
                not name
                or not value
                or any(char in name for char in "\r\n:")
                or any(char in value for char in "\r\n")
            ):
                raise InvalidArgumentError("Invalid MCP HTTP header")
            normalized[name] = value
        return normalized

    @staticmethod
    def _public_server(server: dict[str, Any], tool_count: int) -> dict[str, Any]:
        return {
            key: value
            for key, value in server.items()
            if key not in {"header_names", "headers", "tools"}
        } | {
            "header_names": sorted(server.get("headers", {})),
            "tool_count": tool_count,
        }

    @staticmethod
    def _tool_revision(tool: dict[str, Any]) -> str:
        revision_input = {
            "name": tool.get("name"),
            "inputSchema": tool.get("inputSchema", {}),
        }
        payload = json.dumps(
            revision_input,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _tool_document(server: dict[str, Any], tool: Tool) -> dict[str, Any]:
        raw_tool = tool.model_dump(mode="json", by_alias=True, exclude_none=True)
        return {
            "kind": "mcp_tool",
            "server": server["name"],
            "address": server["url"],
            "revision": MCPService._tool_revision(raw_tool),
            "tool": raw_tool,
        }

    @asynccontextmanager
    async def _session(
        self,
        server: dict[str, Any],
        headers: dict[str, str],
    ) -> AsyncIterator[ClientSession]:
        timeout = httpx.Timeout(_DEFAULT_TIMEOUT_SECONDS, read=_DEFAULT_TIMEOUT_SECONDS)
        try:
            async with httpx.AsyncClient(headers=headers, timeout=timeout) as http_client:
                async with streamable_http_client(
                    server["url"],
                    http_client=http_client,
                ) as (read, write, _):
                    async with ClientSession(
                        read,
                        write,
                        read_timeout_seconds=timedelta(seconds=_DEFAULT_TIMEOUT_SECONDS),
                    ) as session:
                        yield session
        except (InvalidArgumentError, NotFoundError, UnavailableError):
            raise
        except Exception as exc:
            raise UnavailableError(
                "MCP server",
                f"connection failed ({type(exc).__name__}); check endpoint and credentials",
            ) from exc

    async def _discover(
        self,
        server: dict[str, Any],
        headers: dict[str, str],
    ) -> tuple[InitializeResult, list[Tool]]:
        async with self._session(server, headers) as session:
            initialized = await session.initialize()
            tools: list[Tool] = []
            cursor: str | None = None
            seen: set[str] = set()
            for _ in range(100):
                result = await session.list_tools(cursor=cursor)
                tools.extend(result.tools)
                cursor = result.nextCursor
                if not cursor:
                    return initialized, tools
                if cursor in seen:
                    raise InvalidArgumentError(
                        "MCP server returned a repeated tool pagination cursor"
                    )
                seen.add(cursor)
            raise InvalidArgumentError("MCP tool catalog exceeded 100 pages")

    async def _ensure_directory(self, uri: str, ctx: RequestContext) -> None:
        if await self._service.viking_fs.exists(uri, ctx=ctx):
            return
        try:
            await self._service.fs.mkdir(uri, ctx=ctx)
        except AlreadyExistsError:
            return
        except Exception as exc:
            if "already exists" not in str(exc).lower():
                raise

    async def _publish_catalog(
        self,
        server_name: str,
        tools: list[dict[str, Any]],
        existing_names: set[str],
        ctx: RequestContext,
    ) -> None:
        server_root = f"{_TOOLS_ROOT}/{server_name}"
        await self._ensure_directory(_TOOLS_ROOT, ctx)
        await self._ensure_directory(server_root, ctx)

        operations = [
            {
                "uri": f"{server_root}/{item['tool']['name']}.json",
                "content": json.dumps(item, ensure_ascii=False, indent=2, sort_keys=True),
                "mode": "upsert",
            }
            for item in tools
        ]
        if operations:
            await self._service.fs.batch_write(
                root_uri=server_root,
                operations=operations,
                ctx=ctx,
                wait=True,
            )

        current_names = {item["tool"]["name"] for item in tools}
        for name in sorted(existing_names - current_names):
            await self._service.fs.rm(
                f"{server_root}/{name}.json",
                ctx=ctx,
                recursive=False,
                wait=True,
            )

    async def _catalog_uris(self, server_name: str, ctx: RequestContext) -> list[str]:
        root = f"{_TOOLS_ROOT}/{server_name}"
        uris: list[str] = []
        offset = 0
        while True:
            try:
                page = await self._service.fs.ls(
                    root,
                    ctx=ctx,
                    output="original",
                    node_limit=1000,
                    offset=offset,
                )
            except (AGFSNotFoundError, NotFoundError):
                return []
            entries = page.entries
            uris.extend(
                entry["uri"]
                for entry in entries
                if not entry.get("isDir", False)
                and isinstance(entry.get("uri"), str)
                and entry["uri"].endswith(".json")
            )
            if not page.has_more:
                return uris
            if not entries:
                raise RuntimeError(f"Unable to paginate MCP tool catalog: {root}")
            offset += len(entries)

    async def _catalog_names(self, server_name: str, ctx: RequestContext) -> set[str]:
        prefix = f"{_TOOLS_ROOT}/{server_name}/"
        return {
            uri[len(prefix) : -len(".json")]
            for uri in await self._catalog_uris(server_name, ctx)
            if uri.startswith(prefix)
        }

    async def _catalog(self, server_name: str, ctx: RequestContext) -> list[dict[str, Any]]:
        uris = await self._catalog_uris(server_name, ctx)
        contents = await asyncio.gather(*(self._service.fs.read(uri, ctx=ctx) for uri in uris))
        documents: list[dict[str, Any]] = []
        for uri, content in zip(uris, contents, strict=True):
            try:
                document = json.loads(content)
                tool = document["tool"]
                tool_name = tool["name"]
            except (KeyError, TypeError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"Invalid MCP tool document: {uri}") from exc
            expected_uri = f"{_TOOLS_ROOT}/{server_name}/{tool_name}.json"
            if (
                not isinstance(document, dict)
                or not isinstance(tool, dict)
                or not isinstance(tool_name, str)
                or document.get("server") != server_name
                or uri != expected_uri
            ):
                raise RuntimeError(f"Invalid MCP tool document: {uri}")
            documents.append({"uri": uri, **document})
        return documents

    async def add(
        self,
        name: str,
        url: str,
        headers: dict[str, str],
        ctx: RequestContext,
    ) -> dict[str, Any]:
        name = self._validate_server_name(name)
        url = self._validate_url(url)
        headers = self._validate_headers(headers)
        candidate = {"name": name, "url": url}
        initialized, discovered = await self._discover(candidate, headers)
        tools = [self._tool_document(candidate, tool) for tool in discovered]
        invalid_names = [
            tool["tool"]["name"]
            for tool in tools
            if not self._valid_tool_name(tool["tool"]["name"])
        ]
        if invalid_names:
            raise InvalidArgumentError(
                f"MCP tool names are not valid OpenViking filenames: {', '.join(invalid_names)}"
            )

        async with self._lock(ctx.account_id):
            registry = await self._load(ctx)
            if name in registry["servers"]:
                raise AlreadyExistsError(name, "MCP server")
            timestamp = _now()
            server = {
                "name": name,
                "url": url,
                "headers": headers,
                "created_at": timestamp,
                "updated_at": timestamp,
            }
            await self._publish_catalog(name, tools, set(), ctx)
            registry["servers"][name] = server
            await self._write_document(self._registry_path(ctx.account_id), registry)

        return {
            **self._public_server(server, len(tools)),
            "protocol_version": initialized.protocolVersion,
        }

    async def remove(self, name: str, ctx: RequestContext) -> dict[str, Any]:
        name = self._validate_server_name(name)
        async with self._lock(ctx.account_id):
            registry = await self._load(ctx)
            if name not in registry["servers"]:
                raise NotFoundError(name, "MCP server")
            await self._service.fs.rm(
                f"{_TOOLS_ROOT}/{name}",
                ctx=ctx,
                recursive=True,
                wait=True,
            )
            registry["servers"].pop(name)
            await self._write_document(self._registry_path(ctx.account_id), registry)
        return {"name": name, "removed": True}

    async def list(self, ctx: RequestContext) -> dict[str, Any]:
        registry = await self._load(ctx)
        items = sorted(registry["servers"].items())
        counts = await asyncio.gather(*(self._catalog_names(name, ctx) for name, _ in items))
        servers = [
            self._public_server(server, len(tool_names))
            for (_, server), tool_names in zip(items, counts, strict=True)
        ]
        return {"servers": servers, "total": len(servers)}

    async def show(self, name: str, ctx: RequestContext) -> dict[str, Any]:
        name = self._validate_server_name(name)
        registry = await self._load(ctx)
        server = registry["servers"].get(name)
        if server is None:
            raise NotFoundError(name, "MCP server")
        tools = await self._catalog(name, ctx)
        return {
            **self._public_server(server, len(tools)),
            "tools": tools,
        }

    async def find(
        self,
        query: str,
        limit: int,
        ctx: RequestContext,
    ) -> dict[str, Any]:
        query = query.strip()
        if not query:
            raise InvalidArgumentError("Tool search query must not be empty")
        registry = await self._load(ctx)
        terms = {term for term in re.split(r"\W+", query.casefold()) if term}
        results: list[dict[str, Any]] = []
        names = sorted(registry["servers"])
        catalogs = await asyncio.gather(*(self._catalog(name, ctx) for name in names))
        for name, documents in zip(names, catalogs, strict=True):
            for document in documents:
                tool = document["tool"]
                haystack = f"{tool.get('name', '')} {tool.get('description', '')}".casefold()
                exact = query.casefold() in haystack
                matches = sum(term in haystack for term in terms)
                if not exact and matches == 0:
                    continue
                score = 1.0 if exact else matches / max(len(terms), 1)
                results.append(
                    {
                        "uri": document["uri"],
                        "server": name,
                        "name": tool["name"],
                        "description": tool.get("description", ""),
                        "revision": document["revision"],
                        "score": round(score, 6),
                    }
                )
        results.sort(key=lambda item: (-item["score"], item["server"], item["name"]))
        return {"tools": results[:limit], "total": len(results)}

    async def _registered(
        self,
        name: str,
        ctx: RequestContext,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        name = self._validate_server_name(name)
        registry = await self._load(ctx)
        server = registry["servers"].get(name)
        if server is None:
            raise NotFoundError(name, "MCP server")
        headers = server.get("headers", {})
        return server, headers

    async def validate(
        self,
        name: str,
        url: str,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        name = self._validate_server_name(name)
        server = {"name": name, "url": self._validate_url(url)}
        headers = self._validate_headers(headers)
        initialized, tools = await self._discover(server, headers)
        return {
            "name": name,
            "valid": True,
            "protocol_version": initialized.protocolVersion,
            "server_info": initialized.serverInfo.model_dump(mode="json"),
            "tool_count": len(tools),
        }

    async def sync(self, name: str, ctx: RequestContext) -> dict[str, Any]:
        async with self._lock(ctx.account_id):
            server, headers = await self._registered(name, ctx)
            initialized, discovered = await self._discover(server, headers)
            tools = [self._tool_document(server, tool) for tool in discovered]
            invalid_names = [
                tool["tool"]["name"]
                for tool in tools
                if not self._valid_tool_name(tool["tool"]["name"])
            ]
            if invalid_names:
                raise InvalidArgumentError(
                    f"MCP tool names are not valid OpenViking filenames: {', '.join(invalid_names)}"
                )
            existing_names = await self._catalog_names(name, ctx)
            await self._publish_catalog(name, tools, existing_names, ctx)
            registry = await self._load(ctx)
            current = registry["servers"][name]
            current.pop("tools", None)
            current["updated_at"] = _now()
            await self._write_document(self._registry_path(ctx.account_id), registry)
        return {
            "name": name,
            "synced": True,
            "protocol_version": initialized.protocolVersion,
            "tool_count": len(tools),
        }

    @staticmethod
    def _valid_tool_name(name: str) -> bool:
        return name not in {".", ".."} and _TOOL_NAME_RE.fullmatch(name) is not None

    async def status(self, name: str, ctx: RequestContext) -> dict[str, Any]:
        name = self._validate_server_name(name)
        registry = await self._load(ctx)
        server = registry["servers"].get(name)
        if server is None:
            raise NotFoundError(name, "MCP server")
        headers = server.get("headers", {})
        try:
            initialized, tools = await self._discover(server, headers)
            return {
                "name": name,
                "status": "running",
                "protocol_version": initialized.protocolVersion,
                "tool_count": len(tools),
            }
        except (InvalidArgumentError, UnavailableError) as exc:
            tool_names = await self._catalog_names(name, ctx)
            return {
                "name": name,
                "status": "error",
                "error": str(exc),
                "tool_count": len(tool_names),
            }

    async def invoke(
        self,
        uri: str,
        arguments: dict[str, Any],
        ctx: RequestContext,
    ) -> dict[str, Any]:
        match = _TOOL_URI_RE.fullmatch(uri)
        if match is None:
            raise InvalidArgumentError(
                "Tool URI must match viking://agent/tools/<server>/<tool>.json"
            )
        server_name = match.group("server")
        tool_name = match.group("tool")
        server, headers = await self._registered(server_name, ctx)
        if tool_name not in await self._catalog_names(server_name, ctx):
            raise NotFoundError(uri, "MCP tool")

        async with self._session(server, headers) as session:
            await session.initialize()
            try:
                result: CallToolResult = await session.call_tool(
                    tool_name,
                    arguments=arguments,
                )
            except Exception as exc:
                raise UnavailableError(
                    "MCP tool",
                    f"call failed ({type(exc).__name__}); outcome may be unknown and was not retried",
                ) from exc
        return {
            "uri": uri,
            "server": server_name,
            "tool": tool_name,
            "result": result.model_dump(mode="json", by_alias=True, exclude_none=True),
        }
