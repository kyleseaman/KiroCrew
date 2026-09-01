"""Execution-host boundary for ACP session processes."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import shlex
import shutil
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from kiro_crew import platform_compat
from kiro_crew.acp import remote_mcp_relay
from kiro_crew.coder.client import CoderClient
from kiro_crew.coder.manager import CoderWorkspaceManager, ManagedWorkspacePolicy
from kiro_crew.coder.registry import WorkspaceBindingRegistry
from kiro_crew.coder.user_bus import user_bus_command
from kiro_crew.constants import (
    CODER_DEFAULT_REMOTE_CWD,
    CODER_DEFAULT_RUNTIME_WARM_MINUTES,
    EXECUTION_LOCATION_PHASES,
    EXECUTION_PHASE_ALLOCATING,
    EXECUTION_PHASE_CONNECTING,
    EXECUTION_PHASE_PROVISIONING,
)
from kiro_crew.env import mcp_search_path, sanitize_spec_env, spec_path_key
from kiro_crew.mcp_gateway.remote_proxy import (
    RemoteHttpMcpTarget,
    RemoteMcpProxy,
    RemoteMcpServiceTarget,
    RemoteMcpTarget,
)
from kiro_crew.mcp_utils import kiro_entry_client_id, kiro_entry_scopes
from kiro_crew.sandbox import RLIMIT_PROFILE_SESSION_HOST, create_subprocess_limited

logger = logging.getLogger(__name__)

_WORKSPACE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_TRANSPORT_ENV_KEYS = (
    "CODER_URL",
    "CODER_SESSION_TOKEN",
    "PATH",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "ALL_PROXY",
)
_CODER_REMOTE_AGENT_ENV_KEYS = (
    "CODER_AGENT_TOKEN",
    "CODER_AGENT_TOKEN_FILE",
)
CODER_SESSION_TOKEN_SECRET = "coder.session_token.v1"
_REMOTE_AGENT_TEXT_KEYS = ("name", "description", "model", "prompt")
_CODER_URL_MAX_BYTES = 4 * 1024
_CODER_CONNECTION_TIMEOUT_SECS = 30.0
_REMOTE_COMMAND_TIMEOUT_SECS = 120.0
_REMOTE_RUNTIME_MARKER = "__KIROCREW_REMOTE_RUNTIME__"
_REMOTE_FORWARD_PORT_MIN = 30000
_REMOTE_FORWARD_PORT_SPAN = 20000
_REMOTE_FORWARD_PORT_ATTEMPTS = 8
_REMOTE_MCP_POLICY_LIST_KEYS = ("autoApprove", "disabledTools")
_REMOTE_MCP_POLICY_INT_KEYS = ("timeout",)
_REMOTE_MCP_SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_HTTP_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_REMOTE_HTTP_URL_MAX_BYTES = 4 * 1024
_REMOTE_HTTP_HEADER_MAX_COUNT = 32
_REMOTE_HTTP_HEADER_NAME_MAX_BYTES = 256
_REMOTE_HTTP_HEADER_VALUE_MAX_BYTES = 8 * 1024
_REMOTE_HTTP_HEADERS_MAX_BYTES = 64 * 1024
_REMOTE_HTTP_CLIENT_ID_MAX_BYTES = 4 * 1024
_REMOTE_HTTP_LOOPBACK_HOSTS = frozenset(("localhost", "127.0.0.1", "::1"))
_CODER_TEMPLATE_CONTRACT_PATH = "/etc/kirocrew-coder-contract.json"
_CODER_TEMPLATE_CONTRACT_VERSION = 1
_REMOTE_PREPARE_SCRIPT = r"""
import json, os, pathlib, pwd, shutil, socket, sys
runtime_id, agent, work_dir, marker, raw_ports, contract_path, expected_version = sys.argv[1:]
contract = json.loads(pathlib.Path(contract_path).read_text(encoding="utf-8"))
required = {"kiro-cli", "systemd-user-scopes"}
if (
    contract.get("version") != int(expected_version)
    or contract.get("user") != pwd.getpwuid(os.geteuid()).pw_name
    or contract.get("remote_cwd") != work_dir
    or not required.issubset(set(contract.get("capabilities", [])))
    or shutil.which("kiro-cli") is None
):
    raise SystemExit(4)
ports = [int(value) for value in raw_ports.split(",")]
selected_port = None
for candidate in ports:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", candidate))
    except OSError:
        pass
    else:
        selected_port = candidate
        break
    finally:
        probe.close()
if selected_port is None:
    raise SystemExit(3)
home = pathlib.Path.home()
runtime_dir = home / ".kiro" / "crew" / "remote-runtimes" / runtime_id
agents_dir = runtime_dir.parents[2] / "agents"
runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
agents_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
os.chmod(runtime_dir, 0o700)
payload = json.load(sys.stdin)
def expand(value):
    if isinstance(value, str):
        return value.replace(marker, str(runtime_dir))
    if isinstance(value, list):
        return [expand(item) for item in value]
    if isinstance(value, dict):
        return {key: expand(item) for key, item in value.items()}
    return value
relay_path = runtime_dir / "remote_mcp_relay.py"
agent_path = agents_dir / (agent + ".json")
relay_path.write_text(payload["relay"], encoding="utf-8")
agent_path.write_text(json.dumps(expand(payload["agent"]), separators=(",", ":")) + "\n", encoding="utf-8")
os.chmod(relay_path, 0o600)
os.chmod(agent_path, 0o600)
work_path = pathlib.Path(work_dir)
work_path.mkdir(parents=True, exist_ok=True)
if work_path.stat().st_uid != os.geteuid() or not os.access(work_path, os.R_OK | os.W_OK | os.X_OK):
    raise SystemExit(4)
print(json.dumps({"runtime_dir": str(runtime_dir), "port": selected_port}, separators=(",", ":")))
""".strip()
_REMOTE_CAPABILITY_SCRIPT = r"""
import json, os, pathlib, re, sys
runtime_dir = pathlib.Path(sys.argv[1])
payload = json.load(sys.stdin)
for capability_id, token in payload.items():
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", capability_id):
        raise SystemExit(2)
    path = runtime_dir / ("cap-" + capability_id)
    path.write_text(token + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
""".strip()
_REMOTE_CLEANUP_SCRIPT = r"""
import pathlib, shutil, sys
path = pathlib.Path(sys.argv[1])
expected = pathlib.Path.home() / ".kiro" / "crew" / "remote-runtimes"
if path.parent == expected:
    shutil.rmtree(path, ignore_errors=False)
""".strip()
_REMOTE_FILE_EXISTS_SCRIPT = r"""
import pathlib, sys
print("1" if pathlib.Path(sys.argv[1]).is_file() else "0")
""".strip()


class SessionHostError(RuntimeError):
    """A session execution host could not prepare or launch its process."""


class LocalSessionHost:
    """The gateway host that owns the existing local kiro-cli subprocess."""

    def __init__(self, work_dir: str | Path) -> None:
        self._work_dir = Path(work_dir)

    @property
    def is_remote(self) -> bool:
        return False

    @property
    def protocol_cwd(self) -> str:
        return str(self._work_dir)


class RemoteSessionHost(ABC):
    """Base contract for a remotely hosted ACP runtime.

    Concrete providers own their transport and lifecycle implementation. ACP
    common paths use this positive capability instead of naming one provider.
    """

    @property
    def is_remote(self) -> bool:
        return True

    @property
    @abstractmethod
    def protocol_cwd(self) -> str:
        raise NotImplementedError

    @property
    @abstractmethod
    def execution_location(self) -> dict[str, str]:
        raise NotImplementedError

    @abstractmethod
    async def start_bridge(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def clone(self) -> "RemoteSessionHost":
        raise NotImplementedError

    @abstractmethod
    def spawn_argv(self, *, agent: str, model: str) -> list[str]:
        raise NotImplementedError

    @abstractmethod
    def transport_env(self, environ: Mapping[str, str]) -> dict[str, str]:
        raise NotImplementedError

    @abstractmethod
    async def prepare(
        self,
        *,
        agent: str,
        projected_spec: Mapping[str, object],
        environ: Mapping[str, str],
        local_cwd: str | Path,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    async def prepare_session_capabilities(
        self,
        *,
        agent_spec: Mapping[str, object],
        session_key: str,
        environ: Mapping[str, str],
        local_cwd: str | Path,
    ) -> list[dict[str, object]]:
        raise NotImplementedError

    @abstractmethod
    def revoke_session_grants(self, session_key: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def remote_session_file(self, session_id: str) -> str:
        raise NotImplementedError

    @abstractmethod
    async def session_file_exists(
        self,
        session_id: str,
        *,
        environ: Mapping[str, str],
        local_cwd: str | Path,
    ) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def close(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        local_cwd: str | Path | None = None,
    ) -> None:
        raise NotImplementedError


class CoderWorkspaceSessionHost(RemoteSessionHost):
    """A kiro-cli process reached through a Coder workspace SSH channel."""

    def __init__(
        self,
        *,
        workspace: str,
        remote_cwd: str,
        coder_bin: str,
        coder_url: str = "",
        session_token: str = "",
        workload_scope_prefix: str = "",
    ) -> None:
        if not _WORKSPACE_RE.fullmatch(workspace):
            raise ValueError("workspace must be one safe Coder name")
        path = PurePosixPath(remote_cwd)
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("remote_cwd must be an absolute normalized POSIX path")
        self._workspace = workspace
        if workload_scope_prefix and not _WORKSPACE_RE.fullmatch(workload_scope_prefix):
            raise ValueError("workload_scope_prefix must be one safe Coder name")
        self._workload_scope_prefix = workload_scope_prefix
        self._remote_cwd = str(path)
        self._coder_bin = coder_bin
        self._transport_credentials = {
            key: value
            for key, value in (
                ("CODER_URL", coder_url),
                ("CODER_SESSION_TOKEN", session_token),
            )
            if value
        }
        self._runtime_id = secrets.token_urlsafe(18)
        candidates: list[int] = []
        while len(candidates) < _REMOTE_FORWARD_PORT_ATTEMPTS:
            candidate = _REMOTE_FORWARD_PORT_MIN + secrets.randbelow(_REMOTE_FORWARD_PORT_SPAN)
            if candidate not in candidates:
                candidates.append(candidate)
        self._remote_port_candidates = tuple(candidates)
        self._remote_port = candidates[0]
        self._proxy = RemoteMcpProxy()
        self._remote_runtime_dir: str | None = None
        self._grant_ids_by_session: dict[str, list[str]] = {}

    @property
    def is_remote(self) -> bool:
        return True

    @property
    def protocol_cwd(self) -> str:
        return self._remote_cwd

    @property
    def execution_location(self) -> dict[str, str]:
        """Return the non-secret location descriptor safe for dashboard clients."""
        return {
            "kind": "coder",
            "workspace": self._workspace,
            "remote_cwd": self._remote_cwd,
        }

    @property
    def remote_port(self) -> int:
        return self._remote_port

    @property
    def remote_runtime_dir(self) -> str | None:
        return self._remote_runtime_dir

    async def start_bridge(self) -> None:
        await self._proxy.start()

    def clone(self) -> "CoderWorkspaceSessionHost":
        """Return an unstarted host for a dedicated descendant runtime."""
        return CoderWorkspaceSessionHost(
            workspace=self._workspace,
            remote_cwd=self._remote_cwd,
            coder_bin=self._coder_bin,
            coder_url=self._transport_credentials.get("CODER_URL", ""),
            session_token=self._transport_credentials.get("CODER_SESSION_TOKEN", ""),
            workload_scope_prefix=self._workload_scope_prefix,
        )

    def spawn_argv(self, *, agent: str, model: str) -> list[str]:
        if not _WORKSPACE_RE.fullmatch(agent):
            raise ValueError("agent must be one safe Kiro agent name")
        forward = f"{self._remote_port}:127.0.0.1:{self._proxy.local_port}"
        remote_argv = [
            "kiro-cli",
            "acp",
            "--agent",
            agent,
        ]
        if model:
            remote_argv.extend(("--model", model))
        if self._workload_scope_prefix:
            unit = f"kirocrew-{self._workload_scope_prefix}-{self._runtime_id}"
            remote_argv = [
                "systemd-run",
                "--user",
                "--scope",
                "--quiet",
                "--collect",
                f"--unit={unit}",
                *remote_argv,
            ]
        remote_argv = [
            "env",
            *(item for key in _CODER_REMOTE_AGENT_ENV_KEYS for item in ("-u", key)),
            *remote_argv,
        ]
        remote_command = (
            user_bus_command(remote_argv)
            if self._workload_scope_prefix
            else shlex.join(remote_argv)
        )
        return [
            self._coder_bin,
            "ssh",
            self._workspace,
            "--remote-forward",
            forward,
            "--",
            remote_command,
        ]

    def transport_env(self, environ: Mapping[str, str]) -> dict[str, str]:
        transport = {key: environ[key] for key in _TRANSPORT_ENV_KEYS if environ.get(key)}
        transport.update(self._transport_credentials)
        return transport

    def prepare_argv(self, *, agent: str) -> list[str]:
        if not _WORKSPACE_RE.fullmatch(agent):
            raise ValueError("agent must be one safe Kiro agent name")
        remote_command = shlex.join(
            [
                "env",
                *(item for key in _CODER_REMOTE_AGENT_ENV_KEYS for item in ("-u", key)),
                "python3",
                "-c",
                _REMOTE_PREPARE_SCRIPT,
                self._runtime_id,
                agent,
                self._remote_cwd,
                _REMOTE_RUNTIME_MARKER,
                ",".join(str(port) for port in self._remote_port_candidates),
                _CODER_TEMPLATE_CONTRACT_PATH,
                str(_CODER_TEMPLATE_CONTRACT_VERSION),
            ]
        )
        return [self._coder_bin, "ssh", self._workspace, "--", remote_command]

    def _remote_python_argv(self, script: str, *args: str) -> list[str]:
        command = shlex.join(
            [
                "env",
                *(item for key in _CODER_REMOTE_AGENT_ENV_KEYS for item in ("-u", key)),
                "python3",
                "-c",
                script,
                *args,
            ]
        )
        return [self._coder_bin, "ssh", self._workspace, "--", command]

    async def _run_remote_python(
        self,
        *,
        argv: list[str],
        payload: bytes | None,
        environ: Mapping[str, str],
        local_cwd: str | Path,
        operation: str,
    ) -> bytes:
        process = await create_subprocess_limited(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(local_cwd),
            env=self.transport_env(environ),
            start_new_session=platform_compat.IS_POSIX,
            creationflags=(
                platform_compat.CREATE_NEW_PROCESS_GROUP | platform_compat._SUBPROCESS_NO_WINDOW
            ),
            profile=RLIMIT_PROFILE_SESSION_HOST,
        )
        try:
            stdout, _stderr = await asyncio.wait_for(
                process.communicate(input=payload),
                timeout=_REMOTE_COMMAND_TIMEOUT_SECS,
            )
        except asyncio.TimeoutError as exc:
            platform_compat.kill_process_tree(process.pid, platform_compat.SIGKILL)
            await process.wait()
            raise SessionHostError(f"Coder workspace {operation} timed out") from exc
        except asyncio.CancelledError:
            platform_compat.kill_process_tree(process.pid, platform_compat.SIGKILL)
            await process.wait()
            raise
        if process.returncode:
            raise SessionHostError(
                f"Coder workspace {operation} failed (exit {process.returncode})"
            )
        return stdout

    async def prepare(
        self,
        *,
        agent: str,
        projected_spec: Mapping[str, object],
        environ: Mapping[str, str],
        local_cwd: str | Path,
    ) -> None:
        await self.start_bridge()
        relay_source = await asyncio.to_thread(
            Path(remote_mcp_relay.__file__).read_text,
            encoding="utf-8",
        )
        payload = (
            json.dumps(
                {"agent": projected_spec, "relay": relay_source},
                separators=(",", ":"),
            )
            + "\n"
        ).encode()
        stdout = await self._run_remote_python(
            argv=self.prepare_argv(agent=agent),
            payload=payload,
            environ=environ,
            local_cwd=local_cwd,
            operation="preparation",
        )
        try:
            result = json.loads(stdout)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SessionHostError("Coder workspace preparation returned invalid data") from exc
        if not isinstance(result, dict):
            raise SessionHostError("Coder workspace preparation returned invalid data")
        runtime_dir = result.get("runtime_dir")
        remote_port = result.get("port")
        if not isinstance(runtime_dir, str) or remote_port not in self._remote_port_candidates:
            raise SessionHostError("Coder workspace preparation returned invalid data")
        path = PurePosixPath(runtime_dir)
        if not path.is_absolute() or path.name != self._runtime_id:
            raise SessionHostError("Coder workspace preparation returned an invalid path")
        self._remote_runtime_dir = str(path)
        self._remote_port = remote_port

    async def prepare_session_capabilities(
        self,
        *,
        agent_spec: Mapping[str, object],
        session_key: str,
        environ: Mapping[str, str],
        local_cwd: str | Path,
    ) -> list[dict[str, object]]:
        runtime_dir = self._remote_runtime_dir
        if runtime_dir is None:
            raise SessionHostError("Coder workspace bridge is not prepared")
        self.revoke_session_grants(session_key)
        targets = await asyncio.to_thread(
            resolve_remote_mcp_targets,
            agent_spec,
            local_cwd=local_cwd,
            session_key=session_key,
        )
        http_targets = await asyncio.to_thread(
            resolve_remote_http_mcp_targets,
            agent_spec,
            session_key=session_key,
        )
        service_targets: dict[str, RemoteMcpServiceTarget] = {
            **targets,
            **http_targets,
        }
        raw_servers = agent_spec.get("mcpServers")
        server_specs = raw_servers if isinstance(raw_servers, Mapping) else {}
        relay_path = f"{runtime_dir}/remote_mcp_relay.py"
        token_payload: dict[str, str] = {}
        entries: list[dict[str, object]] = []
        grant_ids: list[str] = []
        try:
            for name, target in sorted(service_targets.items()):
                grant = self._proxy.mint(session_key, target)
                grant_ids.append(grant.grant_id)
                token_payload[grant.capability_id] = grant.token
                original = server_specs.get(name)
                policy = original if isinstance(original, Mapping) else {}
                entry = remote_mcp_relay_entry(
                    policy,
                    relay_path=relay_path,
                    port=self._remote_port,
                    capability_file=f"{runtime_dir}/cap-{grant.capability_id}",
                )
                entries.append(_acp_remote_server_entry(name, entry))

            for name in sorted(
                unsupported_remote_mcp_names(
                    agent_spec,
                    resolved_http_names=frozenset(http_targets),
                )
            ):
                original = server_specs.get(name)
                policy = original if isinstance(original, Mapping) else {}
                entry = remote_mcp_unsupported_entry(
                    policy,
                    relay_path=relay_path,
                    code="remote_mcp_http_unavailable",
                )
                entries.append(_acp_remote_server_entry(name, entry))

            if token_payload:
                payload = (json.dumps(token_payload, separators=(",", ":")) + "\n").encode()
                await self._run_remote_python(
                    argv=self._remote_python_argv(
                        _REMOTE_CAPABILITY_SCRIPT,
                        runtime_dir,
                    ),
                    payload=payload,
                    environ=environ,
                    local_cwd=local_cwd,
                    operation="capability preparation",
                )
        except BaseException:
            for grant_id in grant_ids:
                self._proxy.revoke_grant(grant_id)
            raise
        self._grant_ids_by_session[session_key] = grant_ids
        return sorted(entries, key=lambda item: str(item["name"]))

    def revoke_session_grants(self, session_key: str) -> None:
        for grant_id in self._grant_ids_by_session.pop(session_key, []):
            self._proxy.revoke_grant(grant_id)

    def remote_session_file(self, session_id: str) -> str:
        if not _SESSION_ID_RE.fullmatch(session_id):
            raise SessionHostError("remote session id is invalid")
        if self._remote_runtime_dir is None:
            raise SessionHostError("Coder workspace bridge is not prepared")
        kiro_home = PurePosixPath(self._remote_runtime_dir).parents[2]
        return str(kiro_home / "sessions" / f"{session_id}.json")

    async def session_file_exists(
        self,
        session_id: str,
        *,
        environ: Mapping[str, str],
        local_cwd: str | Path,
    ) -> bool:
        session_file = self.remote_session_file(session_id)
        stdout = await self._run_remote_python(
            argv=self._remote_python_argv(
                _REMOTE_FILE_EXISTS_SCRIPT,
                session_file,
            ),
            payload=None,
            environ=environ,
            local_cwd=local_cwd,
            operation="session probe",
        )
        return stdout.strip() == b"1"

    async def close(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        local_cwd: str | Path | None = None,
    ) -> None:
        await self._proxy.close()
        self._grant_ids_by_session.clear()
        runtime_dir = self._remote_runtime_dir
        self._remote_runtime_dir = None
        if runtime_dir is None or environ is None or local_cwd is None:
            return
        try:
            await self._run_remote_python(
                argv=self._remote_python_argv(_REMOTE_CLEANUP_SCRIPT, runtime_dir),
                payload=None,
                environ=environ,
                local_cwd=local_cwd,
                operation="cleanup",
            )
        except Exception:
            pass


class ManagedCoderWorkspaceSessionHost(CoderWorkspaceSessionHost):
    """Lazy parent host whose verified workspace is supplied by the gateway manager."""

    def __init__(
        self,
        *,
        session_key: str,
        manager: CoderWorkspaceManager,
        remote_cwd: str,
        coder_bin: str,
        coder_url: str,
        session_token: str,
        runtime_warm_minutes: int = CODER_DEFAULT_RUNTIME_WARM_MINUTES,
        template: str | None = None,
        preset: str | None = None,
        allow_start: bool = True,
    ) -> None:
        super().__init__(
            workspace="crew-pending",
            remote_cwd=remote_cwd,
            coder_bin=coder_bin,
            coder_url=coder_url,
            session_token=session_token,
        )
        self._parent_session_key = session_key
        self._manager = manager
        self._template = template
        self._preset = preset
        self._allow_start = allow_start
        self._managed_ready = False
        binding = manager.registry.get_by_session(session_key)
        if binding is None or binding.state == "deleted":
            self._startup_phase = EXECUTION_PHASE_ALLOCATING
        else:
            self._workspace = binding.workspace_name
            self._startup_phase = (
                EXECUTION_PHASE_PROVISIONING
                if binding.state == "stopped" or not binding.workspace_uuid
                else EXECUTION_PHASE_CONNECTING
            )
        self._execution_location_callback: Callable[[], None] | None = None
        self.runtime_warm_seconds = max(0, runtime_warm_minutes) * 60

    def set_execution_location_callback(self, callback: Callable[[], None] | None) -> None:
        """Receive a nudge when dashboard-safe startup metadata changes."""
        self._execution_location_callback = callback

    def _set_startup_progress(self, phase: str, workspace: str) -> None:
        if phase not in EXECUTION_LOCATION_PHASES:
            return
        if workspace and _WORKSPACE_RE.fullmatch(workspace):
            self._workspace = workspace
        self._startup_phase = phase
        if self._execution_location_callback is not None:
            self._execution_location_callback()

    @property
    def execution_location(self) -> dict[str, str]:
        if not self._managed_ready:
            return {
                "kind": "coder",
                "workspace": "" if self._workspace == "crew-pending" else self._workspace,
                "remote_cwd": self._remote_cwd,
                "state": "starting",
                "phase": self._startup_phase,
            }
        location = super().execution_location
        location["state"] = "running"
        return location

    async def prepare(
        self,
        *,
        agent: str,
        projected_spec: Mapping[str, object],
        environ: Mapping[str, str],
        local_cwd: str | Path,
    ) -> None:
        prepare_started = time.monotonic()
        workspace = await self._manager.ensure_ready(
            self._parent_session_key,
            template=self._template,
            preset=self._preset,
            on_progress=self._set_startup_progress,
            allow_start=self._allow_start,
        )
        control_plane_ms = (time.monotonic() - prepare_started) * 1000.0
        if not _WORKSPACE_RE.fullmatch(workspace.name):
            raise SessionHostError("Managed Coder workspace returned an invalid name")
        self._workspace = workspace.name
        self._workload_scope_prefix = workspace.name
        self._managed_ready = True
        if self._execution_location_callback is not None:
            self._execution_location_callback()
        try:
            transport_started = time.monotonic()
            await super().prepare(
                agent=agent,
                projected_spec=projected_spec,
                environ=environ,
                local_cwd=local_cwd,
            )
            logger.info(
                "Coder session host ready workspace=%s prefetch=%s "
                "control_plane_ms=%.0f transport_ms=%.0f total_ms=%.0f",
                self._workspace,
                not self._allow_start,
                control_plane_ms,
                (time.monotonic() - transport_started) * 1000.0,
                (time.monotonic() - prepare_started) * 1000.0,
            )
        except BaseException:
            self._managed_ready = False
            if self._execution_location_callback is not None:
                self._execution_location_callback()
            raise

    def clone(self) -> CoderWorkspaceSessionHost:
        if not self._managed_ready:
            raise SessionHostError("Managed Coder parent workspace is not ready")
        return super().clone()


def _remote_mcp_policy_fields(spec: Mapping[str, object]) -> dict[str, object]:
    fields: dict[str, object] = {}
    for key in _REMOTE_MCP_POLICY_LIST_KEYS:
        value = spec.get(key)
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            fields[key] = list(value)
    for key in _REMOTE_MCP_POLICY_INT_KEYS:
        value = spec.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            fields[key] = value
    return fields


def remote_mcp_relay_entry(
    spec: Mapping[str, object],
    *,
    relay_path: str,
    port: int,
    capability_file: str,
) -> dict[str, object]:
    """Build a remote entry containing no gateway target or bearer details."""
    entry = _remote_mcp_policy_fields(spec)
    entry.update(
        {
            "command": "python3",
            "args": [
                relay_path,
                "--port",
                str(port),
                "--cap-file",
                capability_file,
            ],
            "env": {},
        }
    )
    return entry


def remote_mcp_unsupported_entry(
    spec: Mapping[str, object],
    *,
    relay_path: str,
    code: str,
) -> dict[str, object]:
    """Build a local fail-closed entry for an unsupported remote transport."""
    entry = _remote_mcp_policy_fields(spec)
    entry.update(
        {
            "command": "python3",
            "args": [relay_path, "--unsupported-code", code],
            "env": {},
        }
    )
    return entry


def _acp_remote_server_entry(
    name: str,
    entry: Mapping[str, object],
) -> dict[str, object]:
    raw_args = entry.get("args")
    args = list(raw_args) if isinstance(raw_args, list) else []
    shaped = {key: value for key, value in entry.items() if key not in {"command", "args", "env"}}
    shaped.update(
        {
            "name": name,
            "command": entry["command"],
            "args": args,
            "env": [],
        }
    )
    return shaped


def _remote_mcp_reference_available(ref: str, names: frozenset[str]) -> bool:
    if not ref.startswith("@"):
        return True
    server_name = ref[1:].split("/", 1)[0]
    return bool(server_name) and server_name in names


def project_remote_agent_spec(
    spec: Mapping[str, object],
    *,
    relay_entries: Mapping[str, Mapping[str, object]] | None = None,
) -> dict[str, object]:
    """Return the credential-free subset of an agent spec safe for a workspace."""
    projected: dict[str, object] = {}
    safe_entries = relay_entries or {}
    available_names = frozenset(safe_entries)
    for key in _REMOTE_AGENT_TEXT_KEYS:
        value = spec.get(key)
        if value is None and key in ("description", "model"):
            continue
        if not isinstance(value, str):
            raise ValueError(f"remote agent {key} must be a string")
        projected[key] = value

    for key in ("tools", "allowedTools"):
        value = spec.get(key, [])
        if not isinstance(value, list):
            raise ValueError(f"remote agent {key} must be a list")
        projected[key] = [
            ref
            for ref in value
            if isinstance(ref, str) and _remote_mcp_reference_available(ref, available_names)
        ]

    projected["mcpServers"] = {name: dict(entry) for name, entry in safe_entries.items()}
    return projected


def _is_remote_http_spec(spec: object) -> bool:
    return (
        isinstance(spec, Mapping)
        and spec.get("disabled") is not True
        and isinstance(spec.get("url"), str)
        and bool(str(spec.get("url")).strip())
        and not spec.get("command")
    )


def unsupported_remote_mcp_names(
    spec: Mapping[str, object],
    *,
    resolved_http_names: frozenset[str] = frozenset(),
) -> frozenset[str]:
    """Return enabled HTTP/SSE names that must receive a fail-closed relay."""
    raw_servers = spec.get("mcpServers")
    if not isinstance(raw_servers, Mapping):
        return frozenset()
    return frozenset(
        name
        for name, server_spec in raw_servers.items()
        if (
            isinstance(name, str)
            and name not in resolved_http_names
            and _is_remote_http_spec(server_spec)
        )
    )


def _bounded_text(value: str, limit: int) -> bool:
    return len(value.encode("utf-8")) <= limit and not any(
        ord(character) < 32 or ord(character) == 127 for character in value
    )


def _remote_http_url_is_trusted(url: str) -> bool:
    if not url or not _bounded_text(url, _REMOTE_HTTP_URL_MAX_BYTES):
        return False
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        parsed.port
    except ValueError:
        return False
    if (
        hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return False
    if parsed.scheme == "https":
        return True
    return parsed.scheme == "http" and hostname in _REMOTE_HTTP_LOOPBACK_HOSTS


def _remote_http_headers(raw: object) -> dict[str, str] | None:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping) or len(raw) > _REMOTE_HTTP_HEADER_MAX_COUNT:
        return None
    headers: dict[str, str] = {}
    total_bytes = 0
    for name, value in raw.items():
        if not isinstance(name, str) or not isinstance(value, str):
            return None
        if (
            not _HTTP_HEADER_NAME_RE.fullmatch(name)
            or not _bounded_text(name, _REMOTE_HTTP_HEADER_NAME_MAX_BYTES)
            or not _bounded_text(value, _REMOTE_HTTP_HEADER_VALUE_MAX_BYTES)
        ):
            return None
        total_bytes += len(name.encode("utf-8")) + len(value.encode("utf-8"))
        if total_bytes > _REMOTE_HTTP_HEADERS_MAX_BYTES:
            return None
        headers[name] = value
    return headers


def _remote_http_oauth_hints(
    raw_spec: Mapping[str, object],
    *,
    server_name: str,
) -> tuple[tuple[str, ...], str] | None:
    raw_scopes = raw_spec.get("scopes", raw_spec.get("oauthScopes"))
    oauth = raw_spec.get("oauth")
    if raw_scopes is None and isinstance(oauth, Mapping):
        raw_scopes = oauth.get("oauthScopes")
    if raw_scopes is not None and (
        not isinstance(raw_scopes, list)
        or not all(isinstance(scope, str) and scope.strip() for scope in raw_scopes)
    ):
        return None

    if "clientId" in raw_spec:
        raw_client_id = raw_spec.get("clientId")
    elif isinstance(oauth, Mapping) and "clientId" in oauth:
        raw_client_id = oauth.get("clientId")
    else:
        raw_client_id = None
    if raw_client_id is not None and (
        not isinstance(raw_client_id, str)
        or not raw_client_id.strip()
        or not _bounded_text(raw_client_id, _REMOTE_HTTP_CLIENT_ID_MAX_BYTES)
    ):
        return None

    typed_spec = dict(raw_spec)
    return (
        tuple(kiro_entry_scopes(typed_spec, server=server_name)),
        kiro_entry_client_id(typed_spec),
    )


def resolve_remote_http_mcp_targets(
    spec: Mapping[str, object],
    *,
    session_key: str,
) -> dict[str, RemoteHttpMcpTarget]:
    """Resolve strict HTTP entries to gateway-only service targets."""
    if not session_key:
        raise ValueError("remote MCP targets require a logical session key")
    raw_servers = spec.get("mcpServers")
    if not isinstance(raw_servers, Mapping):
        return {}

    targets: dict[str, RemoteHttpMcpTarget] = {}
    for name, raw_spec in raw_servers.items():
        if (
            not isinstance(name, str)
            or not _REMOTE_MCP_SERVER_NAME_RE.fullmatch(name)
            or not isinstance(raw_spec, Mapping)
            or raw_spec.get("disabled") is True
            or "command" in raw_spec
        ):
            continue
        url = raw_spec.get("url")
        if not isinstance(url, str) or not _remote_http_url_is_trusted(url):
            continue
        headers = _remote_http_headers(raw_spec.get("headers"))
        oauth_hints = _remote_http_oauth_hints(raw_spec, server_name=name)
        if headers is None or oauth_hints is None:
            continue
        scopes, client_id = oauth_hints
        targets[name] = RemoteHttpMcpTarget(
            server_name=name,
            url=url,
            headers=headers,
            scopes=scopes,
            client_id=client_id,
        )
    return targets


def resolve_remote_mcp_targets(
    spec: Mapping[str, object],
    *,
    local_cwd: str | Path,
    session_key: str,
) -> dict[str, RemoteMcpTarget]:
    """Resolve strict stdio entries to gateway-only process targets."""
    if not session_key:
        raise ValueError("remote MCP targets require a logical session key")
    raw_servers = spec.get("mcpServers")
    if not isinstance(raw_servers, Mapping):
        return {}

    targets: dict[str, RemoteMcpTarget] = {}
    for name, raw_spec in raw_servers.items():
        if not isinstance(name, str) or not isinstance(raw_spec, Mapping):
            continue
        if raw_spec.get("disabled") is True or "url" in raw_spec:
            continue
        command = raw_spec.get("command")
        args = raw_spec.get("args", [])
        declared_env = raw_spec.get("env", {})
        if not isinstance(command, str) or not command.strip():
            continue
        if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
            continue
        if not isinstance(declared_env, Mapping) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in declared_env.items()
        ):
            continue

        typed_env: dict[str, str] = dict(declared_env)
        path_key = spec_path_key(typed_env)
        declared_path = typed_env.get(path_key, "") if path_key else ""
        env = dict(os.environ)
        env["PATH"] = mcp_search_path(declared_path)
        env.update(
            sanitize_spec_env((key, value) for key, value in typed_env.items() if key != path_key)
        )
        resolved = shutil.which(command, path=env["PATH"])
        if not resolved:
            continue

        first_party = False
        try:
            from kiro_crew.mcp_discovery import _is_first_party_managed_argv

            first_party = _is_first_party_managed_argv(
                name,
                command,
                list(args),
                typed_env,
            )
        except Exception:
            first_party = False
        targets[name] = RemoteMcpTarget(
            command=resolved,
            args=tuple(args),
            env=env,
            cwd=str(local_cwd),
            first_party=first_party,
        )
    return targets


def validate_coder_url(value: str) -> str:
    """Return a normalized Coder base URL or raise a bounded validation error."""
    url = value.strip().rstrip("/")
    if not url or len(url.encode("utf-8")) > _CODER_URL_MAX_BYTES:
        raise ValueError("Coder URL is required")
    try:
        parsed = urlsplit(url)
        parsed.port
    except ValueError as exc:
        raise ValueError("Coder URL is invalid") from exc
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Coder URL must be an HTTP(S) base URL without credentials")
    return url


def validate_coder_workspace(value: str) -> str:
    workspace = value.strip()
    if not _WORKSPACE_RE.fullmatch(workspace):
        raise ValueError("workspace must be one safe Coder name")
    return workspace


def validate_coder_remote_cwd(value: str) -> str:
    raw = value.strip()
    path = PurePosixPath(raw)
    if not raw or not path.is_absolute() or ".." in path.parts:
        raise ValueError("remote_cwd must be an absolute normalized POSIX path")
    return str(path)


def session_host_from_config(
    work_dir: str | Path,
    *,
    enabled: bool,
    url: str,
    workspace: str,
    remote_cwd: str,
    session_token: str,
    environ: Mapping[str, str] | None = None,
) -> LocalSessionHost | CoderWorkspaceSessionHost:
    """Build the persisted Coder host, failing closed when enabled is incomplete."""
    if not enabled:
        return LocalSessionHost(work_dir)
    if not session_token:
        raise SessionHostError("Coder session hosting requires a session token")
    try:
        coder_url = validate_coder_url(url)
        coder_workspace = validate_coder_workspace(workspace)
        coder_remote_cwd = validate_coder_remote_cwd(remote_cwd)
    except ValueError as exc:
        raise SessionHostError(str(exc)) from exc

    env = os.environ if environ is None else environ
    requested_bin = env.get("KIROCREW_CODER_BIN", "coder")
    coder_bin = shutil.which(requested_bin, path=env.get("PATH"))
    if not coder_bin:
        raise SessionHostError(f"Coder CLI is not executable: {requested_bin}")
    return CoderWorkspaceSessionHost(
        workspace=coder_workspace,
        remote_cwd=coder_remote_cwd,
        coder_bin=coder_bin,
        coder_url=coder_url,
        session_token=session_token,
    )


def managed_coder_manager_from_config(
    *,
    url: str,
    token: str,
    template: str,
    preset: str,
    remote_cwd: str,
    workspace_prefix: str,
    stop_after_minutes: int,
    delete_after_days: int,
    max_running: int,
    local_cwd: Path,
    environ: Mapping[str, str] | None = None,
) -> tuple[CoderWorkspaceManager, str]:
    """Build one gateway manager shared by every parent from a provider factory."""
    if not token:
        raise SessionHostError("Coder session hosting requires a session token")
    try:
        coder_url = validate_coder_url(url)
        coder_template = validate_coder_workspace(template)
        coder_preset = validate_coder_workspace(preset) if preset else ""
        validate_coder_remote_cwd(remote_cwd)
        coder_prefix = validate_coder_workspace(workspace_prefix)
    except ValueError as exc:
        raise SessionHostError(str(exc)) from exc
    env = os.environ if environ is None else environ
    requested_bin = env.get("KIROCREW_CODER_BIN", "coder")
    coder_bin = shutil.which(requested_bin, path=env.get("PATH"))
    if not coder_bin:
        raise SessionHostError(f"Coder CLI is not executable: {requested_bin}")
    manager = CoderWorkspaceManager(
        registry=WorkspaceBindingRegistry(local_cwd / "coder_workspaces.json"),
        client=CoderClient(coder_bin, coder_url, token, local_cwd),
        policy=ManagedWorkspacePolicy(
            template=coder_template,
            preset=coder_preset,
            prefix=coder_prefix,
            stop_after_minutes=stop_after_minutes,
            delete_after_days=delete_after_days,
            max_running=max_running,
        ),
    )
    return manager, coder_bin


async def probe_coder_connection(
    *,
    url: str,
    token: str,
    workspace: str,
    remote_cwd: str,
    local_cwd: str | Path,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Verify that the gateway can execute a bounded command in the workspace."""
    env = os.environ if environ is None else environ
    host = session_host_from_config(
        local_cwd,
        enabled=True,
        url=url,
        workspace=workspace,
        remote_cwd=remote_cwd,
        session_token=token,
        environ=env,
    )
    if not isinstance(host, CoderWorkspaceSessionHost):  # pragma: no cover - enabled=True
        raise SessionHostError("Coder connection test is not configured")
    argv = host._remote_python_argv("print('ok')")
    process = await create_subprocess_limited(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(local_cwd),
        env=host.transport_env(env),
        start_new_session=platform_compat.IS_POSIX,
        creationflags=(
            platform_compat.CREATE_NEW_PROCESS_GROUP | platform_compat._SUBPROCESS_NO_WINDOW
        ),
        profile=RLIMIT_PROFILE_SESSION_HOST,
    )
    try:
        stdout, _stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=_CODER_CONNECTION_TIMEOUT_SECS,
        )
    except asyncio.TimeoutError as exc:
        platform_compat.kill_process_tree(process.pid, platform_compat.SIGKILL)
        await process.wait()
        raise SessionHostError("Coder connection test timed out") from exc
    if process.returncode or stdout.strip() != b"ok":
        raise SessionHostError("Coder connection test failed")


def session_host_from_env(
    work_dir: str | Path,
    environ: Mapping[str, str] | None = None,
) -> LocalSessionHost | CoderWorkspaceSessionHost:
    """Build the opt-in Coder host, otherwise preserve local execution."""
    env = os.environ if environ is None else environ
    workspace = env.get("KIROCREW_CODER_WORKSPACE", "").strip()
    if not workspace:
        return LocalSessionHost(work_dir)

    missing = [key for key in ("CODER_URL", "CODER_SESSION_TOKEN") if not env.get(key)]
    if missing:
        raise SessionHostError("Coder session hosting requires " + ", ".join(missing))

    return session_host_from_config(
        work_dir,
        enabled=True,
        url=env.get("CODER_URL", ""),
        workspace=workspace,
        remote_cwd=env.get("KIROCREW_CODER_REMOTE_CWD", CODER_DEFAULT_REMOTE_CWD),
        session_token=env.get("CODER_SESSION_TOKEN", ""),
        environ=env,
    )
