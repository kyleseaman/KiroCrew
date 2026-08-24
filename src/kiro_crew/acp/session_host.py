"""Execution-host boundary for ACP session processes."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

from kiro_crew import platform_compat
from kiro_crew.sandbox import RLIMIT_PROFILE_SESSION_HOST, create_subprocess_limited

_WORKSPACE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
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
_REMOTE_AGENT_TEXT_KEYS = ("name", "description", "model", "prompt")
_DEFAULT_REMOTE_CWD = "/home/coder/workspace"
_REMOTE_PREPARE_SCRIPT = (
    'set -eu; umask 077; mkdir -p "$HOME/.kiro/agents" "$2"; '
    'cat > "$HOME/.kiro/agents/$1.json"; '
    'chmod 600 "$HOME/.kiro/agents/$1.json"'
)


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


class CoderWorkspaceSessionHost:
    """A kiro-cli process reached through a Coder workspace SSH channel."""

    def __init__(self, *, workspace: str, remote_cwd: str, coder_bin: str) -> None:
        if not _WORKSPACE_RE.fullmatch(workspace):
            raise ValueError("workspace must be one safe Coder name")
        path = PurePosixPath(remote_cwd)
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("remote_cwd must be an absolute normalized POSIX path")
        self._workspace = workspace
        self._remote_cwd = str(path)
        self._coder_bin = coder_bin

    @property
    def is_remote(self) -> bool:
        return True

    @property
    def protocol_cwd(self) -> str:
        return self._remote_cwd

    def spawn_argv(self, *, agent: str, model: str) -> list[str]:
        argv = [
            self._coder_bin,
            "ssh",
            self._workspace,
            "--",
            "kiro-cli",
            "acp",
            "--agent",
            agent,
        ]
        if model:
            argv.extend(("--model", model))
        return argv

    def transport_env(self, environ: Mapping[str, str]) -> dict[str, str]:
        return {key: environ[key] for key in _TRANSPORT_ENV_KEYS if environ.get(key)}

    def prepare_argv(self, *, agent: str) -> list[str]:
        if not _WORKSPACE_RE.fullmatch(agent):
            raise ValueError("agent must be one safe Kiro agent name")
        remote_command = shlex.join(
            [
                "sh",
                "-c",
                _REMOTE_PREPARE_SCRIPT,
                "kirocrew-prepare",
                agent,
                self._remote_cwd,
            ]
        )
        return [self._coder_bin, "ssh", self._workspace, "--", remote_command]

    async def prepare(
        self,
        *,
        agent: str,
        projected_spec: Mapping[str, object],
        environ: Mapping[str, str],
        local_cwd: str | Path,
    ) -> None:
        payload = (json.dumps(projected_spec, separators=(",", ":")) + "\n").encode()
        process = await create_subprocess_limited(
            *self.prepare_argv(agent=agent),
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
        await process.communicate(input=payload)
        if process.returncode:
            # Remote stderr can echo environment values or secret-bearing command
            # failures. The detailed line remains available from a manual
            # `coder ssh`; the gateway error exposes only the bounded operation.
            raise SessionHostError(
                f"Coder workspace preparation failed (exit {process.returncode})"
            )


def project_remote_agent_spec(spec: Mapping[str, object]) -> dict[str, object]:
    """Return the MCP-free subset of an agent spec safe for a POC workspace."""
    projected: dict[str, object] = {}
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
        projected[key] = [ref for ref in value if isinstance(ref, str) and not ref.startswith("@")]

    projected["mcpServers"] = {}
    return projected


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

    requested_bin = env.get("KIROCREW_CODER_BIN", "coder")
    coder_bin = shutil.which(requested_bin, path=env.get("PATH"))
    if not coder_bin:
        raise SessionHostError(f"Coder CLI is not executable: {requested_bin}")

    return CoderWorkspaceSessionHost(
        workspace=workspace,
        remote_cwd=env.get("KIROCREW_CODER_REMOTE_CWD", _DEFAULT_REMOTE_CWD),
        coder_bin=coder_bin,
    )
