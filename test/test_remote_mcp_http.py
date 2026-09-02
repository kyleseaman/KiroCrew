"""Gateway-owned HTTP transport for remote MCP capabilities."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import pytest
from aiohttp import web
from mcp.shared.inbound import MCP_METHOD_HEADER
from mcp.types import JSONRPCRequest

from kiro_crew.mcp_gateway.remote_http import GatewayMcpHttpAdapter, _MessageState
from kiro_crew.mcp_gateway.remote_proxy import (
    RemoteHttpMcpTarget,
    RemoteMcpProxy,
)


@asynccontextmanager
async def _http_server(
    handler,
) -> AsyncIterator[str]:
    app = web.Application()
    app.router.add_route("*", "/mcp", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    sockets = site._server.sockets
    assert sockets
    try:
        yield f"http://127.0.0.1:{sockets[0].getsockname()[1]}/mcp"
    finally:
        await runner.cleanup()


@asynccontextmanager
async def _running_app(app: web.Application) -> AsyncIterator[str]:
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    sockets = site._server.sockets
    assert sockets
    try:
        yield f"http://127.0.0.1:{sockets[0].getsockname()[1]}"
    finally:
        await runner.cleanup()


async def _connect(
    proxy: RemoteMcpProxy,
    token: str,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy.local_port)
    writer.write(json.dumps({"version": 1, "token": token}).encode() + b"\n")
    await writer.drain()
    return reader, writer


@pytest.mark.asyncio
async def test_http_capability_round_trips_json_rpc_with_gateway_headers() -> None:
    seen: list[tuple[dict[str, object], str | None]] = []

    async def handler(request: web.Request) -> web.Response:
        payload = await request.json()
        seen.append((payload, request.headers.get("X-Gateway-Only")))
        return web.json_response(
            {
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "serverInfo": {"name": "test", "version": "1"},
                },
            }
        )

    async with _http_server(handler) as url:
        target = RemoteHttpMcpTarget(
            server_name="remote-http",
            url=url,
            headers={"X-Gateway-Only": "secret-canary"},
        )
        proxy = RemoteMcpProxy()
        await proxy.start()
        grant = proxy.mint("dashboard:one", target)
        reader = None
        writer = None
        request = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "relay-test", "version": "1"},
            },
        }
        try:
            reader, writer = await _connect(proxy, grant.token)
            writer.write(json.dumps(request).encode() + b"\n")
            await writer.drain()

            response = json.loads(await asyncio.wait_for(reader.readline(), timeout=5))

            assert response["id"] == 7
            assert response["result"]["serverInfo"]["name"] == "test"
            assert seen == [(request, "secret-canary")]
            assert "secret-canary" not in json.dumps(response)
        finally:
            if writer is not None:
                writer.close()
                await writer.wait_closed()
            await proxy.close()


@pytest.mark.asyncio
async def test_http_adapter_rejects_oversized_or_malformed_relay_lines() -> None:
    requests = 0

    async def handler(request: web.Request) -> web.Response:
        nonlocal requests
        requests += 1
        return web.Response(status=500)

    async with _http_server(handler) as url:
        target = RemoteHttpMcpTarget(server_name="remote-http", url=url, headers={})
        for payload in (b"not-json\n", b"x" * (8 * 1024 * 1024 + 1) + b"\n"):
            proxy = RemoteMcpProxy()
            await proxy.start()
            grant = proxy.mint("dashboard:one", target)
            reader = None
            writer = None
            try:
                reader, writer = await _connect(proxy, grant.token)
                writer.write(payload)
                await writer.drain()

                assert await asyncio.wait_for(reader.read(), timeout=5) == b""
            finally:
                if writer is not None:
                    writer.close()
                    await writer.wait_closed()
                await proxy.close()

    assert requests == 0


def test_http_adapter_has_bounded_public_failure_codes() -> None:
    assert GatewayMcpHttpAdapter.transport_error_code == "remote_mcp_transport_failed"


def test_message_state_reads_typed_rpc_without_serializing(monkeypatch) -> None:
    request = JSONRPCRequest(jsonrpc="2.0", id=1, method="tools/list", params={})

    def refuse_dump(*_args, **_kwargs):
        raise AssertionError("routing metadata must not serialize the whole message")

    monkeypatch.setattr(JSONRPCRequest, "model_dump", refuse_dump)

    metadata = _MessageState(protocol_version="2026-07-28").outbound_metadata(request)

    assert metadata is not None
    assert metadata.headers[MCP_METHOD_HEADER] == "tools/list"


def test_message_state_bounds_unanswered_request_tracking() -> None:
    state = _MessageState(protocol_version="2026-07-28")

    for request_id in range(300):
        state.outbound_metadata(
            JSONRPCRequest(
                jsonrpc="2.0",
                id=request_id,
                method="tools/list",
                params={},
            )
        )

    assert len(state.pending_methods) == 256
    assert 0 not in state.pending_methods
    assert 299 in state.pending_methods


@pytest.mark.asyncio
async def test_http_adapter_derives_modern_routing_headers_from_mcp_state() -> None:
    seen: list[dict[str, str]] = []
    connections: set[int] = set()

    async def handler(request: web.Request) -> web.Response:
        assert request.transport is not None
        connections.add(id(request.transport))
        payload = await request.json()
        seen.append(
            {
                name: request.headers.get(name, "")
                for name in (
                    "Mcp-Protocol-Version",
                    "Mcp-Method",
                    "Mcp-Name",
                    "Mcp-Param-Tenant",
                )
            }
        )
        method = payload["method"]
        if method == "initialize":
            result = {
                "protocolVersion": "2026-07-28",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "test", "version": "1"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "lookup",
                        "description": "Lookup",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "tenant": {
                                    "type": "string",
                                    "x-mcp-header": "Tenant",
                                }
                            },
                        },
                    }
                ]
            }
        else:
            result = {"content": [{"type": "text", "text": "ok"}]}
        return web.json_response({"jsonrpc": "2.0", "id": payload["id"], "result": result})

    async with _http_server(handler) as url:
        proxy = RemoteMcpProxy()
        await proxy.start()
        grant = proxy.mint(
            "dashboard:one",
            RemoteHttpMcpTarget(server_name="remote-http", url=url, headers={}),
        )
        reader, writer = await _connect(proxy, grant.token)
        requests = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2026-07-28",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "lookup", "arguments": {"tenant": "acme"}},
            },
        ]
        try:
            for request in requests:
                writer.write(json.dumps(request).encode() + b"\n")
                await writer.drain()
                assert json.loads(await asyncio.wait_for(reader.readline(), timeout=5))["id"] == (
                    request["id"]
                )
        finally:
            writer.close()
            await writer.wait_closed()
            await proxy.close()

    assert seen[0]["Mcp-Protocol-Version"] == ""
    assert seen[1]["Mcp-Protocol-Version"] == "2026-07-28"
    assert seen[1]["Mcp-Method"] == "tools/list"
    assert seen[2]["Mcp-Method"] == "tools/call"
    assert seen[2]["Mcp-Name"] == "lookup"
    assert seen[2]["Mcp-Param-Tenant"] == "acme"
    assert len(connections) == 1


@pytest.mark.asyncio
async def test_http_adapter_refuses_cross_origin_redirect_with_configured_header() -> None:
    destination_requests = 0

    async def destination(request: web.Request) -> web.Response:
        nonlocal destination_requests
        destination_requests += 1
        return web.Response(status=204)

    destination_app = web.Application()
    destination_app.router.add_post("/mcp", destination)
    async with _running_app(destination_app) as destination_origin:

        async def redirect(request: web.Request) -> web.Response:
            raise web.HTTPTemporaryRedirect(location=f"{destination_origin}/mcp")

        source_app = web.Application()
        source_app.router.add_post("/mcp", redirect)
        async with _running_app(source_app) as source_origin:
            proxy = RemoteMcpProxy()
            await proxy.start()
            grant = proxy.mint(
                "dashboard:one",
                RemoteHttpMcpTarget(
                    server_name="remote-http",
                    url=f"{source_origin}/mcp",
                    headers={"X-Gateway-Only": "redirect-secret-canary"},
                ),
            )
            reader, writer = await _connect(proxy, grant.token)
            try:
                writer.write(
                    json.dumps(
                        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
                    ).encode()
                    + b"\n"
                )
                await writer.drain()

                response = await asyncio.wait_for(reader.read(), timeout=5)

                assert b"redirect-secret-canary" not in response
                assert destination_requests == 0
            finally:
                writer.close()
                await writer.wait_closed()
                await proxy.close()


@pytest.mark.asyncio
async def test_http_adapter_refuses_more_than_three_same_origin_redirects() -> None:
    requests = 0
    app = web.Application()

    async def redirect(request: web.Request) -> web.Response:
        nonlocal requests
        requests += 1
        step = int(request.match_info["step"])
        if step < 4:
            raise web.HTTPTemporaryRedirect(location=f"/redirect/{step + 1}")
        return web.Response(status=204)

    app.router.add_post("/redirect/{step}", redirect)
    async with _running_app(app) as origin:
        proxy = RemoteMcpProxy()
        await proxy.start()
        grant = proxy.mint(
            "dashboard:one",
            RemoteHttpMcpTarget(
                server_name="remote-http",
                url=f"{origin}/redirect/0",
                headers={},
            ),
        )
        reader, writer = await _connect(proxy, grant.token)
        try:
            writer.write(
                json.dumps(
                    {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
                ).encode()
                + b"\n"
            )
            await writer.drain()

            assert await asyncio.wait_for(reader.read(), timeout=5) == b""
            assert requests == 4
        finally:
            writer.close()
            await writer.wait_closed()
            await proxy.close()


@pytest.mark.asyncio
async def test_http_adapter_caps_non_streaming_response_body() -> None:
    body_canary = "body-secret-canary"

    async def handler(request: web.Request) -> web.Response:
        del request
        return web.json_response(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"value": body_canary + "x" * (8 * 1024 * 1024)},
            }
        )

    async with _http_server(handler) as url:
        assert urlsplit(url).hostname == "127.0.0.1"
        proxy = RemoteMcpProxy()
        await proxy.start()
        grant = proxy.mint(
            "dashboard:one",
            RemoteHttpMcpTarget(server_name="remote-http", url=url, headers={}),
        )
        reader, writer = await _connect(proxy, grant.token)
        try:
            writer.write(
                json.dumps(
                    {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
                ).encode()
                + b"\n"
            )
            await writer.drain()

            response = await asyncio.wait_for(reader.read(), timeout=5)

            assert body_canary.encode() not in response
        finally:
            writer.close()
            await writer.wait_closed()
            await proxy.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("modern_status", [404, 405])
async def test_http_adapter_falls_back_to_legacy_sse_only_for_protocol_status(
    modern_status: int,
) -> None:
    modern_posts = 0
    legacy_gets = 0
    legacy_posts = 0
    stream_ready = asyncio.Event()
    stream_closed = asyncio.Event()
    stream_response: web.StreamResponse | None = None

    async def modern(request: web.Request) -> web.StreamResponse:
        nonlocal modern_posts, legacy_gets, stream_response
        if request.method == "POST":
            modern_posts += 1
            return web.Response(status=modern_status)
        legacy_gets += 1
        stream_response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await stream_response.prepare(request)
        await stream_response.write(b"event: endpoint\ndata: /messages\n\n")
        stream_ready.set()
        await stream_closed.wait()
        return stream_response

    async def messages(request: web.Request) -> web.Response:
        nonlocal legacy_posts
        legacy_posts += 1
        payload = await request.json()
        await stream_ready.wait()
        assert stream_response is not None
        response = {
            "jsonrpc": "2.0",
            "id": payload["id"],
            "result": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "serverInfo": {"name": "legacy", "version": "1"},
            },
        }
        await stream_response.write(
            b"event: message\ndata: " + json.dumps(response).encode() + b"\n\n"
        )
        return web.Response(status=202)

    app = web.Application()
    app.router.add_route("*", "/mcp", modern)
    app.router.add_post("/messages", messages)
    async with _running_app(app) as origin:
        proxy = RemoteMcpProxy()
        await proxy.start()
        grant = proxy.mint(
            "dashboard:one",
            RemoteHttpMcpTarget(
                server_name="legacy",
                url=f"{origin}/mcp",
                headers={},
            ),
        )
        reader, writer = await _connect(proxy, grant.token)
        try:
            writer.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 7,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-11-25",
                            "capabilities": {},
                            "clientInfo": {"name": "test", "version": "1"},
                        },
                    }
                ).encode()
                + b"\n"
            )
            await writer.drain()

            response = json.loads(await asyncio.wait_for(reader.readline(), timeout=5))

            assert response["id"] == 7
            assert response["result"]["serverInfo"]["name"] == "legacy"
            assert modern_posts == 1
            assert legacy_gets == 1
            assert legacy_posts == 1
        finally:
            stream_closed.set()
            writer.close()
            await writer.wait_closed()
            await proxy.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("modern_status", [400, 401, 403, 429, 500])
async def test_http_adapter_does_not_fallback_for_non_protocol_http_failures(
    modern_status: int,
) -> None:
    legacy_gets = 0

    async def handler(request: web.Request) -> web.Response:
        nonlocal legacy_gets
        if request.method == "GET":
            legacy_gets += 1
        return web.Response(status=modern_status)

    async with _http_server(handler) as url:
        proxy = RemoteMcpProxy()
        await proxy.start()
        grant = proxy.mint(
            "dashboard:one",
            RemoteHttpMcpTarget(server_name="remote-http", url=url, headers={}),
        )
        reader, writer = await _connect(proxy, grant.token)
        try:
            writer.write(
                json.dumps(
                    {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
                ).encode()
                + b"\n"
            )
            await writer.drain()

            await asyncio.wait_for(reader.readline(), timeout=5)

            assert legacy_gets == 0
        finally:
            writer.close()
            await writer.wait_closed()
            await proxy.close()
