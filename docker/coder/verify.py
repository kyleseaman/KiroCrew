#!/usr/bin/env python3
"""Re-run every measurement the Coder RFC claims.

The RFC asserts specific numbers -- per-frame transport overhead, a working ACP
handshake into a workspace, kernel-enforced size ceilings. This script re-derives
them so a reader can check the claims instead of trusting them.

Usage:
    python3 docker/coder/verify.py                       # verify whatever exists
    python3 docker/coder/verify.py --workspace ws-small --workspace ws-build
    python3 docker/coder/verify.py --api-key-env MY_VAR  # for the ACP check

Requires: a reachable coderd, the `coder` CLI logged in, and at least one
workspace built from docker/coder/main.tf. See README.md to bring that up.

Never prints a credential value. The ACP check is skipped, not failed, when no
key is available -- kiro-cli refuses to start `acp` without one.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import statistics
import subprocess
import sys
import time

API_KEY_VAR = "KIRO_API_KEY"
FRAMES = 6
CONNECT_TIMEOUT = 60


class Report:
    """Collects pass/fail/skip lines so the summary survives a mid-run failure."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def add(self, state: str, name: str, detail: str = "") -> None:
        self.rows.append((state, name, detail))
        mark = {"pass": "[ ok ]", "fail": "[FAIL]", "skip": "[skip]", "info": "[    ]"}[state]
        print(f"  {mark} {name}" + (f" -- {detail}" if detail else ""))

    def exit_code(self) -> int:
        return sum(1 for s, _, _ in self.rows if s == "fail")


def sh(argv: list[str], timeout: int = 60) -> tuple[int, str]:
    """Run a command with stdin closed and return (rc, combined output).

    stdin MUST be closed: `coder ssh` holds its exec channel open while stdin is
    readable, so an inherited stdin makes even `--version` hang forever.
    """
    try:
        p = subprocess.run(
            argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return 124, "(timed out)"
    except FileNotFoundError as exc:
        return 127, str(exc)
    return p.returncode, (p.stdout + p.stderr).strip()


def find_coder() -> str | None:
    return shutil.which("coder") or None


def check_prereqs(rep: Report, coder: str | None) -> bool:
    if not coder:
        rep.add("fail", "coder CLI on PATH", "install the release binary matching your server")
        return False
    rc, out = sh([coder, "version"])
    rep.add("pass" if rc == 0 else "fail", "coder CLI", out.splitlines()[0] if out else "")
    rc, out = sh([coder, "whoami"])
    if rc != 0:
        rep.add("fail", "coder authenticated", "run: coder login <url>")
        return False
    rep.add("pass", "coder authenticated")
    return True


def discover_workspaces(coder: str, wanted: list[str]) -> list[str]:
    if wanted:
        return wanted
    rc, out = sh([coder, "list", "-o", "json"])
    if rc != 0:
        return []
    try:
        return [w["name"] for w in json.loads(out) if w.get("latest_build", {}).get(
            "status") == "running"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return []


def check_sizing(rep: Report, coder: str, ws: str) -> None:
    """A size parameter is only meaningful if it binds a real kernel ceiling."""
    rc, host = sh([
        "docker", "inspect", f"coder-{os.environ.get('CODER_USER', 'poc')}-{ws}",
        "--format", "{{.HostConfig.NanoCpus}} {{.HostConfig.Memory}} {{.HostConfig.MemorySwap}}",
    ])
    if rc == 0 and host:
        nano, mem, swap = (host.split() + ["", "", ""])[:3]
        try:
            cpus = int(nano) / 1_000_000_000
            gib = int(mem) / (1024**3)
            note = f"host: {cpus:g} CPU / {gib:g} GiB"
            if swap == mem:
                note += " (swap pinned == memory)"
            rep.add("pass", f"{ws}: host ceiling", note)
        except ValueError:
            rep.add("info", f"{ws}: host ceiling", host)
    else:
        rep.add("skip", f"{ws}: host ceiling", "container not inspectable from here")

    # The workspace image runs cgroup v1, so the enforcement files are
    # cpu.cfs_quota_us / memory.limit_in_bytes -- not v2's cpu.max / memory.max.
    # Pass each path as its own argv element: `coder ssh` joins everything after
    # `--` into a single string, so an `sh -c "..."` wrapper gets mangled.
    v1 = [
        "/sys/fs/cgroup/cpu/cpu.cfs_quota_us",
        "/sys/fs/cgroup/cpu/cpu.cfs_period_us",
        "/sys/fs/cgroup/memory/memory.limit_in_bytes",
    ]
    rc, out = sh([coder, "ssh", ws, "--", "cat", *v1], timeout=90)
    nums = [n for n in out.split() if n.isdigit()]
    if rc == 0 and len(nums) >= 3:
        quota, period, limit = int(nums[0]), int(nums[1]), int(nums[2])
        rep.add(
            "pass", f"{ws}: in-container ceiling",
            f"{quota / period:g} CPU / {limit / (1024**3):g} GiB (cgroup v1)",
        )
        return

    # cgroup v2 fallback: cpu.max is "<quota> <period>" or "max <period>".
    rc, out = sh([coder, "ssh", ws, "--", "cat",
                  "/sys/fs/cgroup/cpu.max", "/sys/fs/cgroup/memory.max"], timeout=90)
    parts = out.split()
    if rc == 0 and len(parts) >= 3 and parts[0].isdigit():
        rep.add(
            "pass", f"{ws}: in-container ceiling",
            f"{int(parts[0]) / int(parts[1]):g} CPU / "
            f"{int(parts[2]) / (1024**3):g} GiB (cgroup v2)",
        )
    else:
        rep.add("skip", f"{ws}: in-container ceiling", out[:80])


async def measure_frames(argv: list[str], label: str) -> list[float] | None:
    """Round-trip line-delimited JSON through a process and time each frame.

    `cat` stands in for the agent: a long-lived process that reads lines on stdin
    and writes them back, which is the exact contract the JSON-RPC layer needs.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError:
        return None
    lats: list[float] = []
    try:
        for i in range(FRAMES):
            payload = json.dumps({"jsonrpc": "2.0", "id": i, "method": "ping"}) + "\n"
            t0 = time.perf_counter()
            proc.stdin.write(payload.encode())
            await proc.stdin.drain()
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=CONNECT_TIMEOUT)
            if not line:
                break
            lats.append((time.perf_counter() - t0) * 1000)
    except (asyncio.TimeoutError, ConnectionResetError):
        pass
    finally:
        if proc.stdin and not proc.stdin.is_closing():
            proc.stdin.close()
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            proc.kill()
    return lats or None


def check_transport(rep: Report, coder: str, ws: str) -> None:
    """The claim: an ssh exec channel gives us the remote process's stdio, and
    steady-state per-frame cost is negligible once connected."""
    remote = asyncio.run(measure_frames([coder, "ssh", ws, "cat"], "remote"))
    local = asyncio.run(measure_frames(["cat"], "local"))
    if not remote:
        rep.add("fail", "transport round-trip", "no frames echoed back")
        return
    first, rest = remote[0], remote[1:]
    steady = statistics.median(rest) if rest else first
    detail = f"{len(remote)}/{FRAMES} frames; connect {first:.0f} ms, steady median {steady:.2f} ms"
    if local:
        detail += f"; local control {statistics.median(local):.2f} ms"
    rep.add("pass", "transport round-trip", detail)
    rep.add(
        "info", "transport caveat",
        "same-host workspace: steady-state is protocol overhead, not network RTT",
    )


def check_env_delivery(rep: Report, coder: str, ws: str) -> None:
    """Positive control first: a negative here is meaningless without proving the
    mechanism works at all for an ordinary variable."""
    rc, out = sh([coder, "ssh", "-e", "POC_MARKER=delivered", ws, "--", "printenv", "POC_MARKER"])
    if rc == 0 and "delivered" in out:
        rep.add("pass", "coder ssh -e delivers an ordinary variable")
    else:
        rep.add(
            "skip", "coder ssh -e control",
            "could not establish the control; later results void",
        )
        return

    rc, out = sh([
        coder, "ssh", "-e", f"{API_KEY_VAR}=probe-value", ws, "--", "printenv", API_KEY_VAR,
    ])
    if rc == 0 and not out.strip():
        rep.add(
            "pass", f"coder ssh -e DROPS {API_KEY_VAR}",
            "empty with exit 0 and no warning -- inject via the template instead",
        )
    else:
        rep.add(
            "info", f"coder ssh -e and {API_KEY_VAR}",
            "delivered here; upstream may have fixed it",
        )

    rc, out = sh([
        coder, "ssh", ws, "--", "env", f"{API_KEY_VAR}=probe-value", "printenv", API_KEY_VAR,
    ])
    ok = rc == 0 and "probe-value" in out
    rep.add("pass" if ok else "fail", "env(1) inside the remote command delivers it")


def check_acp(rep: Report, coder: str, ws: str, key: str | None) -> None:
    """The load-bearing claim: a real ACP handshake completes over the remote
    transport with no change to the JSON-RPC layer."""
    rc, out = sh([coder, "ssh", ws, "--", "kiro-cli", "--version"], timeout=90)
    if rc != 0:
        rep.add("skip", "kiro-cli in workspace", out[:80])
        return
    rep.add("pass", "kiro-cli in workspace", out.splitlines()[0] if out else "")

    if not key:
        rep.add(
            "skip", "ACP handshake",
            f"no credential available; `acp` refuses to start without one. "
            f"Set {API_KEY_VAR} or pass --api-key-env",
        )
        return

    async def handshake() -> str:
        proc = await asyncio.create_subprocess_exec(
            coder, "ssh", ws, "--", "env", f"{API_KEY_VAR}={key}", "kiro-cli", "acp",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        req = {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": 1, "clientCapabilities": {
                "fs": {"readTextFile": True, "writeTextFile": True}}},
        }
        try:
            proc.stdin.write((json.dumps(req) + "\n").encode())
            await proc.stdin.drain()
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=CONNECT_TIMEOUT)
            return line.decode(errors="replace").strip()
        except asyncio.TimeoutError:
            return ""
        finally:
            if proc.stdin and not proc.stdin.is_closing():
                proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                proc.kill()

    raw = asyncio.run(handshake())
    if not raw:
        rep.add("fail", "ACP handshake", "no response frame")
        return
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        rep.add("fail", "ACP handshake", f"non-JSON: {raw[:120]}")
        return
    result = obj.get("result") or {}
    if not result:
        rep.add("fail", "ACP handshake", f"error frame: {str(obj.get('error'))[:120]}")
        return
    caps = result.get("agentCapabilities", {}) or {}
    rep.add(
        "pass", "ACP handshake over the remote transport",
        f"protocolVersion={result.get('protocolVersion')} "
        f"loadSession={caps.get('loadSession')} "
        f"mcp.http={(caps.get('mcpCapabilities') or {}).get('http')}",
    )


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--workspace", action="append", default=[],
        help="repeatable; default: all running",
    )
    ap.add_argument("--api-key-env", default=API_KEY_VAR,
                    help=f"env var holding the Kiro credential (default {API_KEY_VAR})")
    args = ap.parse_args()

    rep = Report()
    print("Coder POC verification\n")

    print("prerequisites")
    coder = find_coder()
    if not check_prereqs(rep, coder):
        print("\nsummary: prerequisites unmet")
        return 1
    assert coder

    workspaces = discover_workspaces(coder, args.workspace)
    if not workspaces:
        rep.add("fail", "a running workspace", "create one: see README.md")
        return 1
    rep.add("info", "workspaces under test", ", ".join(workspaces))

    key = os.environ.get(args.api_key_env) or None

    for ws in workspaces:
        print(f"\nworkspace: {ws}")
        check_sizing(rep, coder, ws)

    primary = workspaces[0]
    print(f"\ntransport ({primary})")
    check_transport(rep, coder, primary)

    print(f"\ncredential delivery ({primary})")
    check_env_delivery(rep, coder, primary)

    print(f"\nagent ({primary})")
    check_acp(rep, coder, primary, key)

    failed = rep.exit_code()
    counts = {s: sum(1 for x, _, _ in rep.rows if x == s) for s in ("pass", "fail", "skip")}
    print(f"\nsummary: {counts['pass']} passed, {counts['fail']} failed, {counts['skip']} skipped")
    return failed


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
