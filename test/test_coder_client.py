"""Credential-safe structured Coder CLI client."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.coder.client import CoderClient, CoderWorkspace


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
            return json.dumps([{"name": "kirocrew-arm"}]).encode()
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
            "crew-abc123",
            "--",
            "systemctl --user list-units --type=scope --state=active --no-legend "
            "--plain --no-pager 'kirocrew-crew-abc123-*.scope'",
        ],
        ["/opt/coder", "schedule", "extend", "crew-abc123", "30m"],
    ]
