---
title: Coder-backed remote session sandboxes
status: draft
author: kyleseaman
created: 2026-08-11
last-audited: 2026-08-11
audited-at: 88e00ff6a
doc-pr:
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---
# RFC: Coder-backed remote session sandboxes

- Status: draft — **nothing is on main.** A working proof of concept exists
  (§5) and validated the transport, the auth mechanism, and per-session resource
  ceilings; the KiroCrew-side change is unwritten. The harness lives at
  `docker/coder/`.

## 1. Problem

Kiro Crew assumes it owns a whole machine. The gateway, every agent process,
every MCP server, and every build and test run share one host's cores, memory,
and toolchain. Four consequences:

1. **Deploying into a customer's environment** means asking them to trust a box
   we provision, rather than running inside infrastructure they already govern.
2. **An always-on Crew must be sized for its peak**, so a small control plane is
   impossible — a deliberately undersized host has no headroom to govern, and
   admission control on 2 vCPU just makes everything queue.
3. **No per-project toolchain**, and no non-Linux target at all.
4. **Sessions trample each other.** Nothing caps CPU per session, so one gate
   run saturates the machine (§7.3).

[Coder](https://github.com/coder/coder) provisions development workspaces from
Terraform templates. Integrating with it splits Crew into a **small always-on
control plane** plus an **elastic fleet of per-session execution sandboxes**.

## 2. Motivations, ranked

Ranked deliberately: the order determines what to build first, and two commonly
cited motivations are the *weakest*.

**2.1 Deploy on customer infrastructure — strongest, cheapest.** A template is a
reviewable artifact a platform team approves once, and we inherit their network
controls and image policy. Today's remote story cannot do this: `src/kiro_crew/cloud/`
is AWS-welded end to end (every call shells to the `aws` CLI through one
chokepoint, the provisioner *is* CloudFormation, and the bootstrap is ~245 lines
of bash inlined in a `!Sub` block in `src/kiro_crew/cloud/templates/kirocrew-ec2.yaml`)
and it assumes the AWS account is ours to provision into.

**2.2 Always-on Crew on a small box — the only motivation that *requires*
remote execution.** The others can be satisfied by governing the local host
better. This one cannot. Coder also closes a gap we have not:
`docs/request-for-change/rfc-tailnet-dashboard-access.md` is explicitly
`partial`, noting that `tailscale serve` works largely by accident and that
token IP pinning is inert behind a proxy. A `coder_app` puts the dashboard
behind coderd's own authenticated reverse proxy.

**2.3 Per-session toolchain and image heterogeneity — real capability, no
alternative.** Right-sizing is the same mechanism. Linux and Windows only (§8).

**2.4 Local resource pressure — real, but NOT what this fixes.** Recorded so we
do not justify the project on it. Disk: a representative dev host carried 157
worktrees over 143.9 GiB, of which **81.4 GiB was duplicated `node_modules` +
`.venv`** — recreatable bytes, on a mount with 61% free. A shared package store
reclaims most of it. CPU: measured at load 27.7 on 16 cores, gate runs were
**~318%** while agent sessions were **~1.1%**, and load fell to 4.9 within ten
minutes. Contention is *bursty*, and admission control fixes bursty contention.

## 3. What Coder gives us, and what it does not

**Gives us:** Terraform-defined workspaces over Docker/Kubernetes/EC2/Azure/GCE;
`coder_parameter` values settable at create time, so one template serves many
sizes; `coder_agent.os` accepting `linux`, `darwin`, `windows`; a full REST API;
`coder ssh` as an exec channel; `coder_app` for authenticated ingress.

**Does not give us:** there is **no `coder exec`** — `coder ssh <ws> -- <cmd>`
joins args into one string (RFC 4254 §6.5), so inner quoting is lost. There is
no `coder cp`. There is no Python SDK. **Prebuilds, quotas, dormancy,
auto-delete, template RBAC, audit log and service accounts are all
Premium-licensed**; on AGPL we get autostop/TTL and write our own reaper. And
prebuild *claiming* re-runs `terraform apply` with a new owner/name, which for
AWS `user_data` — where the Windows starter template puts `init_script` — forces
instance replacement, so prebuilds buy nothing there without `ignore_changes`.

## 4. Architecture

### 4.1 The seam decision

The question is *where kiro-cli runs*.

**A. Whole agent remote, gateway unchanged.** The workspace owns the session,
which reintroduces the memory flow-back problem (§7.5).

**B. Agent local, only shell/file tools remote.** Needs a pluggable execution
backend inside kiro-cli, and keeps kiro-cli resident on the control plane —
fighting §2.2.

**C. ACP over a remote transport — RECOMMENDED.** kiro-cli runs in the
workspace; **the gateway remains the ACP client**, which it already is. Today
`AcpClient._spawn` (`src/kiro_crew/acp/client.py`) creates a *local* subprocess
and speaks JSON-RPC over its stdio pipes. Nothing about ACP requires
co-location — the code merely assumes it. Point that transport at a kiro-cli in
a workspace and:

- Transcripts stay on the gateway, because the gateway is the client and sees
  every message. **The flow-back problem disappears rather than being solved.**
- Session memory moves to the workspace (measured: 311–412 MB RSS per session).
- The remote holds no Crew state, so there is nothing to merge (§7.5).
- The existing sandbox wrapper becomes redundant *inside* the workspace — the
  workspace is the isolation boundary. This is also the only way we support
  Windows, where we currently fail closed with no backend.

**One seam, two transports.** `AcpClient._spawn` and `AcpRuntime.spawn`
(`src/kiro_crew/acp/runtime.py`) both funnel into
`create_subprocess_limited(..., profile=RLIMIT_PROFILE_SESSION_HOST)` in
`src/kiro_crew/sandbox.py`. But they do *not* share a transport model:
`AcpClient` has **no reader task** (pull-based, reading stdout at each call site
under `_turn_lock`), while `AcpRuntime` has a dedicated `_reader_task` that is
the exclusive stdout owner. One abstraction covers both at the spawn primitive,
not at the transport layer.

### 4.2 What must change

1. A `SessionHost` abstraction above `create_subprocess_limited`, with today's
   local path as implementation #1 and a Coder-workspace path as #2.
2. A **per-session/per-project execution profile**. None exists: `agent.sandbox`
   is a global `["auto","off"]` enum. This profile maps to a template plus
   `coder_parameter` values.
3. Path mapping. The protocol-level cwd crosses ACP as a bare `str()` with no
   validation, so `session/new` can carry a remote path — but several admission
   gates reject a non-local path, and two fail *silently* (a stored cwd that is
   not a local dir is dropped; `default_project_dir()` degrades to `""`).
4. Repo delivery into the workspace (git clone by parameter, or rsync).
5. An artifact/outbox return path.
6. **MCP transport** (§7.4).
7. Preview/port handling via `coder port-forward`.

### 4.3 Auth

Use **`KIRO_API_KEY`**, the documented portable credential, injected as a
workspace env var from the template. Verified in the POC (§5.2). Note it does
**not appear anywhere in this repo today** — nothing plumbs it.

Gotcha: `KIRO_HOME` does **not** relocate credentials; they resolve from
`HOME` / `XDG_DATA_HOME` / `LOCALAPPDATA`.

## 5. Proof of concept — measured results

Harness: `docker/coder/`. coderd v2.36.0 on loopback with telemetry disabled, a
1.01 GB workspace image carrying kiro-cli, a template with a size parameter, two
live workspaces.

### 5.1 The transport works, and is nearly free per frame

`coder ssh <ws> <cmd>` gives the gateway a **local subprocess whose stdin/stdout
are the remote process's stdio** — exactly the shape `AcpClient`/`AcpRuntime`
already consume. Five JSON frames round-tripped bidirectionally.

| Path | First frame | Steady-state median |
|---|---|---|
| `coder ssh <ws> cat` | 155.1 ms | **0.3 ms** |
| local `cat` (control) | 0.3 ms | 0.0 ms |

Cost is a one-time ~155 ms connection setup, not a per-frame tax. Caveat: the
workspace is a container on the same host, so per-frame cost on a genuinely
remote box will be dominated by real RTT. What this establishes is that the
protocol layer adds nothing.

### 5.2 The ACP handshake completes over the remote transport

A real kiro-cli ACP agent inside the workspace, driven from the host, returned a
full `initialize` result in 1326 ms with exit 0 — including
`loadSession: true` and **`mcpCapabilities: {"http": true, "sse": false}`**.
**No change to the JSON-RPC layer was required.**

### 5.3 Per-session right-sizing binds a real kernel ceiling, and varies

| Workspace | Host `NanoCpus` | Host `Memory` | In-container quota | In-container mem limit |
|---|---|---|---|---|
| `small` | 2,000,000,000 | 2 GiB | 200000 → **2 CPU** | 2 GiB |
| `build` | 8,000,000,000 | 8 GiB | 800000 → **8 CPU** | 8 GiB |

4× CPU and 4× memory from one `--parameter`. `MemorySwap` is pinned equal to
`Memory` so swap cannot escape the limit. The image runs **cgroup v1**, so
enforcement lives in `cpu.cfs_quota_us` / `memory.limit_in_bytes`. Cold start
with the image already local: agent connected in **385 ms**.

### 5.4 Not proven

A session + prompt with real tool use (needs a valid credential — a bogus key
clears kiro-cli's startup gate because it checks *presence, not validity*, but
fails at the model call); real-network latency; remote MCP; transcripts landing
locally end to end; Windows/macOS images; cold-start cost for a fresh image.

## 6. Value

**For customers:** Crew runs inside their governance boundary; adoption is a
template review; per-project images mean Crew works on their toolchain.

**For us:** session memory leaves the control plane; blast radius is a
disposable workspace; Windows failures become reproducible interactively. And
**fan-out costs no extra credits** — Kiro limits are per-user, not per-device.

**Value we should NOT claim:** not a disk fix (§2.4); not a model-throughput fix
— all sandboxes share one kiro-cli identity (§7.6).

## 7. Constraints and prerequisites

### 7.1 The control plane has a floor

A long-running gateway measured **4.58 GB RSS = 3.39 GB anonymous + 0.98 GB
file-backed**, the file-backed side mapping the embedding model, llama.cpp,
FAISS and OpenBLAS. The mmapped model is evictable; **the 3.39 GB anonymous heap
is a hard floor.** So a 1 GB control plane is out and 2 GB would thrash; the
practical floor is 2 vCPU / 4 GB, and what that costs depends entirely on the
provider. **Highest-leverage unlock: make the embedding + vector stack optional
or remote.** Independent of Coder.

### 7.2 Prerequisite: the HTTP 429 misclassification

`src/kiro_crew/acp/client.py` matches only `50[0234]|529` as retryable status
shapes; throttles are recognised only by exception name or the words
"throttl"/"rate limit". A bare **`HTTP 429` falls through to the unknown-shape
branch and is treated as terminal** — the turn dies instead of retrying. There
is no `Retry-After` handling for the model backend. Rare today, routine at
higher concurrency. **Fix before raising concurrency, remote or not.**

### 7.3 Prerequisite: CPU governance (independent of Coder)

`RLIMIT_PROFILE_SESSION_HOST` sets `NOFILE` only. `CPUQuota` exists in
`cgroup_scope_argv` but is opt-in and 0 by default; `CPUWeight` is explicitly
"never a hard throttle". `compute_max_subagents` never calls `os.getloadavg()`.
The only real admission gate is `spawn_min_memory_gb`, and `resource_status`
documents itself as *"advisory only… Two sessions can both read 'ample' and both
launch heavy work."* With `-n auto` in `setup.cfg`, two worktrees each take every
core. Note also that **`nproc` does not respect cgroup quota**, so a right-sized
workspace protects the host but does not size the run — `inject_xdist_auto_cap`
(currently memory-based) is the right place to add cgroup awareness.

### 7.4 MCP

kiro-cli's client already speaks **Streamable HTTP** MCP, and KiroCrew already
emits HTTP MCP entries for apps (bound to loopback). The rewriter deliberately
skips remote entries as "already shareable by nature". Missing: the broker has
**no TCP listener** (no `AF_INET` in `src/kiro_crew/mcp_gateway/`), and its auth
model has **no primitive that survives leaving the host** — peer-credential
checks, `/proc` ancestry and 0600/0700 mode bits are same-kernel facts, with no
token on the wire. Design it as HTTP + a bearer token.

For a POC this is sidestepped: a session with `mcpServers: []` is **first-class
supported** (a lite agent ships with an empty map, `AcpRuntime` has an
`expect_mcp_reports` flag for it, and the field must be present but may be
empty). Surviving tools are the kiro-cli built-ins — `execute_bash`, `fs_read`,
`fs_write`, `code`, `grep`, `glob`, `web_fetch`, `web_search` — enough to prove
remote execution with real tool use. Lost: the KiroCrew capability layer.

### 7.5 The remote must hold no Crew state

Under option C this is automatic. Recorded so nobody reintroduces it:
`snapshot`/`restore` staging is an allowlist that never includes `sessions/` or
`lessons.jsonl`; the memory merge is `INSERT OR IGNORE`, keeping the local row
and discarding the incoming one; deletions do not propagate; the consolidation
offset is tied to a rotation generation; the gateway lock is per-home, so the
anti-clobber invariant does not exist across hosts.

### 7.6 One identity for all sandboxes

Both spawn paths do `env = {**os.environ}` and never set `HOME`/`AWS_*`/`XDG_*`;
isolation is cwd-only. Every session, subagent and cron agent runs as the same
kiro-cli identity, so fan-out multiplies cores but **not** backend throughput.

### 7.7 Env must become an allowlist

`scrub_agent_denied_env` covers only channel tokens, so **`AWS_SECRET_ACCESS_KEY`
and `AWS_SESSION_TOKEN` reach kiro-cli** on these paths (only `scrub_env` drops
those, and these spawns do not call it), alongside `SSH_AUTH_SOCK`,
`KRB5CCNAME`, `PYTHONPATH` and other host-local paths. Locally this is
deliberate — the standard sandbox tier intentionally leaves AWS env alone.
Copying it to *another host* is not. Remote spawns must **allowlist**.

Incidental: a comment in `src/kiro_crew/dashboard/server.py` claims
`KIROCREW_INTERNAL_SECRET` is "stripped from agent env"; no such strip exists in
either ACP spawn path.

### 7.8 Process lifecycle degrades; Coder supplies stronger equivalents

Over an SSH channel the gateway holds only the ssh client: `exit_code` becomes
ssh's status, descendant enumeration returns empty, and the orphan sweep's
identity model collapses (it rests on same-uid `/proc`, reparent-to-init, and
SID inheritance from `start_new_session=True`). `cgroup_scope_argv` would cap
ssh rather than the agent.

The mitigation is architectural: **the workspace replaces the PID tree as the
unit of lifecycle control.** The ceilings in §5.3 are what `cgroup_scope_argv`
was reaching for, enforced by the platform. `coder stop`/`delete` is more
reliable than killpg plus an escaped-child sweep. Autostop/TTL covers orphans.

**One genuinely new risk with no local analogue:** a dropped connection whose
remote agent keeps running burns credits invisibly — liveness reports dead while
the agent is alive. Needs a remote-side idle guard.

## 8. Non-goals

- **macOS per session.** Structurally impossible on EC2 Mac: Dedicated Host
  billing, one instance per host, a **24-hour minimum allocation**, and a scrub
  window on stop of up to 50 min (x86) / 110 min (Apple silicon). No official
  Coder macOS template exists. If macOS is needed it is a **persistent pool**.
- **A unified devcontainer story across all three OSes** — Coder's dev
  containers are Linux only.
- Live workspace resize; starter templates ship `mutable = false` on size.
- Replacing the Instances feature (`src/kiro_crew/instances/`), which is "my hub
  manages my remote boxes" and stays as-is.
- Memory sync / flow-back (§7.5). Disk reclamation (§2.4).

## 9. Phases

**P0 — prerequisites, independent of Coder.** The 429 classifier (§7.2), CPU
admission control and cgroup-aware `-n auto` (§7.3).

**P1 — deployment target.** A reviewed template that runs Crew in a workspace
with `coder_app` ingress and `KIRO_API_KEY` from a template variable. Delivers
§2.1 and §2.2 with no core refactor.

**P2 — remote session transport.** `SessionHost` with the local path as #1 and
Coder as #2; allowlisted env; `mcpServers: []`. Completes the POC's §5.4 gap.

**P3 — remote MCP** as HTTP + bearer token (§7.4).

**P4 — profiles and heterogeneity.** Per-project execution profiles; Windows.

## 10. Risks

| Risk | Severity | Notes |
|---|---|---|
| Cold start makes sessions feel slow | High | Prebuilds are Premium *and* need `ignore_changes` on `user_data`; warm image measured 385 ms to agent-connect, cold image unmeasured |
| Control plane cannot get small enough | Medium | 3.39 GB anonymous floor; embedding offload is the lever |
| Remote MCP cost | Medium | Client half exists; broker needs a listener and a token |
| One identity throttles the fleet | Medium | Fan-out gives cores, not throughput |
| Path mapping bugs | Medium | Two admission gates fail *silently* (§4.2) |
| Dropped connection leaves a live remote agent | Medium | No local analogue; burns credits invisibly |
| Credentials leaking to a remote host | Medium | Must allowlist env (§7.7) |
| AGPL vs Premium cost | Medium | Prebuilds, quotas, dormancy, audit log all Premium |
| Interactive latency per frame | Low | Measured ~0.3 ms steady-state; same-host caveat |

## 11. Open questions

1. Is the real ceiling local cores or one identity's backend throughput?
2. What does MCP-over-network cost to build?
3. Are gate runs children of the session host (they move) or of the gateway via
   Dev Fleet's build path (they do not)? The Dev Fleet build path uses the
   `build` RLIMIT profile from the gateway, so **those do not move**.
4. Does nested subagent spawning have a depth cap? Unresolved.
