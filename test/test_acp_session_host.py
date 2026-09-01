"""Contracts for ACP session processes hosted in a Coder workspace."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import kiro_crew.acp.runtime as runtime_mod
import kiro_crew.acp.session_host as host_mod
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.session_host import (
    CoderWorkspaceSessionHost,
    LocalSessionHost,
    project_remote_agent_spec,
    remote_mcp_relay_entry,
    remote_mcp_unsupported_entry,
    resolve_remote_http_mcp_targets,
    resolve_remote_mcp_targets,
)
from kiro_crew.mcp_gateway.remote_proxy import RemoteHttpMcpTarget


class TestCoderWorkspaceSessionHost:
    def test_implements_provider_neutral_remote_host_contract(self) -> None:
        from kiro_crew.acp.session_host import RemoteSessionHost

        host = CoderWorkspaceSessionHost(
            workspace="crew-dogfood",
            remote_cwd="/home/coder/workspace",
            coder_bin="/opt/coder",
        )

        assert isinstance(host, RemoteSessionHost)
        assert not isinstance(LocalSessionHost("."), RemoteSessionHost)

    @pytest.mark.asyncio
    async def test_builds_exact_ssh_transport_with_loopback_reverse_forward(self) -> None:
        host = CoderWorkspaceSessionHost(
            workspace="crew-dogfood",
            remote_cwd="/home/coder/workspace",
            coder_bin="/opt/coder",
        )

        await host.start_bridge()
        try:
            argv = host.spawn_argv(
                agent="kirocrew",
                model="auto",
            )

            assert argv[:5] == [
                "/opt/coder",
                "ssh",
                "crew-dogfood",
                "--remote-forward",
                f"{host.remote_port}:127.0.0.1:{host._proxy.local_port}",
            ]
            assert argv[5] == "--"
            assert len(argv) == 7
            assert shlex.split(argv[6]) == [
                "env",
                "-u",
                "CODER_AGENT_TOKEN",
                "-u",
                "CODER_AGENT_TOKEN_FILE",
                "kiro-cli",
                "acp",
                "--agent",
                "kirocrew",
                "--model",
                "auto",
            ]
        finally:
            await host.close()

    @pytest.mark.parametrize("agent", ["crew;touch /tmp/bad", "crew name", "-agent"])
    def test_spawn_rejects_unsafe_agent_name(self, agent: str) -> None:
        host = CoderWorkspaceSessionHost(
            workspace="crew-dogfood",
            remote_cwd="/home/coder/workspace",
            coder_bin="/opt/coder",
        )

        with pytest.raises(ValueError, match="agent"):
            host.spawn_argv(agent=agent, model="auto")

    @pytest.mark.asyncio
    async def test_spawn_quotes_model_as_remote_command_data(self) -> None:
        host = CoderWorkspaceSessionHost(
            workspace="crew-dogfood",
            remote_cwd="/home/coder/workspace",
            coder_bin="/opt/coder",
        )

        await host.start_bridge()
        try:
            argv = host.spawn_argv(agent="kirocrew", model="model; touch /tmp/bad")

            assert len(argv) == 7
            assert shlex.split(argv[6])[-2:] == ["--model", "model; touch /tmp/bad"]
        finally:
            await host.close()

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

    def test_prepare_argv_uses_positional_python_arguments(self) -> None:
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
            "env",
            "-u",
            "CODER_AGENT_TOKEN",
            "-u",
            "CODER_AGENT_TOKEN_FILE",
            "python3",
            "-c",
            host_mod._REMOTE_PREPARE_SCRIPT,
            host._runtime_id,
            "kirocrew",
            "/home/coder/workspace",
            host_mod._REMOTE_RUNTIME_MARKER,
            ",".join(str(port) for port in host._remote_port_candidates),
            host_mod._CODER_TEMPLATE_CONTRACT_PATH,
            str(host_mod._CODER_TEMPLATE_CONTRACT_VERSION),
        ]
        assert len(host._remote_port_candidates) == host_mod._REMOTE_FORWARD_PORT_ATTEMPTS
        assert len(set(host._remote_port_candidates)) == len(host._remote_port_candidates)

    def test_remote_prepare_fails_closed_without_template_contract(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        work = tmp_path / "workspace"
        home.mkdir()
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                host_mod._REMOTE_PREPARE_SCRIPT,
                "runtime-id",
                "kirocrew",
                str(work),
                host_mod._REMOTE_RUNTIME_MARKER,
                "32123",
                str(tmp_path / "missing-contract.json"),
                str(host_mod._CODER_TEMPLATE_CONTRACT_VERSION),
            ],
            input=b"{}",
            check=False,
            capture_output=True,
            env={**os.environ, "HOME": str(home)},
        )

        assert result.returncode != 0
        assert not work.exists()

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
        remote_runtime = f"/home/coder/.kiro/crew/remote-runtimes/{host._runtime_id}"
        selected_port = host._remote_port_candidates[-1]
        process.communicate = AsyncMock(
            return_value=(
                json.dumps({"runtime_dir": remote_runtime, "port": selected_port}).encode(),
                b"",
            )
        )
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
        remote_command = spawned["argv"][-1]
        assert remote_command.count(host._remote_cwd) == 1
        assert remote_command.count(host._runtime_id) == 1
        kwargs = spawned["kwargs"]
        assert isinstance(kwargs, dict)
        assert kwargs["cwd"] == str(tmp_path)
        assert kwargs["env"] == {
            "CODER_URL": "https://coder.tail.example",
            "CODER_SESSION_TOKEN": "coder-token",
            "PATH": "/usr/bin",
        }
        payload = process.communicate.await_args.kwargs["input"]
        decoded = json.loads(payload)
        assert decoded["agent"] == spec
        assert "def main" in decoded["relay"]
        assert b"must-not-cross" not in payload
        assert host.remote_runtime_dir == remote_runtime
        assert host.remote_port == selected_port
        await host.close()

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

        try:
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
        finally:
            await host.close()

        assert "secret-value" not in str(caught.value)
        assert "workspace preparation failed" in str(caught.value)

    @pytest.mark.asyncio
    async def test_prepare_times_out_and_kills_the_remote_command(
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
        process.pid = 4242
        process.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
        process.wait = AsyncMock(return_value=-9)
        killed = MagicMock()
        monkeypatch.setattr(
            host_mod,
            "create_subprocess_limited",
            AsyncMock(return_value=process),
        )
        monkeypatch.setattr(host_mod.platform_compat, "kill_process_tree", killed)
        monkeypatch.setattr(host_mod, "_REMOTE_COMMAND_TIMEOUT_SECS", 0.001)

        try:
            with pytest.raises(host_mod.SessionHostError, match="preparation timed out"):
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
        finally:
            await host.close()

        killed.assert_called_once_with(4242, host_mod.platform_compat.SIGKILL)
        process.wait.assert_awaited()

    @pytest.mark.asyncio
    async def test_session_capability_upload_exposes_only_relay_metadata(
        self,
        tmp_path,
        monkeypatch,
    ) -> None:
        host = CoderWorkspaceSessionHost(
            workspace="crew-dogfood",
            remote_cwd="/home/coder/workspace",
            coder_bin="/opt/coder",
        )
        host._remote_runtime_dir = f"/home/coder/.kiro/crew/remote-runtimes/{host._runtime_id}"
        await host.start_bridge()
        calls: list[dict[str, object]] = []

        async def capture_remote(**kwargs):
            calls.append(kwargs)
            return b""

        monkeypatch.setattr(host, "_run_remote_python", capture_remote)
        secret = "gateway-target-secret-canary"
        spec = {
            "mcpServers": {
                "kirocrew-core": {
                    "command": sys.executable,
                    "args": ["-m", "kiro_crew.mcp_core"],
                    "env": {"SERVICE_TOKEN": secret},
                    "autoApprove": ["learn_list"],
                },
                "remote-http": {
                    "url": "https://mcp.example.test/private",
                    "headers": {"Authorization": "Bearer remote-secret"},
                },
            }
        }
        try:
            entries = await host.prepare_session_capabilities(
                agent_spec=spec,
                session_key="dashboard:one",
                environ={"CODER_URL": "https://coder.example", "CODER_SESSION_TOKEN": "x"},
                local_cwd=tmp_path,
            )

            serialized = json.dumps(entries)
            assert [entry["name"] for entry in entries] == [
                "kirocrew-core",
                "remote-http",
            ]
            assert secret not in serialized
            assert sys.executable not in serialized
            assert "mcp.example.test" not in serialized
            assert "Authorization" not in serialized
            assert calls
            cap_call = calls[0]
            argv_text = " ".join(cap_call["argv"])
            payload = cap_call["payload"]
            assert isinstance(payload, bytes)
            for token in json.loads(payload).values():
                assert token not in argv_text
        finally:
            await host.close()


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
    assert host.prepare_argv(agent="kirocrew")[0] == "/opt/coder"


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


def test_remote_agent_projection_keeps_only_bridge_backed_mcp() -> None:
    relay_path = "/home/coder/.kiro/crew/remote-runtimes/run/relay.py"
    local = {
        "name": "kirocrew",
        "description": "Personal agent",
        "model": "auto",
        "prompt": "You are Kiro Crew.",
        "tools": [
            "fs_read",
            "execute_bash",
            "@kirocrew-core",
            "@github/search",
            "@remote-http",
            "@missing/tool",
        ],
        "allowedTools": ["fs_read", "@kirocrew-core", "@missing/tool"],
        "mcpServers": {
            "kirocrew-core": {
                "command": "/gateway/kirocrew",
                "env": {"AWS_SECRET_ACCESS_KEY": "not-for-the-workspace"},
                "autoApprove": ["learn_list"],
                "disabledTools": ["dangerous"],
                "timeout": 9000,
            },
            "remote-http": {
                "url": "https://mcp.example.test/secret-path",
                "headers": {"Authorization": "Bearer not-for-the-workspace"},
            },
        },
        "hooks": {"preToolUse": [{"command": "/gateway/kirocrew hook"}]},
        "toolAliases": {"github": "@github/search"},
        "unknownExtension": {"gatewayPath": "/gateway/private"},
    }

    relay_entries = {
        "kirocrew-core": remote_mcp_relay_entry(
            local["mcpServers"]["kirocrew-core"],
            relay_path=relay_path,
            port=43123,
            capability_file="/home/coder/.kiro/crew/remote-runtimes/run/cap-opaque",
        ),
        "remote-http": remote_mcp_unsupported_entry(
            local["mcpServers"]["remote-http"],
            relay_path=relay_path,
            code="remote_mcp_http_unavailable",
        ),
    }

    projected = project_remote_agent_spec(local, relay_entries=relay_entries)

    assert projected == {
        "name": "kirocrew",
        "description": "Personal agent",
        "model": "auto",
        "prompt": "You are Kiro Crew.",
        "tools": [
            "fs_read",
            "execute_bash",
            "@kirocrew-core",
            "@remote-http",
        ],
        "allowedTools": ["fs_read", "@kirocrew-core"],
        "mcpServers": relay_entries,
    }
    serialized = json.dumps(projected)
    assert "/gateway/kirocrew" not in serialized
    assert "not-for-the-workspace" not in serialized
    assert "mcp.example.test" not in serialized
    assert "Authorization" not in serialized


def test_remote_mcp_target_resolution_is_strict_and_gateway_local(
    tmp_path,
    monkeypatch,
) -> None:
    secret = "gateway-only-secret-canary"
    spec = {
        "mcpServers": {
            "kirocrew-core": {
                "command": sys.executable,
                "args": ["-m", "kiro_crew.mcp_core"],
                "env": {"SERVICE_TOKEN": secret},
            },
            "user-stdio": {"command": sys.executable, "args": ["server.py"]},
            "disabled": {"command": sys.executable, "disabled": True},
            "remote-http": {"url": "https://mcp.example.test"},
            "mixed": {"command": sys.executable, "url": "https://example.test"},
            "bad-args": {"command": sys.executable, "args": [7]},
            "bad-env": {"command": sys.executable, "env": {"X": 7}},
            "missing": {"command": "definitely-not-installed-kirocrew-test"},
        }
    }
    monkeypatch.setenv("PATH", "/usr/bin")

    targets = resolve_remote_mcp_targets(
        spec,
        local_cwd=tmp_path,
        session_key="dashboard:one",
    )

    assert set(targets) == {"kirocrew-core", "user-stdio"}
    assert targets["kirocrew-core"].command == sys.executable
    assert targets["kirocrew-core"].args == ("-m", "kiro_crew.mcp_core")
    assert targets["kirocrew-core"].env["SERVICE_TOKEN"] == secret
    assert targets["kirocrew-core"].cwd == str(tmp_path)


def test_remote_http_mcp_target_resolution_is_strict_and_gateway_only() -> None:
    secret = "gateway-only-canary"
    spec = {
        "mcpServers": {
            "linear": {
                "url": "https://mcp.linear.example/mcp",
                "headers": {"X-Workspace": secret},
                "scopes": ["read", "write"],
                "clientId": "kiro-crew",
            },
            "loopback-v4": {"url": "http://127.0.0.1:43123/mcp"},
            "loopback-v6": {"url": "http://[::1]:43124/mcp"},
            "loopback-name": {"url": "http://localhost:43125/mcp"},
            "insecure": {"url": "http://mcp.example.test/mcp"},
            "userinfo": {"url": "https://user:pass@mcp.example.test/mcp"},
            "query": {"url": "https://mcp.example.test/mcp?token=secret"},
            "fragment": {"url": "https://mcp.example.test/mcp#secret"},
            "mixed": {"url": "https://mcp.example.test/mcp", "command": "server"},
            "bad-headers": {
                "url": "https://mcp.example.test/mcp",
                "headers": {"X-Test": 7},
            },
            "bad-header-name": {
                "url": "https://mcp.example.test/mcp",
                "headers": {"X-Test\nInjected": "value"},
            },
            "bad-scopes": {
                "url": "https://mcp.example.test/mcp",
                "scopes": ["read", 7],
            },
            "bad-client": {
                "url": "https://mcp.example.test/mcp",
                "clientId": 7,
            },
            "disabled": {
                "url": "https://mcp.example.test/mcp",
                "disabled": True,
            },
            "bad/name": {"url": "https://mcp.example.test/mcp"},
        }
    }

    targets = resolve_remote_http_mcp_targets(spec, session_key="dashboard:one")

    assert targets == {
        "linear": RemoteHttpMcpTarget(
            server_name="linear",
            url="https://mcp.linear.example/mcp",
            headers={"X-Workspace": secret},
            scopes=("read", "write"),
            client_id="kiro-crew",
        ),
        "loopback-v4": RemoteHttpMcpTarget(
            server_name="loopback-v4",
            url="http://127.0.0.1:43123/mcp",
            headers={},
        ),
        "loopback-v6": RemoteHttpMcpTarget(
            server_name="loopback-v6",
            url="http://[::1]:43124/mcp",
            headers={},
        ),
        "loopback-name": RemoteHttpMcpTarget(
            server_name="loopback-name",
            url="http://localhost:43125/mcp",
            headers={},
        ),
    }


def test_remote_http_mcp_target_resolution_enforces_global_bounds() -> None:
    servers = {
        "long-url": {"url": "https://mcp.example.test/" + "x" * 4_096},
        "many-headers": {
            "url": "https://mcp.example.test/mcp",
            "headers": {f"X-{index}": "v" for index in range(33)},
        },
        "long-header-name": {
            "url": "https://mcp.example.test/mcp",
            "headers": {"X" * 257: "v"},
        },
        "long-header-value": {
            "url": "https://mcp.example.test/mcp",
            "headers": {"X-Test": "v" * (8 * 1_024 + 1)},
        },
        "large-headers": {
            "url": "https://mcp.example.test/mcp",
            "headers": {f"X-{index}": "v" * 2_100 for index in range(32)},
        },
    }

    assert (
        resolve_remote_http_mcp_targets(
            {"mcpServers": servers},
            session_key="dashboard:one",
        )
        == {}
    )


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

    try:
        with pytest.raises(StopSpawn):
            await runtime.spawn()

        argv = captured["argv"]
        assert isinstance(argv, tuple)
        assert argv[:4] == ("/opt/coder", "ssh", "crew-dogfood", "--remote-forward")
        assert argv[5:] == (
            "--",
            "env -u CODER_AGENT_TOKEN -u CODER_AGENT_TOKEN_FILE "
            "kiro-cli acp --agent kirocrew --model auto",
        )
        remote_port, loopback, local_port = str(argv[4]).split(":")
        assert int(remote_port) == host.remote_port
        assert loopback == "127.0.0.1"
        assert 1 <= int(local_port) <= 65535
    finally:
        await host.close()

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
    close = AsyncMock(wraps=host.close)
    monkeypatch.setattr(host, "close", close)

    with pytest.raises(RuntimeError, match="stop after remote preparation"):
        await runtime.spawn()

    projected = prepare.await_args.kwargs["projected_spec"]
    assert projected["prompt"] == "Dogfood Crew from the remote workspace."
    assert not projected["prompt"].startswith("file://")
    close.assert_awaited_once_with(
        environ=os.environ,
        local_cwd=tmp_path / "gateway-workspace",
    )


@pytest.mark.asyncio
async def test_runtime_remote_session_uses_remote_cwd_and_gateway_relays(
    tmp_path,
    monkeypatch,
) -> None:
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
    runtime._remote_agent_spec = {"mcpServers": {}}
    relays = [
        {
            "name": "kirocrew-core",
            "command": "python3",
            "args": ["/remote/relay.py"],
            "env": [],
        }
    ]
    prepare_capabilities = AsyncMock(return_value=relays)
    monkeypatch.setattr(
        host,
        "prepare_session_capabilities",
        prepare_capabilities,
    )
    revoke = MagicMock(wraps=host.revoke_session_grants)
    monkeypatch.setattr(host, "revoke_session_grants", revoke)

    class StopSession(Exception):
        pass

    send = AsyncMock(side_effect=StopSession)
    runtime._send_and_await = send

    with pytest.raises(StopSession):
        await runtime.create_session(
            cwd=tmp_path / "caller-local-path",
            session_key="dashboard:one",
        )

    params = send.await_args.args[1]
    assert params["cwd"] == "/home/coder/workspace"
    assert params["mcpServers"] == relays
    prepare_capabilities.assert_awaited_once()
    revoke.assert_called_once_with("dashboard:one")


@pytest.mark.asyncio
async def test_runtime_remote_resume_uses_workspace_transcript_and_rotated_relays(
    tmp_path,
    monkeypatch,
) -> None:
    host = CoderWorkspaceSessionHost(
        workspace="crew-dogfood",
        remote_cwd="/home/coder/workspace",
        coder_bin="/opt/coder",
    )
    host._remote_runtime_dir = f"/home/coder/.kiro/crew/remote-runtimes/{host._runtime_id}"
    runtime = AcpRuntime(
        work_dir=tmp_path / "gateway-workspace",
        agent="kirocrew",
        session_host=host,
        expect_mcp_reports=False,
    )
    runtime._initialized = True
    runtime._can_load_session = True
    runtime._remote_agent_spec = {"mcpServers": {}}
    relays = [
        {
            "name": "kirocrew-core",
            "command": "python3",
            "args": ["/remote/relay.py"],
            "env": [],
        }
    ]
    prepare_capabilities = AsyncMock(return_value=relays)
    monkeypatch.setattr(host, "prepare_session_capabilities", prepare_capabilities)
    revoke = MagicMock(wraps=host.revoke_session_grants)
    monkeypatch.setattr(host, "revoke_session_grants", revoke)
    send = AsyncMock(return_value={"modes": []})
    runtime._send_and_await = send

    handle = await runtime.load_session(
        "/gateway/.kiro/sessions/sid-remote.json",
        "sid-remote",
        cwd=tmp_path / "caller-local-path",
        session_key="dashboard:one",
    )

    assert handle.session_id == "sid-remote"
    params = send.await_args.args[1]
    assert params["cwd"] == "/home/coder/workspace"
    assert params["mcpServers"] == relays
    assert params["_meta"] == {
        "_kiro.dev/session_file": "/home/coder/.kiro/sessions/sid-remote.json"
    }
    assert "/gateway" not in json.dumps(params)
    assert runtime._remote_session_keys == {"sid-remote": "dashboard:one"}

    await runtime.terminate_session("sid-remote")
    assert runtime._remote_session_keys == {}
    revoke.assert_called_once_with("dashboard:one")


@pytest.mark.asyncio
async def test_managed_host_resolves_parent_workspace_before_remote_prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.acp.session_host import ManagedCoderWorkspaceSessionHost
    from kiro_crew.coder.client import CoderWorkspace

    class _Manager:
        def __init__(self) -> None:
            self.sessions: list[str] = []
            self.allow_start: list[bool] = []
            self.registry = SimpleNamespace(get_by_session=lambda _key: None)

        async def ensure_ready(
            self,
            session_key: str,
            *,
            template: str | None = None,
            preset: str | None = None,
            on_progress=None,
            allow_start: bool = True,
        ) -> CoderWorkspace:
            self.sessions.append(session_key)
            self.allow_start.append(allow_start)
            if on_progress is not None:
                on_progress("provisioning", "crew-opaque123")
                on_progress("connecting", "crew-opaque123")
            return CoderWorkspace(
                uuid="workspace-uuid",
                name="crew-opaque123",
                owner="kyleseaman",
                template="kirocrew-arm",
                status="running",
                last_used_at="2026-08-25T12:00:00Z",
            )

    manager = _Manager()
    host = ManagedCoderWorkspaceSessionHost(
        session_key="dashboard:one",
        manager=manager,  # type: ignore[arg-type]
        remote_cwd="/home/coder/workspace",
        coder_bin="/opt/coder",
        coder_url="https://coder.example",
        session_token="token",
    )

    async def run_remote(**kwargs: object) -> bytes:
        return json.dumps(
            {
                "runtime_dir": ("/home/coder/.kiro/crew/remote-runtimes/" + host._runtime_id),
                "port": host._remote_port_candidates[0],
            }
        ).encode()

    monkeypatch.setattr(host, "_run_remote_python", run_remote)
    monkeypatch.setattr(
        "kiro_crew.acp.session_host.Path.read_text",
        lambda self, encoding="utf-8": "relay",
    )

    await host.prepare(
        agent="kirocrew",
        projected_spec={"name": "Kiro Crew"},
        environ={"PATH": "/usr/bin"},
        local_cwd=tmp_path,
    )

    assert manager.sessions == ["dashboard:one"]
    assert manager.allow_start == [True]
    assert host.execution_location["workspace"] == "crew-opaque123"
    assert host.prepare_argv(agent="kirocrew")[2] == "crew-opaque123"
    argv = host.spawn_argv(agent="kirocrew", model="auto")
    assert argv[:6] == [
        "/opt/coder",
        "ssh",
        "crew-opaque123",
        "--remote-forward",
        f"{host.remote_port}:127.0.0.1:{host._proxy.local_port}",
        "--",
    ]
    assert argv[6] == (
        'user_id="$(id -u)" && export XDG_RUNTIME_DIR="/run/user/$user_id" && '
        'export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus" && exec '
        "env -u CODER_AGENT_TOKEN -u CODER_AGENT_TOKEN_FILE "
        "systemd-run --user --scope --quiet --collect "
        f"--unit=kirocrew-crew-opaque123-{host._runtime_id} "
        "kiro-cli acp --agent kirocrew --model auto"
    )
    clone = host.clone()
    await clone.start_bridge()
    assert any(
        "systemd-run" in argument for argument in clone.spawn_argv(agent="kirocrew", model="auto")
    )
    await host.close()
    await clone.close()
