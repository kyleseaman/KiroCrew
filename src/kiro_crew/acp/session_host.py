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


class CoderWorkspaceHostError(RuntimeError):
    """The Coder host could not be used, with a reason worth showing a user."""


#: Stdout ceiling for the short bookkeeping commands (mkdir and friends). Their
#: output is only ever a diagnostic message, so a small cap is enough and keeps a
#: misbehaving transport from buffering without bound.
_SHORT_OUTPUT_LIMIT = 64 * 1024


#: Environment names a remote spawn may carry. Deny-by-default: the gateway's own
#: environment holds host-local paths and cloud credentials, and
#: ``scrub_agent_denied_env`` (which the ACP paths call) does NOT drop
#: ``AWS_SECRET_ACCESS_KEY`` or ``AWS_SESSION_TOKEN`` -- only ``scrub_env`` does.
#: Copying the gateway env to another machine would therefore ship live
#: credentials off-box, so a remote host forwards this list and nothing else.
#:
#: Credentials are absent on purpose. The agent's own key reaches the workspace
#: from a per-user Coder secret that coderd injects at workspace start, so it
#: never transits the gateway or this transport.
REMOTE_ENV_ALLOWLIST: frozenset[str] = frozenset(
    {
        "KIROCREW_SESSION_KEY",
        "KIROCREW_CHANNEL_ID",
    }
)


class CoderWorkspaceSessionHost(SessionHost):
    """Runs the agent inside a Coder workspace, reached over ``coder ssh``.

    The gateway still speaks ACP: ``coder ssh <ws> -- <cmd>`` gives a local
    subprocess whose stdin/stdout ARE the remote process's stdio, which is
    exactly the shape the JSON-RPC layer already consumes.

    Two measured constraints shape this class.

    ``coder ssh <ws> -- <cmd>`` joins the remote arguments into ONE string before
    the remote shell sees them (RFC 4254 section 6.5), so inner quoting is lost
    and any token containing whitespace is re-split on the far side. Rather than
    mis-launch, :meth:`_check_tokens` refuses such a token up front.

    ``coder ssh -e VAR=value`` silently drops some names (measured: the agent key
    is dropped with exit 0 and no warning), so forwarded variables are passed as
    an ``env`` prefix on the remote command instead, which was measured to work.
    Values in a remote command are visible in that workspace's process list, so
    only the non-secret session wiring in :data:`REMOTE_ENV_ALLOWLIST` travels.
    """

    name = "coder"
    is_remote = True

    def __init__(self, workspace: str, *, remote_work_dir: str = "~/workspace") -> None:
        if not workspace or not workspace.strip():
            raise CoderWorkspaceHostError("Coder host requires a workspace name")
        self._workspace = workspace.strip()
        self._remote_work_dir = remote_work_dir.rstrip("/") or "~/workspace"
        self._check_tokens(self._workspace, self._remote_work_dir)

    @staticmethod
    def _check_tokens(*tokens: str) -> None:
        """Refuse a token the transport cannot carry intact.

        The remote arguments are re-split on whitespace, so a value containing a
        space would silently arrive as two arguments. Failing here names the
        problem instead of producing a launch that fails at the handshake with
        nothing explaining why.
        """
        for tok in tokens:
            if any(c.isspace() for c in tok):
                raise CoderWorkspaceHostError(
                    f"coder ssh cannot carry an argument containing whitespace: {tok!r}"
                )

    def _coder_bin(self) -> str:
        # Resolved from root-owned directories rather than PATH: the gateway's
        # PATH can lead with agent-writable dirs, and this is the binary that
        # reaches another machine.
        found = platform_compat.trusted_system_bin("coder")
        if not found:
            raise CoderWorkspaceHostError(
                "the 'coder' CLI was not found in a trusted system directory"
            )
        return found

    def _remote_path(self, work_dir: Path) -> str:
        """Map a gateway-side work dir onto its path inside the workspace.

        Only the final component carries over: the gateway's absolute path is
        meaningless in the workspace, and the session's directory name is what
        distinguishes one session's tree from another's.
        """
        return f"{self._remote_work_dir}/{work_dir.name}"

    def protocol_cwd(self, work_dir: Path) -> str:
        # The agent resolves this, not the gateway, so it must be the remote path.
        return self._remote_path(work_dir)

    async def _spawn_transport(
        self,
        remote_argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        sandbox_mode: str,
        is_kiro_cli: bool,
        stdout_limit: int,
        stdin: int,
        stderr: int,
    ) -> tuple[asyncio.subprocess.Process, str | None]:
        """Start one ``coder ssh`` transport process.

        Every spawn this host makes goes through here, so the sandbox wrap, the
        cgroup scope and the spawn stay in one function -- both for the
        spawn-audit chokepoint and because the sandbox temp file must be
        discarded if anything between the wrap and the exec fails.

        The agent itself is confined by the workspace, which is a stronger
        boundary than Crew's OS sandbox. What runs locally is the coder client,
        and that is still a local spawn carrying agent-influenced arguments, so it
        is wrapped and scoped like any other.

        UNVERIFIED: whether the OS sandbox permits the outbound connection this
        client needs has not been measured against a live coderd. If it does not,
        this host needs sandbox mode "off", which would be a documented
        constraint rather than a silent failure.
        """
        self._check_tokens(*remote_argv)
        transport_argv = [
            self._coder_bin(),
            "ssh",
            self._workspace,
            "--",
            *remote_argv,
        ]
        transport_argv, sandbox_cleanup = wrap_argv(
            transport_argv,
            mode=sandbox_mode,
            strip_python_env=True,
            is_kiro_cli=is_kiro_cli,
        )
        try:
            # Bounds the local coder client, not the agent -- the workspace's own
            # CPU and memory ceilings bound the agent, which is the point of
            # running it there. A runaway client is still worth capping.
            transport_argv = await asyncio.to_thread(cgroup_scope_argv, transport_argv)
            process = await create_subprocess_limited(
                *transport_argv,
                stdin=stdin,
                stdout=asyncio.subprocess.PIPE,
                stderr=stderr,
                cwd=str(cwd),
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
            LocalSessionHost._discard_sandbox_file(sandbox_cleanup)
            raise
        return process, sandbox_cleanup

    async def _run_remote(self, *remote_argv: str) -> tuple[int, str]:
        """Run one short command in the workspace and collect its output."""
        process, sandbox_cleanup = await self._spawn_transport(
            list(remote_argv),
            cwd=Path.cwd(),
            env=dict(os.environ),
            sandbox_mode="auto",
            is_kiro_cli=False,
            stdout_limit=_SHORT_OUTPUT_LIMIT,
            stdin=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await process.communicate()
        finally:
            LocalSessionHost._discard_sandbox_file(sandbox_cleanup)
        return process.returncode or 0, out.decode("utf-8", "replace")

    async def prepare_work_dir(self, work_dir: Path) -> None:
        # The directory that matters is the one in the workspace; the gateway's
        # own copy is never the agent's cwd on this host.
        remote = self._remote_path(work_dir)
        rc, out = await self._run_remote("mkdir", "-p", remote)
        if rc != 0:
            raise CoderWorkspaceHostError(
                f"could not create {remote} in workspace {self._workspace}: {out.strip()[:200]}"
            )

    @staticmethod
    def _forwarded_env(env: dict[str, str]) -> list[str]:
        """Return ``NAME=value`` tokens for the allowlisted variables present."""
        forwarded = []
        for name in sorted(REMOTE_ENV_ALLOWLIST):
            value = env.get(name)
            if value:
                forwarded.append(f"{name}={value}")
        return forwarded

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
        remote_env = self._forwarded_env(env)
        # `cd <dir> && exec` is not available: the arguments are re-joined, so a
        # shell operator would have to survive that round trip. env(1) takes the
        # working directory with --chdir and then execs, which needs no shell.
        remote_argv = [
            "env",
            f"--chdir={self._remote_path(work_dir)}",
            *remote_env,
            *argv,
        ]
        process, sandbox_cleanup = await self._spawn_transport(
            remote_argv,
            cwd=work_dir,
            env=env,
            sandbox_mode=sandbox_mode,
            is_kiro_cli=is_kiro_cli,
            stdout_limit=stdout_limit,
            stdin=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        return LaunchedProcess(process, sandbox_cleanup)


#: Preview gate. Selecting a remote host changes where every session's code runs,
#: so it is opt-in through the environment rather than defaulted from config while
#: the remaining workstreams (remote MCP, lifecycle) are unbuilt: a session on
#: this host today starts, but has no KiroCrew tool layer and no working
#: descendant or RSS accounting.
_HOST_ENV = "KIROCREW_SESSION_HOST"
_CODER_WORKSPACE_ENV = "KIROCREW_CODER_WORKSPACE"
_CODER_REMOTE_DIR_ENV = "KIROCREW_CODER_REMOTE_WORKDIR"


def default_session_host() -> SessionHost:
    """Return the host to use when a caller has no explicit preference."""
    if os.environ.get(_HOST_ENV, "").strip().lower() != "coder":
        return _LOCAL_HOST
    workspace = os.environ.get(_CODER_WORKSPACE_ENV, "").strip()
    if not workspace:
        raise CoderWorkspaceHostError(
            f"{_HOST_ENV}=coder requires {_CODER_WORKSPACE_ENV} to name a workspace"
        )
    remote_dir = os.environ.get(_CODER_REMOTE_DIR_ENV, "").strip() or "~/workspace"
    return CoderWorkspaceSessionHost(workspace, remote_work_dir=remote_dir)
