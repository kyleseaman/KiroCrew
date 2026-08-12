"""Where an ACP agent process runs.

``AcpClient`` and ``AcpRuntime`` both launch a kiro-cli process the same way:
wrap the argv for the OS sandbox, wrap that in a cgroup scope, then hand it to
:func:`create_subprocess_limited` with a fixed set of stdio and process-group
kwargs. That sequence is the only part of a spawn that depends on *where* the
agent runs, so it lives behind this interface.

The gateway stays the ACP client either way: the JSON-RPC layer reads and writes
the returned process's stdio pipes, and a process reached over a transport is
still a local subprocess from the gateway's point of view. Nothing in the
protocol layer needs to know which host it is talking to.

Environment construction deliberately stays with the callers. The two spawn
paths build genuinely different environments -- the client sets
``KIROCREW_SESSION_KEY``, ``KIROCREW_CHANNEL_ID`` and repairs ``SSH_AUTH_SOCK``,
while the runtime sets none of those -- so a shared env builder would change
behaviour on one path or the other. A host receives the env it should pass on,
and may filter it (a remote host must, since the gateway's env carries
host-local paths and cloud credentials).
"""

from __future__ import annotations

import asyncio
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import NamedTuple

from kiro_crew import platform_compat
from kiro_crew.sandbox import (
    RLIMIT_PROFILE_SESSION_HOST,
    cgroup_scope_argv,
    create_subprocess_limited,
    wrap_argv,
)


class LaunchedProcess(NamedTuple):
    """A started agent process plus the sandbox temp file to unlink on teardown.

    ``sandbox_cleanup`` is whatever :func:`wrap_argv` produced (a launcher script
    on Linux, a Seatbelt profile on macOS) and is ``None`` when the host does not
    wrap the command.
    """

    process: asyncio.subprocess.Process
    sandbox_cleanup: str | None


class SessionHost(ABC):
    """Launches ACP agent processes on one kind of host."""

    #: Stable identifier for logs and config.
    name: str = ""

    #: True when the agent process does not share the gateway's filesystem or
    #: PID namespace. Callers use this to skip local-only bookkeeping
    #: (descendant scans, RSS accounting) that cannot describe a remote process.
    is_remote: bool = False

    @abstractmethod
    async def prepare_work_dir(self, work_dir: Path) -> None:
        """Ensure the agent's working directory is usable before launch.

        Async because the local implementation is a blocking syscall that must
        not run on the event loop, and a remote implementation is I/O to another
        machine.
        """

    @abstractmethod
    def protocol_cwd(self, work_dir: Path) -> str:
        """Return the cwd string to send in ``session/new`` / ``session/load``.

        This is the path as the *agent* will resolve it, which is not
        necessarily the path the gateway sees.
        """

    @abstractmethod
    async def launch(
        self,
        argv: list[str],
        *,
        work_dir: Path,
        env: dict[str, str],
        sandbox_mode: str,
        is_kiro_cli: bool,
        stdout_limit: int,
        label: str,
    ) -> LaunchedProcess:
        """Wrap *argv* for this host, then start it with stdio pipes attached.

        One method rather than three because the spawn-audit ratchet requires the
        sandbox wrap and the spawn to sit in the same function, and because the
        sandbox temp file must not outlive a failure: everything between the wrap
        and the exec is a leak window this method owns.

        On Windows the local host starts the child SUSPENDED, so the caller must
        call ``finish_suspended_spawn`` (from an executor) before treating the
        process as running. An unresumed child is alive but frozen.
        """


class LocalSessionHost(SessionHost):
    """Runs the agent as a child process of the gateway."""

    name = "local"
    is_remote = False

    async def prepare_work_dir(self, work_dir: Path) -> None:
        # mkdir is a blocking syscall and the parent dirs may live on slow
        # storage, so it never runs on the event loop.
        await asyncio.to_thread(work_dir.mkdir, parents=True, exist_ok=True)

    def protocol_cwd(self, work_dir: Path) -> str:
        return str(work_dir)

    @staticmethod
    def _discard_sandbox_file(path: str | None) -> None:
        """Unlink the sandbox temp file wrap_argv allocated.

        wrap_argv writes a launcher/profile file the child consumes at exec.
        Once no child will exec it, it must be removed or every failed attempt
        leaks one file into the temp dir for the gateway's lifetime.
        """
        if path:
            try:
                os.remove(path)
            except OSError:
                pass

    async def launch(
        self,
        argv: list[str],
        *,
        work_dir: Path,
        env: dict[str, str],
        sandbox_mode: str,
        is_kiro_cli: bool,
        stdout_limit: int,
        label: str,
    ) -> LaunchedProcess:
        # OS-level sandbox: hide sensitive paths from the agent and its tool
        # subprocesses. strip_python_env keeps the host PYTHONPATH/PYTHONHOME out
        # of kiro-cli's foreign MCP subprocesses, which bundle their own
        # interpreter and dependencies.
        argv, sandbox_cleanup = wrap_argv(
            argv,
            mode=sandbox_mode,
            strip_python_env=True,
            is_kiro_cli=is_kiro_cli,
        )
        try:
            # cgroup v2 scope, outermost: bounds this agent and every MCP-server
            # and tool descendant with pids.max and memory.max. A no-op with a
            # warning where cgroup delegation is unavailable. --scope execs into
            # the target, so the returned pid is still the real child. Off-loop
            # because the first call probes /proc and /sys and the config read
            # touches the config dir.
            argv = await asyncio.to_thread(cgroup_scope_argv, argv)

            # Both flags are passed explicitly rather than via a dict unpack,
            # which breaks mypy's Popen overload resolution. On POSIX
            # start_new_session calls setsid so the caller can killpg the whole
            # group and creationflags resolves to 0; on Windows there is no
            # setsid, so CREATE_NEW_PROCESS_GROUP makes the tree taskkill
            # /T-reapable and stops an inherited Ctrl-C reaching the gateway.
            # CREATE_SUSPENDED lets the caller apply the Windows resource ceiling
            # before the child runs; it is 0 on POSIX, so the child is never
            # actually suspended there.
            process = await create_subprocess_limited(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(work_dir),
                limit=stdout_limit,
                env=env,
                start_new_session=platform_compat.IS_POSIX,
                creationflags=(
                    platform_compat.CREATE_NEW_PROCESS_GROUP
                    | platform_compat._SUBPROCESS_NO_WINDOW
                    | platform_compat.CREATE_SUSPENDED
                ),
                profile=RLIMIT_PROFILE_SESSION_HOST,
            )
        except BaseException:
            # Every suspension point above is a leak window: a cancellation
            # unwinds launch without the caller ever receiving the token.
            self._discard_sandbox_file(sandbox_cleanup)
            raise
        return LaunchedProcess(process, sandbox_cleanup)


_LOCAL_HOST = LocalSessionHost()


def default_session_host() -> SessionHost:
    """Return the host to use when a caller has no explicit preference."""
    return _LOCAL_HOST
