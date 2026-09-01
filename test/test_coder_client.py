"""Credential-safe structured Coder CLI client."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.coder.client import (
    CoderClient,
    CoderClientError,
    CoderWorkspace,
    CoderWorkspaceMemory,
)


@pytest.mark.asyncio
async def test_create_keeps_token_out_of_argv_and_reconciles_structured_record(
    tmp_path: Path,
) -> None:
    calls: list[tuple[list[str], dict[str, str]]] = []

    async def run(argv: list[str], env: dict[str, str], cwd: Path) -> bytes:
        calls.append((argv, env))
        if argv[1] == "create":
            return b""
        return json.dumps(
            [
                {
                    "id": "workspace-uuid",
                    "name": "crew-abc123",
                    "owner_id": "owner-immutable-uuid",
                    "owner_name": "kyleseaman",
                    "template_name": "kirocrew-arm",
                    "latest_build": {"status": "running"},
                    "last_used_at": "2026-08-25T12:00:00Z",
                }
            ]
        ).encode()

    client = CoderClient(
        coder_bin="/opt/coder",
        url="https://coder.example",
        token="secret-token-canary",
        cwd=tmp_path,
        runner=run,
    )

    workspace = await client.create_workspace(
        name="crew-abc123",
        template="kirocrew-arm",
        preset="arm-small",
        stop_after_minutes=30,
    )

    assert workspace == CoderWorkspace(
        uuid="workspace-uuid",
        name="crew-abc123",
        owner="owner-immutable-uuid",
        template="kirocrew-arm",
        status="running",
        last_used_at="2026-08-25T12:00:00Z",
    )
    flattened = json.dumps([argv for argv, _env in calls])
    assert "secret-token-canary" not in flattened
    assert all(env["CODER_SESSION_TOKEN"] == "secret-token-canary" for _argv, env in calls)
    assert calls[0][0] == [
        "/opt/coder",
        "create",
        "crew-abc123",
        "--template",
        "kirocrew-arm",
        "--preset",
        "arm-small",
        "--stop-after",
        "30m",
        "--yes",
        "--use-parameter-defaults",
    ]


@pytest.mark.asyncio
async def test_get_workspace_returns_none_for_absent_record(tmp_path: Path) -> None:
    async def run(argv: list[str], env: dict[str, str], cwd: Path) -> bytes:
        return b""

    client = CoderClient("/opt/coder", "https://coder.example", "token", tmp_path, run)

    assert await client.get_workspace("crew-missing") is None


@pytest.mark.asyncio
async def test_stop_workspace_uses_noninteractive_coder_command(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    async def run(argv: list[str], env: dict[str, str], cwd: Path) -> bytes:
        calls.append(argv)
        if argv[1] == "stop":
            return b""
        return json.dumps(
            [
                {
                    "id": "workspace-uuid",
                    "name": "crew-abc123",
                    "owner_id": "owner-immutable-uuid",
                    "template_name": "kirocrew-arm",
                    "latest_build": {"status": "stopped"},
                    "last_used_at": "2026-08-25T12:00:00Z",
                }
            ]
        ).encode()

    client = CoderClient("/opt/coder", "https://coder.example", "token", tmp_path, run)

    workspace = await client.stop_workspace("crew-abc123")

    assert workspace.status == "stopped"
    assert calls[0] == ["/opt/coder", "stop", "crew-abc123", "--yes"]


@pytest.mark.asyncio
async def test_probe_verifies_identity_and_template_without_creating_compute(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    async def run(argv: list[str], env: dict[str, str], cwd: Path) -> bytes:
        calls.append(argv)
        if argv[1] == "whoami":
            return json.dumps(
                [
                    {
                        "user_id": "0d5bd32b-20ba-4937-9f89-a6726be46ba1",
                        "username": "kyleseaman",
                    }
                ]
            ).encode()
        if argv[1:3] == ["templates", "list"]:
            return json.dumps([{"Template": {"name": "kirocrew-arm"}}]).encode()
        raise AssertionError(argv)

    client = CoderClient("/opt/coder", "https://coder.example", "token", tmp_path, run)

    assert await client.probe(template="kirocrew-arm", preset="") == {
        "owner": "kyleseaman",
        "template": "kirocrew-arm",
    }
    assert calls[0] == ["/opt/coder", "whoami", "--output", "json"]
    assert all("create" not in call for call in calls)


@pytest.mark.asyncio
async def test_scope_probe_and_deadline_extension_use_bounded_coder_commands(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    async def run(argv: list[str], env: dict[str, str], cwd: Path) -> bytes:
        calls.append(argv)
        if argv[1] == "ssh":
            return b"kirocrew-crew-abc123-runtime.scope loaded active running\n"
        return b""

    client = CoderClient("/opt/coder", "https://coder.example", "token", tmp_path, run)

    assert await client.has_active_workload_scope("crew-abc123") is True
    await client.extend_workspace_deadline("crew-abc123", 30)

    assert calls == [
        [
            "/opt/coder",
            "ssh",
            "--disable-autostart",
            "crew-abc123",
            "--",
            'user_id="$(id -u)" && export XDG_RUNTIME_DIR="/run/user/$user_id" && '
            'export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus" && exec '
            "systemctl --user list-units --type=scope --state=active --no-legend "
            "--plain --no-pager 'kirocrew-crew-abc123-*.scope'",
        ],
        ["/opt/coder", "schedule", "extend", "crew-abc123", "30m"],
    ]


@pytest.mark.asyncio
async def test_workspace_memory_uses_no_autostart_and_scrubs_agent_tokens(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    async def run(argv: list[str], env: dict[str, str], cwd: Path) -> bytes:
        calls.append(argv)
        return b'{"available_bytes":4294967296,"total_bytes":8589934592}'

    client = CoderClient("/opt/coder", "https://coder.example", "token", tmp_path, run)

    memory = await client.workspace_memory("crew-abc123")

    assert memory == CoderWorkspaceMemory(
        available_gb=4.0,
        total_gb=8.0,
        used_percent=50.0,
        pressure="normal",
    )
    assert calls[0][:5] == [
        "/opt/coder",
        "ssh",
        "--disable-autostart",
        "crew-abc123",
        "--",
    ]
    assert len(calls[0]) == 6
    remote_command = calls[0][5]
    assert remote_command.startswith(
        "env -u CODER_AGENT_TOKEN -u CODER_AGENT_TOKEN_FILE python3 -c "
    )
    assert "'import json" in remote_command


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "output",
    [
        b"not-json",
        b'{"available_bytes":-1,"total_bytes":1024}',
        b'{"available_bytes":2048,"total_bytes":1024}',
        b'{"available_bytes":0,"total_bytes":0}',
        b'{"available_bytes":"1","total_bytes":1024}',
        b'{"available_bytes":0,"total_bytes":1152921504606846977}',
        b"x" * 4097,
    ],
)
async def test_workspace_memory_rejects_untrusted_probe_output(
    tmp_path: Path,
    output: bytes,
) -> None:
    async def run(argv: list[str], env: dict[str, str], cwd: Path) -> bytes:
        return output

    client = CoderClient("/opt/coder", "https://coder.example", "token", tmp_path, run)

    with pytest.raises(CoderClientError):
        await client.workspace_memory("crew-abc123")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("available_bytes", "total_bytes", "pressure"),
    [
        (2_147_483_648, 10_737_418_240, "elevated"),
        (1_073_741_824, 10_737_418_240, "critical"),
    ],
)
async def test_workspace_memory_classifies_pressure(
    tmp_path: Path,
    available_bytes: int,
    total_bytes: int,
    pressure: str,
) -> None:
    async def run(argv: list[str], env: dict[str, str], cwd: Path) -> bytes:
        return json.dumps({"available_bytes": available_bytes, "total_bytes": total_bytes}).encode()

    client = CoderClient("/opt/coder", "https://coder.example", "token", tmp_path, run)

    assert (await client.workspace_memory("crew-abc123")).pressure == pressure
