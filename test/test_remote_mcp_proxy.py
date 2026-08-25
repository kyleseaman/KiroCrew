"""Session-scoped capabilities for remote MCP access."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from kiro_crew import platform_compat
from kiro_crew.acp import remote_mcp_relay
from kiro_crew.mcp_gateway import remote_proxy as remote_proxy_module
from kiro_crew.mcp_gateway.remote_proxy import (
    RemoteHttpMcpTarget,
    RemoteMcpCapabilityRegistry,
    RemoteMcpProxy,
    RemoteMcpTarget,
)


def _target() -> RemoteMcpTarget:
    return RemoteMcpTarget(
        command="/usr/bin/server",
        args=("--stdio",),
        env={"MODE": "test"},
        cwd="/work",
        first_party=False,
    )


def test_capability_is_bound_to_one_session_and_target() -> None:
    registry = RemoteMcpCapabilityRegistry()
    target = _target()
    grant = registry.mint("dashboard:one", target)

    lease = registry.claim(grant.token)

    assert lease is not None
    assert lease.session_key == "dashboard:one"
    assert lease.target is target
    assert registry.claim("wrong-token") is None


def test_capability_registry_preserves_gateway_http_target() -> None:
    registry = RemoteMcpCapabilityRegistry()
    target = RemoteHttpMcpTarget(
        server_name="linear",
        url="https://mcp.linear.example/mcp",
        headers={"X-Workspace": "gateway-only-canary"},
        scopes=("read", "write"),
        client_id="kiro-crew",
    )

    grant = registry.mint("dashboard:one", target)
    lease = registry.claim(grant.token)

    assert lease is not None
    assert lease.session_key == "dashboard:one"
    assert lease.target is target


def test_registry_does_not_retain_raw_bearer() -> None:
    registry = RemoteMcpCapabilityRegistry()
    grant = registry.mint("dashboard:one", _target())

    assert grant.token not in repr(registry)
    assert grant.token not in repr(vars(registry))


def test_capability_refuses_simultaneous_claim_and_allows_reconnect() -> None:
    registry = RemoteMcpCapabilityRegistry()
    grant = registry.mint("dashboard:one", _target())

    first = registry.claim(grant.token)

    assert first is not None
    assert registry.claim(grant.token) is None
    registry.release(first)
    assert registry.claim(grant.token) is not None


def test_revoked_grant_cannot_be_claimed_or_released_back_to_life() -> None:
    registry = RemoteMcpCapabilityRegistry()
    grant = registry.mint("dashboard:one", _target())
    lease = registry.claim(grant.token)

    assert lease is not None
    registry.revoke_grant(grant.grant_id)
    registry.release(lease)

    assert registry.claim(grant.token) is None


def test_revoke_all_invalidates_every_capability() -> None:
    registry = RemoteMcpCapabilityRegistry()
    first = registry.mint("dashboard:one", _target())
    second = registry.mint("cron:two", _target())

    registry.revoke_all()

    assert registry.claim(first.token) is None
    assert registry.claim(second.token) is None


def _write_echo_backend(tmp_path: Path) -> Path:
    backend = tmp_path / "echo_backend.py"
    backend.write_text(
        "import sys\n"
        "for line in sys.stdin.buffer:\n"
        "    sys.stdout.buffer.write(line)\n"
        "    sys.stdout.buffer.flush()\n",
        encoding="utf-8",
    )
    return backend


def _backend_target(tmp_path: Path) -> RemoteMcpTarget:
    return RemoteMcpTarget(
        command=sys.executable,
        args=(str(_write_echo_backend(tmp_path)),),
        env={},
        cwd=str(tmp_path),
        first_party=False,
    )


async def _connect(
    proxy: RemoteMcpProxy, token: str
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy.local_port)
    writer.write(json.dumps({"version": 1, "token": token}).encode() + b"\n")
    await writer.drain()
    return reader, writer


@pytest.mark.asyncio
async def test_proxy_forwards_literal_mcp_bytes(tmp_path: Path) -> None:
    proxy = RemoteMcpProxy()
    await proxy.start()
    grant = proxy.mint("dashboard:one", _backend_target(tmp_path))
    reader = None
    writer = None
    request = b'{"jsonrpc":"2.0","id":7,"method":"tools/list"}\n'
    try:
        reader, writer = await _connect(proxy, grant.token)
        writer.write(request)
        await writer.drain()

        assert await reader.readline() == request
    finally:
        if writer is not None:
            writer.close()
            await writer.wait_closed()
        await proxy.close()


@pytest.mark.asyncio
async def test_proxy_resolves_sandbox_wrapper_off_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wrapped_off_loop = False

    def cold_probe_guard(
        argv: list[str],
        mode: str,
        *,
        env: dict[str, str],
        strip_python_env: bool,
        first_party_fixed_argv: bool,
    ) -> tuple[list[str], dict[str, str], None]:
        nonlocal wrapped_off_loop
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            wrapped_off_loop = True
        else:
            raise RuntimeError("cold sandbox probing cannot run on the event loop")
        return list(argv), dict(env), None

    monkeypatch.setattr(remote_proxy_module, "sandboxed_spawn_argv", cold_probe_guard)
    proxy = RemoteMcpProxy()
    await proxy.start()
    grant = proxy.mint("dashboard:one", _backend_target(tmp_path))
    reader = None
    writer = None
    request = b'{"jsonrpc":"2.0","id":8,"method":"tools/list"}\n'
    try:
        reader, writer = await _connect(proxy, grant.token)
        writer.write(request)
        await writer.drain()

        assert await reader.readline() == request
        assert wrapped_off_loop is True
    finally:
        if writer is not None:
            writer.close()
            await writer.wait_closed()
        await proxy.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "auth_line",
    [
        b"not-json\n",
        json.dumps({"version": 1, "token": "wrong"}).encode() + b"\n",
        b"x" * 70_000 + b"\n",
    ],
)
async def test_proxy_rejects_invalid_auth_without_spawning(
    monkeypatch: pytest.MonkeyPatch,
    auth_line: bytes,
) -> None:
    spawned = False

    async def unexpected_spawn(*args: object, **kwargs: object) -> None:
        nonlocal spawned
        spawned = True
        raise AssertionError("invalid authentication reached the spawn boundary")

    monkeypatch.setattr(
        "kiro_crew.mcp_gateway.remote_proxy.create_subprocess_limited",
        unexpected_spawn,
    )
    proxy = RemoteMcpProxy()
    await proxy.start()
    reader = None
    writer = None
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy.local_port)
        writer.write(auth_line)
        await writer.drain()

        assert await reader.read() == b""
        assert spawned is False
    finally:
        if writer is not None:
            writer.close()
            await writer.wait_closed()
        await proxy.close()


@pytest.mark.asyncio
async def test_proxy_refuses_concurrent_capability_reuse(tmp_path: Path) -> None:
    proxy = RemoteMcpProxy()
    await proxy.start()
    grant = proxy.mint("dashboard:one", _backend_target(tmp_path))
    first_reader = second_reader = None
    first_writer = second_writer = None
    request = b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n'
    try:
        first_reader, first_writer = await _connect(proxy, grant.token)
        first_writer.write(request)
        await first_writer.drain()
        assert await first_reader.readline() == request

        second_reader, second_writer = await _connect(proxy, grant.token)
        assert await second_reader.read() == b""
    finally:
        for writer in (second_writer, first_writer):
            if writer is not None:
                writer.close()
                await writer.wait_closed()
        await proxy.close()


@pytest.mark.asyncio
async def test_proxy_closes_connection_when_backend_spawn_fails(tmp_path: Path) -> None:
    proxy = RemoteMcpProxy()
    await proxy.start()
    target = RemoteMcpTarget(
        command=str(tmp_path / "missing-server"),
        args=(),
        env={},
        cwd=str(tmp_path),
        first_party=False,
    )
    grant = proxy.mint("dashboard:one", target)
    reader = None
    writer = None
    try:
        reader, writer = await _connect(proxy, grant.token)
        assert await reader.read() == b""
    finally:
        if writer is not None:
            writer.close()
            await writer.wait_closed()
        await proxy.close()


@pytest.mark.asyncio
async def test_proxy_shutdown_closes_live_connection(tmp_path: Path) -> None:
    proxy = RemoteMcpProxy()
    await proxy.start()
    grant = proxy.mint("dashboard:one", _backend_target(tmp_path))
    reader, writer = await _connect(proxy, grant.token)
    request = b'{"jsonrpc":"2.0","id":2,"method":"ping"}\n'
    try:
        writer.write(request)
        await writer.drain()
        assert await reader.readline() == request

        await proxy.close()

        assert await reader.read() == b""
    finally:
        writer.close()
        await writer.wait_closed()
        await proxy.close()


@pytest.mark.asyncio
async def test_revoking_grant_closes_its_live_connection(tmp_path: Path) -> None:
    proxy = RemoteMcpProxy()
    await proxy.start()
    grant = proxy.mint("dashboard:one", _backend_target(tmp_path))
    reader, writer = await _connect(proxy, grant.token)
    request = b'{"jsonrpc":"2.0","id":3,"method":"ping"}\n'
    try:
        writer.write(request)
        await writer.drain()
        assert await reader.readline() == request

        proxy.revoke_grant(grant.grant_id)

        assert await reader.read() == b""
        reconnect_reader, reconnect_writer = await _connect(proxy, grant.token)
        try:
            assert await reconnect_reader.read() == b""
        finally:
            reconnect_writer.close()
            await reconnect_writer.wait_closed()
    finally:
        writer.close()
        await writer.wait_closed()
        await proxy.close()


@pytest.mark.asyncio
async def test_real_relay_preserves_session_identity_and_gateway_secret_boundary(
    tmp_path: Path,
) -> None:
    state_file = tmp_path / "gateway-observation.json"
    backend = tmp_path / "managed_core_fake.py"
    backend.write_text(
        "import json, os, pathlib, sys\n"
        "state = pathlib.Path(sys.argv[1])\n"
        "for line in sys.stdin.buffer:\n"
        "    request = json.loads(line)\n"
        "    state.write_text(json.dumps({\n"
        "        'session_key': os.environ.get('KIROCREW_SESSION_KEY'),\n"
        "        'has_gateway_secret': bool(os.environ.get('GATEWAY_SECRET')),\n"
        "        'method': request.get('method'),\n"
        "    }))\n"
        "    response = {'jsonrpc': '2.0', 'id': request.get('id'), "
        "'result': {'content': [{'type': 'text', 'text': 'lesson stored'}]}}\n"
        "    sys.stdout.write(json.dumps(response) + '\\n')\n"
        "    sys.stdout.flush()\n",
        encoding="utf-8",
    )
    gateway_secret = "gateway-only-secret-canary"
    target = RemoteMcpTarget(
        command=sys.executable,
        args=(str(backend), str(state_file)),
        env={"GATEWAY_SECRET": gateway_secret},
        cwd=str(tmp_path),
    )
    proxy = RemoteMcpProxy()
    await proxy.start()
    grant = proxy.mint("subagent:child", target)
    cap_file = tmp_path / "capability"
    cap_file.write_text(grant.token + "\n", encoding="utf-8")
    platform_compat.restrict_to_owner(cap_file)
    argv = [
        sys.executable,
        str(Path(remote_mcp_relay.__file__).resolve()),
        "--port",
        str(proxy.local_port),
        "--cap-file",
        str(cap_file),
    ]
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(tmp_path),
        start_new_session=platform_compat.IS_POSIX,
        creationflags=(
            platform_compat.CREATE_NEW_PROCESS_GROUP | platform_compat._SUBPROCESS_NO_WINDOW
        ),
    )
    request = (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 11,
                "method": "tools/call",
                "params": {"name": "learn_add", "arguments": {"text": "canary"}},
            }
        ).encode()
        + b"\n"
    )
    try:
        stdout, stderr = await process.communicate(input=request)
        response = json.loads(stdout)
        observed = json.loads(state_file.read_text(encoding="utf-8"))

        assert process.returncode == 0
        assert stderr == b""
        assert response["result"]["content"][0]["text"] == "lesson stored"
        assert observed == {
            "session_key": "subagent:child",
            "has_gateway_secret": True,
            "method": "tools/call",
        }
        assert gateway_secret not in stdout.decode()
        assert gateway_secret not in request.decode()
        assert gateway_secret not in " ".join(argv)
        assert cap_file.read_text(encoding="utf-8").strip() == grant.token
    finally:
        if process.returncode is None:
            await platform_compat.kill_process_tree_async(
                process.pid,
                platform_compat.SIGKILL,
            )
            await process.wait()
        await proxy.close()
