"""End-to-end tests for the credential-file remote MCP relay."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from kiro_crew import platform_compat
from kiro_crew.acp import remote_mcp_relay


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        await platform_compat.kill_process_tree_async(
            process.pid,
            platform_compat.SIGKILL,
        )
    except (OSError, ProcessLookupError):
        pass
    await process.wait()


@pytest.mark.asyncio
async def test_relay_authenticates_from_file_and_copies_literal_bytes(tmp_path: Path) -> None:
    bearer = "relay-secret-canary"
    observed_auth: asyncio.Future[dict[str, object]] = asyncio.get_running_loop().create_future()

    async def echo(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            auth = json.loads(await reader.readline())
            observed_auth.set_result(auth)
            while line := await reader.readline():
                writer.write(line)
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(echo, "127.0.0.1", 0)
    assert server.sockets
    port = int(server.sockets[0].getsockname()[1])
    cap_file = tmp_path / "capability"
    cap_file.write_text(bearer + "\n", encoding="utf-8")
    platform_compat.restrict_to_owner(cap_file)
    argv = [
        sys.executable,
        str(Path(remote_mcp_relay.__file__).resolve()),
        "--port",
        str(port),
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
    request = b'{"jsonrpc":"2.0","id":9,"method":"tools/list"}\n'
    try:
        stdout, stderr = await process.communicate(input=request)

        assert process.returncode == 0
        assert stdout == request
        assert stderr == b""
        assert await observed_auth == {"version": 1, "token": bearer}
        assert bearer not in " ".join(argv)
    finally:
        await _stop_process(process)
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_relay_forwards_before_its_stdin_reaches_eof(tmp_path: Path) -> None:
    async def echo(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            await reader.readline()
            while line := await reader.readline():
                writer.write(line)
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(echo, "127.0.0.1", 0)
    assert server.sockets
    port = int(server.sockets[0].getsockname()[1])
    cap_file = tmp_path / "capability"
    cap_file.write_text("streaming-relay-canary\n", encoding="utf-8")
    platform_compat.restrict_to_owner(cap_file)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(Path(remote_mcp_relay.__file__).resolve()),
        "--port",
        str(port),
        "--cap-file",
        str(cap_file),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(tmp_path),
        start_new_session=platform_compat.IS_POSIX,
        creationflags=(
            platform_compat.CREATE_NEW_PROCESS_GROUP | platform_compat._SUBPROCESS_NO_WINDOW
        ),
    )
    assert process.stdin is not None
    assert process.stdout is not None
    request = b'{"jsonrpc":"2.0","id":10,"method":"tools/list"}\n'
    try:
        process.stdin.write(request)
        await process.stdin.drain()

        assert await asyncio.wait_for(process.stdout.readline(), timeout=1.0) == request
        assert process.returncode is None
    finally:
        process.stdin.close()
        await _stop_process(process)
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_unsupported_relay_fails_http_mcp_closed_without_proxy(tmp_path: Path) -> None:
    argv = [
        sys.executable,
        str(Path(remote_mcp_relay.__file__).resolve()),
        "--unsupported-code",
        "remote_mcp_http_unavailable",
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
                "id": 3,
                "method": "initialize",
                "params": {},
            }
        ).encode()
        + b"\n"
    )
    try:
        stdout, stderr = await process.communicate(input=request)
        response = json.loads(stdout)

        assert process.returncode == 0
        assert stderr == b""
        assert response["id"] == 3
        assert response["error"]["data"]["code"] == "remote_mcp_http_unavailable"
    finally:
        await _stop_process(process)
