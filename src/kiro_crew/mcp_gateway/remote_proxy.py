"""Gateway-owned capabilities for remote MCP transports."""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import secrets
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from kiro_crew import platform_compat
from kiro_crew.executors import subprocess_executor
from kiro_crew.sandbox import create_subprocess_limited, sandboxed_spawn_argv

_TOKEN_BYTES = 32
_OPAQUE_ID_BYTES = 18
_AUTH_LINE_LIMIT = 8 * 1024
_RELAY_LINE_LIMIT = 8 * 1024 * 1024
_TOKEN_TEXT_LIMIT = 512
_STREAM_CHUNK_BYTES = 64 * 1024
_SUBPROCESS_STREAM_LIMIT = 1024 * 1024
_PROCESS_EXIT_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True)
class RemoteMcpTarget:
    """One trusted gateway-side stdio MCP process."""

    command: str
    args: tuple[str, ...]
    env: Mapping[str, str]
    cwd: str
    first_party: bool = False


@dataclass(frozen=True)
class RemoteHttpMcpTarget:
    """One trusted gateway-side HTTP MCP resource."""

    server_name: str
    url: str
    headers: Mapping[str, str]
    scopes: tuple[str, ...] = ()
    client_id: str = ""


RemoteMcpServiceTarget = RemoteMcpTarget | RemoteHttpMcpTarget


class RemoteMcpHttpAdapter(Protocol):
    async def run(
        self,
        target: RemoteHttpMcpTarget,
        session_key: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None: ...


@dataclass(frozen=True)
class RemoteMcpGrant:
    """The bearer and opaque identifiers returned only to the bridge owner."""

    grant_id: str
    token: str
    capability_id: str


@dataclass(frozen=True)
class RemoteMcpTargetLease:
    """An exclusive, temporary claim on a session-bound target."""

    session_key: str
    target: RemoteMcpServiceTarget
    _grant_id: str
    _token_digest: bytes
    _lease_id: str


@dataclass
class _CapabilityRecord:
    grant_id: str
    capability_id: str
    session_key: str
    target: RemoteMcpServiceTarget
    lease_id: str | None = None


class RemoteMcpCapabilityRegistry:
    """Mint and revoke in-memory capabilities without retaining raw bearers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[bytes, _CapabilityRecord] = {}
        self._grant_digests: dict[str, bytes] = {}

    @staticmethod
    def _digest(token: str) -> bytes:
        return hashlib.sha256(token.encode("utf-8")).digest()

    def mint(self, session_key: str, target: RemoteMcpServiceTarget) -> RemoteMcpGrant:
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        digest = self._digest(token)
        grant_id = secrets.token_urlsafe(_OPAQUE_ID_BYTES)
        capability_id = secrets.token_urlsafe(_OPAQUE_ID_BYTES)
        record = _CapabilityRecord(
            grant_id=grant_id,
            capability_id=capability_id,
            session_key=session_key,
            target=target,
        )
        with self._lock:
            self._records[digest] = record
            self._grant_digests[grant_id] = digest
        return RemoteMcpGrant(
            grant_id=grant_id,
            token=token,
            capability_id=capability_id,
        )

    def claim(self, token: str) -> RemoteMcpTargetLease | None:
        digest = self._digest(token)
        with self._lock:
            record = self._records.get(digest)
            if record is None or record.lease_id is not None:
                return None
            lease_id = secrets.token_urlsafe(_OPAQUE_ID_BYTES)
            record.lease_id = lease_id
            return RemoteMcpTargetLease(
                session_key=record.session_key,
                target=record.target,
                _grant_id=record.grant_id,
                _token_digest=digest,
                _lease_id=lease_id,
            )

    def release(self, lease: RemoteMcpTargetLease) -> None:
        with self._lock:
            record = self._records.get(lease._token_digest)
            if record is not None and record.lease_id == lease._lease_id:
                record.lease_id = None

    def revoke_grant(self, grant_id: str) -> None:
        with self._lock:
            digest = self._grant_digests.pop(grant_id, None)
            if digest is not None:
                self._records.pop(digest, None)

    def revoke_all(self) -> None:
        with self._lock:
            self._records.clear()
            self._grant_digests.clear()


class RemoteMcpProxy:
    """Loopback-only, capability-authenticated MCP byte proxy."""

    def __init__(
        self,
        registry: RemoteMcpCapabilityRegistry | None = None,
        *,
        http_adapter: RemoteMcpHttpAdapter | None = None,
    ) -> None:
        self._registry = registry or RemoteMcpCapabilityRegistry()
        self._http_adapter = http_adapter
        self._server: asyncio.Server | None = None
        self._handlers: set[asyncio.Task[None]] = set()
        self._active_grants: dict[str, asyncio.Task[None]] = {}

    @property
    def local_port(self) -> int:
        if self._server is None or not self._server.sockets:
            raise RuntimeError("remote MCP proxy is not running")
        return int(self._server.sockets[0].getsockname()[1])

    async def start(self) -> None:
        if self._server is not None:
            return
        self._server = await asyncio.start_server(
            self._accept,
            host="127.0.0.1",
            port=0,
            limit=_RELAY_LINE_LIMIT + 1,
        )

    def mint(self, session_key: str, target: RemoteMcpServiceTarget) -> RemoteMcpGrant:
        return self._registry.mint(session_key, target)

    def revoke_grant(self, grant_id: str) -> None:
        self._registry.revoke_grant(grant_id)
        task = self._active_grants.pop(grant_id, None)
        if task is not None:
            task.cancel()

    async def close(self) -> None:
        server = self._server
        self._server = None
        self._registry.revoke_all()
        if server is not None:
            server.close()
            await server.wait_closed()

        handlers = tuple(self._handlers)
        for task in handlers:
            task.cancel()
        if handlers:
            await asyncio.gather(*handlers, return_exceptions=True)

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(self._handle_connection(reader, writer))
        self._handlers.add(task)
        task.add_done_callback(self._handlers.discard)

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        lease: RemoteMcpTargetLease | None = None
        process: asyncio.subprocess.Process | None = None
        cleanup_path: str | None = None
        pumps: list[asyncio.Task[None]] = []
        try:
            token = await self._authenticate(reader)
            if token is None:
                return
            lease = self._registry.claim(token)
            if lease is None:
                return
            current_task = asyncio.current_task()
            if current_task is not None:
                self._active_grants[lease._grant_id] = current_task

            if isinstance(lease.target, RemoteHttpMcpTarget):
                from kiro_crew.mcp_gateway.remote_http import GatewayMcpHttpAdapter

                http_adapter = self._http_adapter or GatewayMcpHttpAdapter()
                await http_adapter.run(
                    lease.target,
                    lease.session_key,
                    reader,
                    writer,
                )
                return

            process, cleanup_path = await self._spawn(lease)
            assert process.stdin is not None
            assert process.stdout is not None
            assert process.stderr is not None

            upstream = asyncio.create_task(self._pump(reader, process.stdin))
            downstream = asyncio.create_task(self._pump(process.stdout, writer))
            stderr = asyncio.create_task(self._discard(process.stderr))
            pumps.extend((upstream, downstream, stderr))

            done, _ = await asyncio.wait(
                (upstream, downstream),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if downstream in done:
                upstream.cancel()
            else:
                await downstream
        except asyncio.CancelledError:
            raise
        except Exception:
            # The peer receives only EOF. Spawn and transport exceptions can
            # contain gateway paths or environment-derived text.
            return
        finally:
            for task in pumps:
                task.cancel()
            if pumps:
                await asyncio.gather(*pumps, return_exceptions=True)
            if process is not None:
                await self._terminate_process(process)
            if cleanup_path:
                Path(cleanup_path).unlink(missing_ok=True)
            if lease is not None:
                current_task = asyncio.current_task()
                if self._active_grants.get(lease._grant_id) is current_task:
                    self._active_grants.pop(lease._grant_id, None)
                self._registry.release(lease)
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    @staticmethod
    async def _authenticate(reader: asyncio.StreamReader) -> str | None:
        try:
            line = await reader.readline()
        except (ValueError, asyncio.LimitOverrunError):
            return None
        if not line or len(line) > _AUTH_LINE_LIMIT:
            return None
        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        if not isinstance(payload, dict) or set(payload) != {"version", "token"}:
            return None
        token = payload.get("token")
        if payload.get("version") != 1 or not isinstance(token, str):
            return None
        if not token or len(token) > _TOKEN_TEXT_LIMIT:
            return None
        return token

    @staticmethod
    async def _spawn(
        lease: RemoteMcpTargetLease,
    ) -> tuple[asyncio.subprocess.Process, str | None]:
        target = lease.target
        if not isinstance(target, RemoteMcpTarget):
            raise RuntimeError("HTTP targets must use the HTTP adapter")
        wrapped_argv, env, cleanup_path = await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(),
            functools.partial(
                sandboxed_spawn_argv,
                [target.command, *target.args],
                mode="standard",
                env=dict(target.env),
                strip_python_env=True,
                first_party_fixed_argv=target.first_party,
            ),
        )
        env["KIROCREW_SESSION_KEY"] = lease.session_key
        try:
            process = await create_subprocess_limited(
                *wrapped_argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=target.cwd,
                env=env,
                limit=_SUBPROCESS_STREAM_LIMIT,
                start_new_session=platform_compat.IS_POSIX,
                creationflags=(
                    platform_compat.CREATE_NEW_PROCESS_GROUP | platform_compat._SUBPROCESS_NO_WINDOW
                ),
            )
        except BaseException:
            if cleanup_path:
                Path(cleanup_path).unlink(missing_ok=True)
            raise
        return process, cleanup_path

    @staticmethod
    async def _pump(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            while data := await reader.read(_STREAM_CHUNK_BYTES):
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()

    @staticmethod
    async def _discard(reader: asyncio.StreamReader) -> None:
        while await reader.read(_STREAM_CHUNK_BYTES):
            pass

    @staticmethod
    async def _terminate_process(process: asyncio.subprocess.Process) -> None:
        if process.returncode is None:
            try:
                await platform_compat.kill_process_tree_async(
                    process.pid,
                    platform_compat.SIGTERM,
                )
            except (OSError, ProcessLookupError):
                pass
        try:
            await asyncio.wait_for(
                process.wait(),
                timeout=_PROCESS_EXIT_TIMEOUT_SECONDS,
            )
            return
        except asyncio.TimeoutError:
            pass
        try:
            await platform_compat.kill_process_tree_async(
                process.pid,
                platform_compat.SIGKILL,
            )
        except (OSError, ProcessLookupError):
            pass
        await process.wait()
