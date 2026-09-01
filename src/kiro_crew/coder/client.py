"""Credential-safe Coder CLI adapter returning bounded structured records."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.coder.user_bus import user_bus_command
from kiro_crew.sandbox import RLIMIT_PROFILE_SESSION_HOST, create_subprocess_limited

_COMMAND_TIMEOUT_SECS = 600.0
_OUTPUT_MAX_BYTES = 4 * 1024 * 1024
_DIAGNOSTIC_MAX_BYTES = 2 * 1024
_PIPE_READ_BYTES = 64 * 1024
_HEALTH_OUTPUT_MAX_BYTES = 4 * 1024
_HEALTH_TIMEOUT_SECS = 30.0
_MEMORY_ELEVATED_PERCENT = 80.0
_MEMORY_CRITICAL_PERCENT = 90.0
_BYTES_PER_GIB = 1024**3
_MEMORY_BYTES_MAX = 1 << 60
_WORKSPACE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_CODER_DEADLINE_EXTENSION_MINUTES_MIN = 30
logger = logging.getLogger(__name__)
_WORKSPACE_MEMORY_SCRIPT = r"""
import json
from pathlib import Path


def read_int(path):
    try:
        value = Path(path).read_text(encoding="utf-8").strip()
        return None if value == "max" else int(value)
    except (OSError, ValueError):
        return None


meminfo = {}
for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
    key, value = line.split(":", 1)
    meminfo[key] = int(value.strip().split()[0]) * 1024

total = meminfo["MemTotal"]
available = meminfo["MemAvailable"]
for limit_path, current_path in (
    ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory.current"),
    (
        "/sys/fs/cgroup/memory/memory.limit_in_bytes",
        "/sys/fs/cgroup/memory/memory.usage_in_bytes",
    ),
):
    limit = read_int(limit_path)
    current = read_int(current_path)
    if limit is None or current is None or limit <= 0 or limit >= total:
        continue
    total = limit
    available = min(available, max(0, limit - current))
    break

print(json.dumps({"available_bytes": min(available, total), "total_bytes": total}, separators=(",", ":")))
""".strip()


class CoderClientError(RuntimeError):
    """A bounded Coder lifecycle operation failed."""


@dataclass(frozen=True)
class CoderWorkspace:
    uuid: str
    name: str
    owner: str
    template: str
    status: str
    last_used_at: str


@dataclass(frozen=True)
class CoderWorkspaceMemory:
    available_gb: float
    total_gb: float
    used_percent: float
    pressure: str


Runner = Callable[[list[str], dict[str, str], Path], Awaitable[bytes]]


class CoderClient:
    def __init__(
        self,
        coder_bin: str,
        url: str,
        token: str,
        cwd: Path,
        runner: Runner | None = None,
    ) -> None:
        self.coder_bin = coder_bin
        self.url = url
        self._token = token
        self.cwd = cwd
        self._runner = runner or self._run

    def _env(self) -> dict[str, str]:
        env = {
            key: value
            for key in (
                "PATH",
                "SSL_CERT_FILE",
                "SSL_CERT_DIR",
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "NO_PROXY",
            )
            if (value := os.environ.get(key))
        }
        env["CODER_URL"] = self.url
        env["CODER_SESSION_TOKEN"] = self._token
        return env

    async def _run(self, argv: list[str], env: dict[str, str], cwd: Path) -> bytes:
        process = await create_subprocess_limited(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
            env=env,
            start_new_session=platform_compat.IS_POSIX,
            creationflags=(
                platform_compat.CREATE_NEW_PROCESS_GROUP | platform_compat._SUBPROCESS_NO_WINDOW
            ),
            profile=RLIMIT_PROFILE_SESSION_HOST,
        )
        if process.stdout is None or process.stderr is None:
            raise CoderClientError("Coder lifecycle command pipes are unavailable")
        stdout_task = asyncio.create_task(self._drain_bounded(process.stdout, _OUTPUT_MAX_BYTES))
        stderr_task = asyncio.create_task(
            self._drain_bounded(process.stderr, _DIAGNOSTIC_MAX_BYTES)
        )
        wait_task = asyncio.create_task(process.wait())
        tasks = (stdout_task, stderr_task, wait_task)
        try:
            (stdout, stdout_exceeded), (stderr, _stderr_exceeded), returncode = (
                await asyncio.wait_for(
                    asyncio.gather(*tasks),
                    timeout=_COMMAND_TIMEOUT_SECS,
                )
            )
        except asyncio.TimeoutError as exc:
            platform_compat.kill_process_tree(process.pid, platform_compat.SIGKILL)
            await process.wait()
            raise CoderClientError("Coder lifecycle command timed out") from exc
        except asyncio.CancelledError:
            platform_compat.kill_process_tree(process.pid, platform_compat.SIGKILL)
            await process.wait()
            raise
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        if returncode:
            diagnostic = self._redacted_diagnostic(stderr)
            if diagnostic:
                logger.warning("Coder lifecycle command failed: %s", diagnostic)
            raise CoderClientError(f"Coder lifecycle command failed (exit {returncode})")
        if stdout_exceeded:
            raise CoderClientError("Coder lifecycle command returned too much data")
        return stdout

    @staticmethod
    async def _drain_bounded(
        stream: asyncio.StreamReader,
        limit: int,
    ) -> tuple[bytes, bool]:
        retained = bytearray()
        exceeded = False
        while chunk := await stream.read(_PIPE_READ_BYTES):
            remaining = limit - len(retained)
            if remaining > 0:
                retained.extend(chunk[:remaining])
            if len(chunk) > remaining:
                exceeded = True
        return bytes(retained), exceeded

    @staticmethod
    def _redacted_diagnostic(stderr: bytes) -> str:
        if not stderr:
            return ""
        from kiro_crew.security import redact_and_truncate

        return redact_and_truncate(
            stderr.decode("utf-8", errors="replace"),
            max_chars=_DIAGNOSTIC_MAX_BYTES,
        ).strip()

    async def _call(self, *args: str) -> bytes:
        return await self._runner([self.coder_bin, *args], self._env(), self.cwd)

    @staticmethod
    def _workspace(raw: object) -> CoderWorkspace | None:
        if not isinstance(raw, dict):
            return None
        latest = raw.get("latest_build")
        status = latest.get("status", "") if isinstance(latest, dict) else raw.get("status", "")
        values = {
            "uuid": raw.get("id", ""),
            "name": raw.get("name", ""),
            # Lifecycle authorization binds to the immutable Coder owner ID.
            # Older servers may omit it, so retain the display-name fallback
            # without preferring that mutable label when both are present.
            "owner": raw.get(
                "owner_id",
                raw.get("owner_name", raw.get("owner", "")),
            ),
            "template": raw.get("template_name", raw.get("template", "")),
            "status": status,
            "last_used_at": raw.get("last_used_at", ""),
        }
        if not all(isinstance(value, str) for value in values.values()):
            raise CoderClientError("Coder workspace record has an invalid shape")
        if not values["uuid"] or not values["name"]:
            return None
        return CoderWorkspace(**values)

    @staticmethod
    def _json(output: bytes, operation: str) -> Any:
        if not output.strip():
            return []
        try:
            return json.loads(output)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise CoderClientError(f"Coder {operation} returned invalid data") from exc

    async def list_workspaces(self) -> tuple[CoderWorkspace, ...]:
        raw = self._json(await self._call("list", "--output", "json"), "workspace query")
        if not isinstance(raw, list):
            raise CoderClientError("Coder workspace query returned an invalid shape")
        records: list[CoderWorkspace] = []
        for value in raw:
            workspace = self._workspace(value)
            if workspace is not None:
                records.append(workspace)
        return tuple(records)

    async def get_workspace(self, name: str) -> CoderWorkspace | None:
        return next(
            (workspace for workspace in await self.list_workspaces() if workspace.name == name),
            None,
        )

    async def current_user(self) -> tuple[str, str]:
        """Return the authenticated Coder username and immutable user id."""
        owner_bytes = await self._call("whoami", "--output", "json")
        if len(owner_bytes) > 1024:
            raise CoderClientError("Coder identity response is too large")
        identity = self._json(owner_bytes, "identity query")
        if not isinstance(identity, list) or len(identity) != 1:
            raise CoderClientError("Coder identity response is invalid")
        record = identity[0]
        if not isinstance(record, dict):
            raise CoderClientError("Coder identity response is invalid")
        owner = record.get("username")
        owner_id = record.get("user_id")
        if not isinstance(owner, str) or not owner or not isinstance(owner_id, str) or not owner_id:
            raise CoderClientError("Coder identity response is invalid")
        return owner, owner_id

    async def probe(self, *, template: str, preset: str) -> dict[str, str]:
        owner, _owner_id = await self.current_user()
        raw = self._json(
            await self._call("templates", "list", "--output", "json"),
            "template query",
        )
        if not isinstance(raw, list):
            raise CoderClientError("Coder template query returned an invalid shape")
        names: set[str] = set()
        for value in raw:
            if not isinstance(value, dict):
                continue
            # Coder's table renderer wraps current JSON records under the
            # display-column label; older releases returned the record itself.
            record = value.get("Template", value)
            if isinstance(record, dict) and isinstance(record.get("name"), str):
                names.add(record["name"])
        if template not in names:
            raise CoderClientError("Coder template is unavailable")
        # Presets are applied by the create command. Coder deployments that do
        # not advertise preset metadata still validate the template without
        # provisioning compute; create remains the authoritative preset check.
        _ = preset
        return {"owner": owner, "template": template}

    async def create_workspace(
        self,
        *,
        name: str,
        template: str,
        preset: str,
        stop_after_minutes: int,
    ) -> CoderWorkspace:
        workspace_name = self._validated_workspace_name(name)
        args = ["create", workspace_name, "--template", template]
        if preset:
            args.extend(("--preset", preset))
        args.extend(
            (
                "--stop-after",
                f"{stop_after_minutes}m",
                "--yes",
                "--use-parameter-defaults",
            )
        )
        await self._call(*args)
        workspace = await self.get_workspace(workspace_name)
        if workspace is None:
            raise CoderClientError("Coder did not return the created workspace")
        return workspace

    async def start_workspace(self, name: str) -> CoderWorkspace:
        workspace_name = self._validated_workspace_name(name)
        await self._call("start", workspace_name, "--yes")
        workspace = await self.get_workspace(workspace_name)
        if workspace is None:
            raise CoderClientError("Coder did not return the started workspace")
        return workspace

    async def stop_workspace(self, name: str) -> CoderWorkspace:
        await self._call("stop", self._validated_workspace_name(name), "--yes")
        workspace = await self.get_workspace(name)
        if workspace is None:
            raise CoderClientError("Coder did not return the stopped workspace")
        return workspace

    async def delete_workspace(self, name: str) -> None:
        await self._call("delete", self._validated_workspace_name(name), "--yes")

    @staticmethod
    def _validated_workspace_name(name: str) -> str:
        if not _WORKSPACE_NAME_RE.fullmatch(name):
            raise CoderClientError("Coder workspace name is invalid")
        return name

    async def has_active_workload_scope(self, name: str) -> bool:
        """Return whether this workspace has a live Kiro Crew systemd scope."""
        workspace = self._validated_workspace_name(name)
        pattern = f"kirocrew-{workspace}-*.scope"
        command = user_bus_command(
            [
                "systemctl",
                "--user",
                "list-units",
                "--type=scope",
                "--state=active",
                "--no-legend",
                "--plain",
                "--no-pager",
                pattern,
            ]
        )
        output = await self._call("ssh", "--disable-autostart", workspace, "--", command)
        return bool(output.strip())

    async def workspace_memory(self, name: str) -> CoderWorkspaceMemory:
        """Read bounded memory telemetry without starting a stopped workspace."""
        workspace = self._validated_workspace_name(name)
        remote_command = shlex.join(
            [
                "env",
                "-u",
                "CODER_AGENT_TOKEN",
                "-u",
                "CODER_AGENT_TOKEN_FILE",
                "python3",
                "-c",
                _WORKSPACE_MEMORY_SCRIPT,
            ]
        )
        try:
            output = await asyncio.wait_for(
                self._call(
                    "ssh",
                    "--disable-autostart",
                    workspace,
                    "--",
                    remote_command,
                ),
                timeout=_HEALTH_TIMEOUT_SECS,
            )
        except asyncio.TimeoutError as exc:
            raise CoderClientError("Coder workspace health probe timed out") from exc
        if len(output) > _HEALTH_OUTPUT_MAX_BYTES:
            raise CoderClientError("Coder workspace health probe returned too much data")
        raw = self._json(output, "workspace health probe")
        if not isinstance(raw, dict):
            raise CoderClientError("Coder workspace health probe returned an invalid shape")
        available = raw.get("available_bytes")
        total = raw.get("total_bytes")
        if (
            not isinstance(available, int)
            or isinstance(available, bool)
            or not isinstance(total, int)
            or isinstance(total, bool)
            or total <= 0
            or total > _MEMORY_BYTES_MAX
            or available < 0
            or available > total
        ):
            raise CoderClientError("Coder workspace health probe returned invalid memory data")
        used_percent = (total - available) / total * 100
        if used_percent >= _MEMORY_CRITICAL_PERCENT:
            pressure = "critical"
        elif used_percent >= _MEMORY_ELEVATED_PERCENT:
            pressure = "elevated"
        else:
            pressure = "normal"
        return CoderWorkspaceMemory(
            available_gb=round(available / _BYTES_PER_GIB, 2),
            total_gb=round(total / _BYTES_PER_GIB, 2),
            used_percent=round(used_percent, 1),
            pressure=pressure,
        )

    async def extend_workspace_deadline(self, name: str, minutes: int) -> None:
        """Renew a running workspace deadline without reprovisioning it."""
        workspace = self._validated_workspace_name(name)
        duration = max(_CODER_DEADLINE_EXTENSION_MINUTES_MIN, minutes)
        await self._call("schedule", "extend", workspace, f"{duration}m")
