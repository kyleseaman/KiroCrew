"""Contracts for ACP session processes hosted in a Coder workspace."""

from __future__ import annotations

import json
import shlex
from unittest.mock import AsyncMock, MagicMock

import pytest

import kiro_crew.acp.runtime as runtime_mod
import kiro_crew.acp.session_host as host_mod
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.session_host import (
    CoderWorkspaceSessionHost,
    LocalSessionHost,
    project_remote_agent_spec,
)


class TestCoderWorkspaceSessionHost:
    def test_builds_exact_ssh_transport_without_forwarding_gateway_env(self) -> None:
        host = CoderWorkspaceSessionHost(
            workspace="crew-dogfood",
            remote_cwd="/home/coder/workspace",
            coder_bin="/opt/coder",
        )

        argv = host.spawn_argv(
            agent="kirocrew",
            model="auto",
        )

        assert argv == [
            "/opt/coder",
            "ssh",
            "crew-dogfood",
            "--",
            "kiro-cli",
            "acp",
            "--agent",
            "kirocrew",
            "--model",
            "auto",
        ]
        assert "env" not in argv

    def test_transport_env_is_a_small_allowlist(self) -> None:
        host = CoderWorkspaceSessionHost(
            workspace="crew-dogfood",
            remote_cwd="/home/coder/workspace",
            coder_bin="/opt/coder",
        )
        gateway_env = {
            "CODER_URL": "https://coder.tail.example",
            "CODER_SESSION_TOKEN": "coder-token",
            "PATH": "/usr/local/bin:/usr/bin",
            "SSL_CERT_FILE": "/etc/ssl/cert.pem",
            "HTTPS_PROXY": "http://proxy.internal:3128",
            "AWS_ACCESS_KEY_ID": "AKIAFAKE",
            "AWS_SECRET_ACCESS_KEY": "do-not-forward",
            "SSH_AUTH_SOCK": "/tmp/agent.sock",
            "KIRO_API_KEY": "workspace-secret-only",
            "KIROCREW_HOME": "/gateway/state",
            "PYTHONPATH": "/gateway/python",
        }

        env = host.transport_env(gateway_env)

        assert env == {
            "CODER_URL": "https://coder.tail.example",
            "CODER_SESSION_TOKEN": "coder-token",
            "PATH": "/usr/local/bin:/usr/bin",
            "SSL_CERT_FILE": "/etc/ssl/cert.pem",
            "HTTPS_PROXY": "http://proxy.internal:3128",
        }

    @pytest.mark.parametrize(
        ("workspace", "remote_cwd"),
        [
            ("crew;shutdown", "/home/coder/workspace"),
            ("crew dogfood", "/home/coder/workspace"),
            ("crew-dogfood", "relative/workspace"),
            ("crew-dogfood", "/home/coder/../root"),
        ],
    )
    def test_rejects_ambiguous_workspace_or_remote_path(
        self,
        workspace: str,
        remote_cwd: str,
    ) -> None:
        with pytest.raises(ValueError):
            CoderWorkspaceSessionHost(
                workspace=workspace,
                remote_cwd=remote_cwd,
                coder_bin="/opt/coder",
            )

    def test_protocol_cwd_is_remote_path(self) -> None:
        host = CoderWorkspaceSessionHost(
            workspace="crew-dogfood",
            remote_cwd="/home/coder/workspace",
            coder_bin="/opt/coder",
        )

        assert host.protocol_cwd == "/home/coder/workspace"

    def test_prepare_argv_uses_positional_shell_arguments(self) -> None:
        host = CoderWorkspaceSessionHost(
            workspace="crew-dogfood",
            remote_cwd="/home/coder/workspace",
            coder_bin="/opt/coder",
        )

        argv = host.prepare_argv(agent="kirocrew")

        assert argv[:4] == [
            "/opt/coder",
            "ssh",
            "crew-dogfood",
            "--",
        ]
        assert len(argv) == 5
        assert shlex.split(argv[4]) == [
            "sh",
            "-c",
            host_mod._REMOTE_PREPARE_SCRIPT,
            "kirocrew-prepare",
            "kirocrew",
            "/home/coder/workspace",
        ]

    @pytest.mark.asyncio
    async def test_prepare_sends_only_projected_json_to_bounded_subprocess(
        self,
        tmp_path,
        monkeypatch,
    ) -> None:
        host = CoderWorkspaceSessionHost(
            workspace="crew-dogfood",
            remote_cwd="/home/coder/workspace",
            coder_bin="/opt/coder",
        )
        process = MagicMock()
        process.returncode = 0
        process.communicate = AsyncMock(return_value=(b"", b""))
        spawned: dict[str, object] = {}

        async def fake_spawn(*argv, **kwargs):
            spawned["argv"] = argv
            spawned["kwargs"] = kwargs
            return process

        monkeypatch.setattr(host_mod, "create_subprocess_limited", fake_spawn)
        spec = {
            "name": "kirocrew",
            "prompt": "Dogfood Crew.",
            "tools": ["fs_read"],
            "allowedTools": [],
            "mcpServers": {},
        }
        gateway_env = {
            "CODER_URL": "https://coder.tail.example",
            "CODER_SESSION_TOKEN": "coder-token",
            "PATH": "/usr/bin",
            "KIRO_API_KEY": "must-not-cross",
            "AWS_SECRET_ACCESS_KEY": "must-not-cross-either",
        }

        await host.prepare(
            agent="kirocrew",
            projected_spec=spec,
            environ=gateway_env,
            local_cwd=tmp_path,
        )

        assert spawned["argv"] == tuple(host.prepare_argv(agent="kirocrew"))
        kwargs = spawned["kwargs"]
        assert isinstance(kwargs, dict)
        assert kwargs["cwd"] == str(tmp_path)
        assert kwargs["env"] == {
            "CODER_URL": "https://coder.tail.example",
            "CODER_SESSION_TOKEN": "coder-token",
            "PATH": "/usr/bin",
        }
        payload = process.communicate.await_args.kwargs["input"]
        assert json.loads(payload) == spec
        assert b"must-not-cross" not in payload

    @pytest.mark.asyncio
    async def test_prepare_surfaces_sanitized_remote_failure(
        self,
        tmp_path,
        monkeypatch,
    ) -> None:
        host = CoderWorkspaceSessionHost(
            workspace="crew-dogfood",
            remote_cwd="/home/coder/workspace",
            coder_bin="/opt/coder",
        )
        process = MagicMock()
        process.returncode = 1
        process.communicate = AsyncMock(
            return_value=(b"", b"workspace failed with KIRO_API_KEY=secret-value\n")
        )
        monkeypatch.setattr(
            host_mod,
            "create_subprocess_limited",
            AsyncMock(return_value=process),
        )

        with pytest.raises(RuntimeError) as caught:
            await host.prepare(
                agent="kirocrew",
                projected_spec={
                    "name": "kirocrew",
                    "prompt": "Dogfood Crew.",
                    "tools": [],
                    "allowedTools": [],
                    "mcpServers": {},
                },
                environ={
                    "CODER_URL": "https://coder.tail.example",
                    "CODER_SESSION_TOKEN": "coder-token",
                },
                local_cwd=tmp_path,
            )

        assert "secret-value" not in str(caught.value)
        assert "workspace preparation failed" in str(caught.value)


def test_local_host_keeps_local_protocol_path(tmp_path) -> None:
    host = LocalSessionHost(tmp_path)

    assert host.is_remote is False
    assert host.protocol_cwd == str(tmp_path)


def test_session_host_from_env_defaults_to_local(tmp_path) -> None:
    host = host_mod.session_host_from_env(tmp_path, {})

    assert isinstance(host, LocalSessionHost)


def test_session_host_from_env_builds_authenticated_coder_host(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        host_mod.shutil,
        "which",
        lambda command, path=None: "/opt/coder" if command == "coder" else None,
    )

    host = host_mod.session_host_from_env(
        tmp_path,
        {
            "KIROCREW_CODER_WORKSPACE": "crew-dogfood",
            "KIROCREW_CODER_REMOTE_CWD": "/home/coder/project",
            "CODER_URL": "https://coder.tail.example",
            "CODER_SESSION_TOKEN": "coder-token",
            "PATH": "/usr/local/bin:/usr/bin",
        },
    )

    assert isinstance(host, CoderWorkspaceSessionHost)
    assert host.protocol_cwd == "/home/coder/project"
    assert host.spawn_argv(agent="kirocrew", model="auto")[0] == "/opt/coder"


@pytest.mark.parametrize("missing", ["CODER_URL", "CODER_SESSION_TOKEN"])
def test_session_host_from_env_requires_coder_transport_auth(
    tmp_path,
    monkeypatch,
    missing,
) -> None:
    monkeypatch.setattr(host_mod.shutil, "which", lambda command, path=None: "/opt/coder")
    environ = {
        "KIROCREW_CODER_WORKSPACE": "crew-dogfood",
        "CODER_URL": "https://coder.tail.example",
        "CODER_SESSION_TOKEN": "coder-token",
    }
    environ.pop(missing)

    with pytest.raises(host_mod.SessionHostError, match=missing):
        host_mod.session_host_from_env(tmp_path, environ)


def test_session_host_from_env_requires_coder_cli(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(host_mod.shutil, "which", lambda command, path=None: None)

    with pytest.raises(host_mod.SessionHostError, match="Coder CLI"):
        host_mod.session_host_from_env(
            tmp_path,
            {
                "KIROCREW_CODER_WORKSPACE": "crew-dogfood",
                "CODER_URL": "https://coder.tail.example",
                "CODER_SESSION_TOKEN": "coder-token",
            },
        )


def test_remote_agent_projection_keeps_builtin_tools_and_removes_mcp() -> None:
    local = {
        "name": "kirocrew",
        "description": "Personal agent",
        "model": "auto",
        "prompt": "You are Kiro Crew.",
        "tools": ["fs_read", "execute_bash", "@kirocrew-core", "@github/search"],
        "allowedTools": ["fs_read", "@kirocrew-core"],
        "mcpServers": {
            "kirocrew-core": {
                "command": "/gateway/kirocrew",
                "env": {"AWS_SECRET_ACCESS_KEY": "not-for-the-workspace"},
            }
        },
        "hooks": {"preToolUse": [{"command": "/gateway/kirocrew hook"}]},
        "toolAliases": {"github": "@github/search"},
        "unknownExtension": {"gatewayPath": "/gateway/private"},
    }

    projected = project_remote_agent_spec(local)

    assert projected == {
        "name": "kirocrew",
        "description": "Personal agent",
        "model": "auto",
        "prompt": "You are Kiro Crew.",
        "tools": ["fs_read", "execute_bash"],
        "allowedTools": ["fs_read"],
        "mcpServers": {},
    }


def test_remote_agent_projection_rejects_wrong_field_types() -> None:
    with pytest.raises(ValueError, match="name"):
        project_remote_agent_spec({"name": ["kirocrew"], "prompt": "ok"})

    with pytest.raises(ValueError, match="prompt"):
        project_remote_agent_spec({"name": "kirocrew", "prompt": {"file": "local"}})


def test_runtime_remote_spawn_initializes_local_scratch_state(tmp_path) -> None:
    host = CoderWorkspaceSessionHost(
        workspace="crew-dogfood",
        remote_cwd="/home/coder/workspace",
        coder_bin="/opt/coder",
    )

    runtime = AcpRuntime(work_dir=tmp_path, session_host=host)

    assert runtime._scratch_dir is None


@pytest.mark.asyncio
async def test_runtime_remote_spawn_skips_local_confinement_and_gateway_env(
    tmp_path,
    monkeypatch,
) -> None:
    agents_dir = tmp_path / "kiro-home" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "kirocrew.json").write_text(
        json.dumps(
            {
                "name": "kirocrew",
                "prompt": "Dogfood Crew.",
                "tools": ["fs_read", "@kirocrew-core"],
                "allowedTools": [],
                "mcpServers": {"kirocrew-core": {"command": "/gateway/kirocrew"}},
            }
        ),
        encoding="utf-8",
    )
    host = CoderWorkspaceSessionHost(
        workspace="crew-dogfood",
        remote_cwd="/home/coder/workspace",
        coder_bin="/opt/coder",
    )
    prepare = AsyncMock()
    monkeypatch.setattr(host, "prepare", prepare)
    monkeypatch.setenv("CODER_URL", "https://coder.tail.example")
    monkeypatch.setenv("CODER_SESSION_TOKEN", "coder-token")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("KIRO_API_KEY", "gateway-key-must-not-cross")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-key-must-not-cross")
    monkeypatch.setattr(runtime_mod, "kiro_agents_dir", lambda: agents_dir)
    monkeypatch.setattr(runtime_mod, "ensure_agent_materialized", lambda agent: True)
    monkeypatch.setattr(
        runtime_mod,
        "wrap_argv",
        lambda *_args, **_kwargs: pytest.fail("remote spawn used the local sandbox"),
    )
    monkeypatch.setattr(
        runtime_mod,
        "cgroup_scope_argv",
        lambda *_args, **_kwargs: pytest.fail("remote spawn used the gateway cgroup"),
    )

    captured: dict[str, object] = {}

    class StopSpawn(Exception):
        pass

    async def stop_spawn(*argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        raise StopSpawn

    monkeypatch.setattr(runtime_mod, "create_subprocess_limited", stop_spawn)
    runtime = AcpRuntime(
        work_dir=tmp_path / "gateway-workspace",
        agent="kirocrew",
        model="auto",
        session_host=host,
    )

    with pytest.raises(StopSpawn):
        await runtime.spawn()

    assert captured["argv"] == tuple(host.spawn_argv(agent="kirocrew", model="auto"))
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["cwd"] == str(tmp_path / "gateway-workspace")
    assert kwargs["env"] == {
        "CODER_URL": "https://coder.tail.example",
        "CODER_SESSION_TOKEN": "coder-token",
        "PATH": "/usr/bin",
    }
    prepare.assert_awaited_once()
    prepared = prepare.await_args.kwargs
    assert prepared["agent"] == "kirocrew"
    assert prepared["projected_spec"]["mcpServers"] == {}
    assert prepared["projected_spec"]["tools"] == ["fs_read"]


@pytest.mark.asyncio
async def test_runtime_remote_spawn_inlines_file_prompt(tmp_path, monkeypatch) -> None:
    agents_dir = tmp_path / "kiro-home" / "agents"
    agents_dir.mkdir(parents=True)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Dogfood Crew from the remote workspace.", encoding="utf-8")
    (agents_dir / "kirocrew.json").write_text(
        json.dumps(
            {
                "name": "kirocrew",
                "prompt": f"file://{prompt}",
                "tools": [],
                "allowedTools": [],
            }
        ),
        encoding="utf-8",
    )
    host = CoderWorkspaceSessionHost(
        workspace="crew-dogfood",
        remote_cwd="/home/coder/workspace",
        coder_bin="/opt/coder",
    )
    prepare = AsyncMock()
    monkeypatch.setattr(host, "prepare", prepare)
    monkeypatch.setattr(runtime_mod, "kiro_agents_dir", lambda: agents_dir)
    monkeypatch.setattr(runtime_mod, "ensure_agent_materialized", lambda agent: True)
    monkeypatch.setattr(
        runtime_mod,
        "create_subprocess_limited",
        AsyncMock(side_effect=RuntimeError("stop after remote preparation")),
    )
    runtime = AcpRuntime(
        work_dir=tmp_path / "gateway-workspace",
        agent="kirocrew",
        session_host=host,
    )

    with pytest.raises(RuntimeError, match="stop after remote preparation"):
        await runtime.spawn()

    projected = prepare.await_args.kwargs["projected_spec"]
    assert projected["prompt"] == "Dogfood Crew from the remote workspace."
    assert not projected["prompt"].startswith("file://")


@pytest.mark.asyncio
async def test_runtime_remote_session_uses_remote_cwd_and_empty_mcp(tmp_path) -> None:
    host = CoderWorkspaceSessionHost(
        workspace="crew-dogfood",
        remote_cwd="/home/coder/workspace",
        coder_bin="/opt/coder",
    )
    runtime = AcpRuntime(
        work_dir=tmp_path / "gateway-workspace",
        agent="kirocrew",
        session_host=host,
    )
    runtime._initialized = True

    class StopSession(Exception):
        pass

    send = AsyncMock(side_effect=StopSession)
    runtime._send_and_await = send

    with pytest.raises(StopSession):
        await runtime.create_session(cwd=tmp_path / "caller-local-path")

    params = send.await_args.args[1]
    assert params["cwd"] == "/home/coder/workspace"
    assert params["mcpServers"] == []
