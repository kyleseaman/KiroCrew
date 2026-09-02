"""Gateway-owned MCP Streamable HTTP transport for remote session relays."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import cast

import anyio
import httpx2
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.inbound import (
    MCP_METHOD_HEADER,
    MCP_NAME_HEADER,
    MCP_PROTOCOL_VERSION_HEADER,
    NAME_BEARING_METHODS,
    encode_header_value,
    find_invalid_x_mcp_header,
    mcp_param_headers,
    x_mcp_header_map,
)
from mcp.shared.message import ClientMessageMetadata, SessionMessage
from mcp.types import (
    JSONRPCError,
    JSONRPCMessage,
    JSONRPCNotification,
    JSONRPCRequest,
    JSONRPCResponse,
    jsonrpc_message_adapter,
)

from kiro_crew.mcp_gateway.remote_proxy import RemoteHttpMcpTarget

_RELAY_LINE_MAX_BYTES = 8 * 1024 * 1024
_HTTP_BODY_MAX_BYTES = 8 * 1024 * 1024
_HTTP_CONNECT_TIMEOUT_SECONDS = 10.0
_HTTP_WRITE_TIMEOUT_SECONDS = 10.0
_HTTP_POOL_TIMEOUT_SECONDS = 10.0
_HTTP_READ_TIMEOUT_SECONDS = 300.0
_HTTP_MAX_REDIRECTS = 3
_HTTP_CLIENT_MAX_HISTORY = 32
_PENDING_METHODS_MAX = 256
_REDIRECT_COUNT_EXTENSION = "kiro_crew.remote_mcp_redirect_count"
_REDIRECT_STATUSES = frozenset((301, 302, 303, 307, 308))
_CREDENTIAL_HEADER_NAMES = frozenset(("authorization", "cookie"))


class RemoteMcpTransportError(RuntimeError):
    """A bounded remote MCP failure with no endpoint or credential material."""


class _CappedResponseStream(httpx2.AsyncByteStream):
    def __init__(self, inner: httpx2.AsyncByteStream) -> None:
        self._inner = inner

    async def __aiter__(self):
        total = 0
        async for chunk in self._inner:
            total += len(chunk)
            if total > _HTTP_BODY_MAX_BYTES:
                raise RemoteMcpTransportError(GatewayMcpHttpAdapter.transport_error_code)
            yield chunk

    async def aclose(self) -> None:
        await self._inner.aclose()


def _origin(url: httpx2.URL) -> tuple[str, str, int | None]:
    return (url.scheme, url.host, url.port)


@dataclass
class _MessageState:
    protocol_version: str = ""
    pending_methods: dict[str | int, str] = field(default_factory=dict)
    tool_headers: dict[str, Mapping[tuple[str, ...], str]] = field(default_factory=dict)

    def outbound_metadata(self, message: JSONRPCMessage) -> ClientMessageMetadata | None:
        if not isinstance(message, (JSONRPCRequest, JSONRPCNotification)):
            return None
        method = message.method
        if isinstance(message, JSONRPCRequest):
            self.pending_methods[message.id] = method
            while len(self.pending_methods) > _PENDING_METHODS_MAX:
                self.pending_methods.pop(next(iter(self.pending_methods)))
        if method == "initialize":
            return None
        if not self.protocol_version:
            return None

        headers = {
            MCP_PROTOCOL_VERSION_HEADER: self.protocol_version,
            MCP_METHOD_HEADER: method,
        }
        params = message.params
        typed_params = params if isinstance(params, Mapping) else {}
        name_key = NAME_BEARING_METHODS.get(method)
        name = typed_params.get(name_key) if name_key is not None else None
        if isinstance(name, str):
            headers[MCP_NAME_HEADER] = encode_header_value(name)
        if method == "tools/call" and isinstance(name, str):
            arguments = typed_params.get("arguments")
            typed_arguments = arguments if isinstance(arguments, Mapping) else {}
            headers.update(mcp_param_headers(self.tool_headers.get(name, {}), typed_arguments))
        return ClientMessageMetadata(headers=headers)

    def observe_inbound(self, message: JSONRPCMessage) -> None:
        if not isinstance(message, (JSONRPCResponse, JSONRPCError)):
            return
        message_id = message.id
        if message_id is None:
            return
        method = self.pending_methods.pop(message_id, "")
        if not isinstance(message, JSONRPCResponse):
            return
        result = message.result
        if method == "initialize":
            version = result.get("protocolVersion")
            if isinstance(version, str):
                self.protocol_version = version
            return
        if method != "tools/list":
            return
        tools = result.get("tools")
        if not isinstance(tools, list):
            return
        discovered: dict[str, Mapping[tuple[str, ...], str]] = {}
        for tool in tools:
            if not isinstance(tool, Mapping):
                continue
            name = tool.get("name")
            schema = tool.get("inputSchema")
            if (
                isinstance(name, str)
                and isinstance(schema, Mapping)
                and find_invalid_x_mcp_header(schema) is None
            ):
                discovered[name] = x_mcp_header_map(schema)
        self.tool_headers = discovered


class GatewayMcpHttpAdapter:
    """Translate a relay's JSON-lines MCP channel to gateway HTTP transport."""

    transport_error_code = "remote_mcp_transport_failed"

    def __init__(self, oauth_manager=None) -> None:
        self._oauth_manager = oauth_manager

    async def run(
        self,
        target: RemoteHttpMcpTarget,
        session_key: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        first_message = await self._read_relay_message(reader)
        if first_message is None:
            return
        exact_url = httpx2.URL(target.url)
        configured_names = frozenset(name.lower() for name in target.headers)
        initial_post_status: int | None = None
        legacy_mode = False
        oauth_auth_active = False

        async def inject_configured_headers(request: httpx2.Request) -> None:
            request.extensions.setdefault(_REDIRECT_COUNT_EXTENSION, 0)
            if request.url == exact_url or (
                legacy_mode and _origin(request.url) == _origin(exact_url)
            ):
                for name, value in target.headers.items():
                    request.headers[name] = value

        async def enforce_response_policy(response: httpx2.Response) -> None:
            nonlocal initial_post_status
            if (
                initial_post_status is None
                and response.request.method == "POST"
                and response.request.url == exact_url
            ):
                initial_post_status = response.status_code
            if response.status_code in _REDIRECT_STATUSES:
                location = response.headers.get("location")
                if location:
                    redirect_count = response.request.extensions.get(
                        _REDIRECT_COUNT_EXTENSION,
                        0,
                    )
                    if not isinstance(redirect_count, int):
                        raise RemoteMcpTransportError(self.transport_error_code)
                    redirect_count += 1
                    response.request.extensions[_REDIRECT_COUNT_EXTENSION] = redirect_count
                    if redirect_count > _HTTP_MAX_REDIRECTS:
                        raise RemoteMcpTransportError(self.transport_error_code)
                    destination = response.url.join(location)
                    request_names = frozenset(response.request.headers.keys())
                    oauth_sensitive_post = (
                        oauth_auth_active
                        and response.request.method == "POST"
                        and response.request.url != exact_url
                    )
                    carries_credentials = (
                        bool(request_names & (_CREDENTIAL_HEADER_NAMES | configured_names))
                        or oauth_sensitive_post
                    )
                    if carries_credentials and _origin(destination) != _origin(response.url):
                        raise RemoteMcpTransportError(self.transport_error_code)
            if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != (
                "text/event-stream"
            ):
                response.stream = _CappedResponseStream(
                    cast(httpx2.AsyncByteStream, response.stream)
                )

        timeout = httpx2.Timeout(
            connect=_HTTP_CONNECT_TIMEOUT_SECONDS,
            read=_HTTP_READ_TIMEOUT_SECONDS,
            write=_HTTP_WRITE_TIMEOUT_SECONDS,
            pool=_HTTP_POOL_TIMEOUT_SECONDS,
        )
        oauth_manager = self._oauth_manager
        try:
            if oauth_manager is None:
                from kiro_crew.mcp_gateway.oauth import runtime_oauth_manager

                oauth_manager = runtime_oauth_manager()
            async with AsyncExitStack() as stack:
                auth = None
                if oauth_manager is not None:
                    auth = await stack.enter_async_context(
                        oauth_manager.interaction(target, session_key)
                    )
                    oauth_auth_active = auth is not None
                client = await stack.enter_async_context(
                    httpx2.AsyncClient(
                        auth=auth,
                        timeout=timeout,
                        verify=True,
                        follow_redirects=True,
                        max_redirects=_HTTP_CLIENT_MAX_HISTORY,
                        event_hooks={
                            "request": [inject_configured_headers],
                            "response": [enforce_response_policy],
                        },
                    )
                )
                state = _MessageState()
                fallback = False
                async with streamable_http_client(
                    target.url,
                    http_client=client,
                    terminate_on_close=True,
                ) as (http_reader, http_writer):
                    await http_writer.send(
                        SessionMessage(
                            message=first_message,
                            metadata=state.outbound_metadata(first_message),
                        )
                    )
                    first_item = await http_reader.receive()
                    fallback = self._should_fallback_to_legacy(
                        first_message,
                        first_item,
                        initial_post_status,
                    )
                    if not fallback:
                        await self._write_http_item(first_item, writer, state)
                        if oauth_manager is not None:
                            await oauth_manager.transport_succeeded()
                        await self._pump_transport(
                            reader,
                            writer,
                            http_reader,
                            http_writer,
                            state,
                        )

                if fallback:
                    legacy_mode = True

                    def legacy_client_factory(
                        headers: dict[str, str] | None = None,
                        timeout: httpx2.Timeout | None = None,
                        auth: httpx2.Auth | None = None,
                    ) -> httpx2.AsyncClient:
                        return httpx2.AsyncClient(
                            headers=headers,
                            timeout=timeout,
                            auth=auth,
                            follow_redirects=True,
                            max_redirects=_HTTP_CLIENT_MAX_HISTORY,
                            verify=True,
                            event_hooks={
                                "request": [inject_configured_headers],
                                "response": [enforce_response_policy],
                            },
                        )

                    state = _MessageState()
                    async with sse_client(
                        target.url,
                        headers=None,
                        timeout=_HTTP_CONNECT_TIMEOUT_SECONDS,
                        sse_read_timeout=_HTTP_READ_TIMEOUT_SECONDS,
                        httpx_client_factory=legacy_client_factory,
                        auth=auth,
                    ) as (sse_reader, sse_writer):
                        await sse_writer.send(
                            SessionMessage(
                                message=first_message,
                                metadata=state.outbound_metadata(first_message),
                            )
                        )
                        first_item = await sse_reader.receive()
                        await self._write_http_item(first_item, writer, state)
                        if oauth_manager is not None:
                            await oauth_manager.transport_succeeded()
                        await self._pump_transport(
                            reader,
                            writer,
                            sse_reader,
                            sse_writer,
                            state,
                        )
        except asyncio.CancelledError:
            if oauth_manager is not None:
                await oauth_manager.transport_failed()
            raise
        except RemoteMcpTransportError:
            if oauth_manager is not None:
                await oauth_manager.transport_failed()
            raise
        except Exception:
            if oauth_manager is not None:
                await oauth_manager.transport_failed()
            raise RemoteMcpTransportError(self.transport_error_code) from None

    @staticmethod
    def _should_fallback_to_legacy(
        first_message: JSONRPCMessage,
        first_item: SessionMessage | Exception,
        initial_post_status: int | None,
    ) -> bool:
        return (
            initial_post_status in (404, 405)
            and isinstance(first_message, JSONRPCRequest)
            and first_message.method == "initialize"
            and isinstance(first_item, SessionMessage)
            and isinstance(first_item.message, JSONRPCError)
            and first_item.message.id == first_message.id
        )

    async def _read_relay_message(
        self,
        reader: asyncio.StreamReader,
    ) -> JSONRPCMessage | None:
        try:
            line = await reader.readline()
        except (ValueError, asyncio.LimitOverrunError):
            raise RemoteMcpTransportError(self.transport_error_code) from None
        if not line:
            return None
        if len(line) > _RELAY_LINE_MAX_BYTES or not line.endswith(b"\n"):
            raise RemoteMcpTransportError(self.transport_error_code)
        try:
            return jsonrpc_message_adapter.validate_json(line, by_name=False)
        except ValueError:
            raise RemoteMcpTransportError(self.transport_error_code) from None

    async def _pump_transport(
        self,
        reader,
        writer,
        http_reader,
        http_writer,
        state: _MessageState,
    ) -> None:
        upstream = asyncio.create_task(self._relay_to_http(reader, http_writer, state))
        downstream = asyncio.create_task(self._http_to_relay(http_reader, writer, state))
        done, pending = await asyncio.wait(
            (upstream, downstream),
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            task.result()

    async def _relay_to_http(self, reader, http_writer, state: _MessageState) -> None:
        while True:
            message = await self._read_relay_message(reader)
            if message is None:
                return
            metadata = state.outbound_metadata(message)
            await http_writer.send(SessionMessage(message=message, metadata=metadata))

    async def _http_to_relay(self, http_reader, writer, state: _MessageState) -> None:
        while True:
            try:
                item = await http_reader.receive()
            except (anyio.EndOfStream, anyio.ClosedResourceError):
                return
            await self._write_http_item(item, writer, state)

    async def _write_http_item(self, item, writer, state: _MessageState) -> None:
        if isinstance(item, Exception):
            raise RemoteMcpTransportError(self.transport_error_code)
        state.observe_inbound(item.message)
        payload = item.message.model_dump_json(
            by_alias=True,
            exclude_none=True,
        ).encode("utf-8")
        if len(payload) > _RELAY_LINE_MAX_BYTES:
            raise RemoteMcpTransportError(self.transport_error_code)
        writer.write(payload + b"\n")
        await writer.drain()
