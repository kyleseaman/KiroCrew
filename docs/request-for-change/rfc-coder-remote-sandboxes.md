---
title: Coder-backed remote session sandboxes
status: draft
author: kyleseaman
created: 2026-08-11
last-audited: 2026-08-11
audited-at: 8ee44d84f
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

## Summary — the recommended architecture

**Run the gateway as a long-lived container anywhere, and run every session's
agent process in a Coder workspace instead of as a local child process.**

```
   CONTROL PLANE (long-lived, small)          EXECUTION PLANE (ephemeral, sized)
 ┌───────────────────────────────────┐      ┌──────────────────────────────────┐
 │ kirocrew gateway  (container)     │      │ Coder workspace  "session-a"     │
 │  · owns ALL state:               │◄────►│  · kiro-cli acp                  │
 │    memory, transcripts, artifacts │ ACP  │  · the repo / worktree           │
 │  · is the ACP *client*            │ over │  · the project's toolchain       │
 │  · persistent volume, ~4 GB       │ ssh  │  · its own CPU/RAM ceiling       │
 │  · needs a Coder API token        │      │  · NO Crew state whatsoever      │
 └───────────────────────────────────┘      └──────────────────────────────────┘
        │  creates / stops / deletes                  ┌──────────────────────┐
        └────────────── coderd REST ──────────────────│ workspace "session-b"│
                                                      │  (different image,   │
                                                      │   different size)    │
                                                      └──────────────────────┘
```

Why this shape and not the alternatives:

- **The gateway stays the ACP client**, which it already is. Transcripts, memory
  and usage records never leave the control plane, so the memory flow-back
  problem is *designed out* rather than solved (§7.5).
- **The workspace holds no Crew state**, so there is nothing to merge, nothing to
  sync, and a workspace can be destroyed at any time without loss.
- **Each session gets its own kernel-enforced ceiling and its own image**, which
  is the only way to deliver right-sizing and per-project toolchains at all.
- **Co-locating the gateway with coderd makes the transport cost negligible.**
  Measured per-frame overhead is ~0.3 ms (§5.1); a laptop gateway driving cloud
  sandboxes would instead pay WAN RTT on *every* protocol frame.

Two clarifications that are easy to get wrong:

1. **Deploying the gateway into Coder does NOT give you sandboxes.** A gateway
   running inside a workspace still spawns kiro-cli as a local child *of that
   same workspace*, so every session shares one ceiling and one image. It
   relocates the machine; it does not divide it. The two capabilities are
   independent, and the sandbox half is the part that needs new code.
2. **A gateway inside Coder is viable, but it is a different capability.** The
   gateway does not *have* to be a Coder workspace, and does not have to be one
   to get sandboxes — those are separate questions. Running it as a plain
   long-lived container (Docker, ECS, a k8s Deployment, systemd) is the simplest
   path and is available today. But a workspace-hosted gateway also works, and
   has been built: see §3.1. The lifecycle objection (workspaces are modelled as
   ephemeral dev environments — stop/start, TTL, dormancy, auto-delete) is
   answered by a persistent volume for the data home, which is exactly what that
   template does. What a workspace-hosted gateway does *not* buy you is the
   sandbox half, per clarification 1. **Either host works for the control plane;
   workspaces are what make sandboxes.**

What this costs, honestly: the gateway container is **stateful** (persistent
volume for the data home) with a ~4 GB floor until the embedding stack is
offloaded (§7.1); it needs a **Coder API token** with workspace-create rights;
and because every session becomes remote, **remote MCP stops being optional**
(§7.4) — without it a session loses the whole KiroCrew capability layer.

**Half of this already ships.** `docker/Dockerfile` is a working gateway image
(`EXPOSE 5476`, healthcheck on `/api/health`, `CMD gateway`), and
`docs/guides/docker.md` already recommends it for 24/7 use. The new work is the
session backend, not the packaging.

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

### 3.1 Prior art: a gateway-in-workspace template already exists

`greg-the-coder/partner-demo-gitops`, template `kiro-crew-prototype-a` (pushed
2026-08-11), runs the **entire Kiro Crew gateway inside one Coder workspace** on
Kubernetes. A sibling `awshp-k8s-base-kirocrew` template exists in the same repo.
Read at 355 lines of `main.tf` plus a 199-line startup script and a self-contained
`modules/kirocrew` (232 + 255 lines). Not executed here, so the notes below are
from source, not from a run.

It installs kiro-cli and Crew from the public installers
(`cli.kiro.dev/install`, `download.crew.kiro.dev/cli.sh`), starts
`kirocrew gateway` as a `coder_script`, and publishes the dashboard as a
subdomain `coder_app` with a healthcheck. A PVC at `/home/coder` (10–50 GiB)
persists the data home; CPU 2–8 and memory 4–16 GiB come from
`coder_parameter` values applied as Kubernetes `limits`.

**How it relates to this RFC.** It delivers the *control-plane* half — §2.1
(deploy on customer infrastructure behind Coder's ingress/egress controls) and
§2.2 (always-on Crew) — and it is further along on dashboard exposure than
anything here. It does **not** deliver §2.3 (per-session right-sizing and
per-project image), because each session's kiro-cli is still a local child of
that one workspace, sharing its ceiling and its image. That is clarification 1 in
the Summary, demonstrated. The two compose: that template is a *host* for the
work in §4.2, and a Coder-native one, which makes it a better target than the
Docker harness in `docker/coder/`.

**Findings worth reusing, all of them traps this RFC otherwise misses:**

- **The dashboard needs the Coder proxy host trusted explicitly.** The template
  sets `KIROCREW_CORS_ORIGINS` and runs `config set dashboard.url`, or the
  gateway answers "Host header not allowed." through Coder's proxy, and token
  cookies are scoped to the wrong origin.
- **`unset PYTHONPATH PYTHONHOME` before installing.** Inherited from the Coder
  agent environment, pip treats foreign packages as already satisfied and
  silently skips Crew's dependencies, producing a venv that fails at first
  gateway start. This is the same hazard `wrap_argv(strip_python_env=True)`
  guards against for MCP subprocesses (§4.2), reached from the opposite
  direction.
- **Self-authenticating app tile.** A loopback-bound (`127.0.0.1`) redirector
  mints a short-lived dashboard token and 302s the browser to the app URL, so the
  tile lands authenticated. `share` defaults to `owner`.
- **`coder stat cpu|mem|disk` as `coder_agent` metadata** surfaces live usage on
  the workspace page — directly useful given the CPU asymmetry in §7.3.



## 4. Architecture

### 4.1 The seam decision

**Recommendation: option C — the gateway keeps the ACP client role and the agent
process moves into a Coder workspace.** This is the "two-plane" model in the
Summary. The alternatives are recorded because each looks reasonable until you
follow it through.

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

### 4.2 What must change inside Crew

Anchors verified at `8ee44d84f`. Ordered by dependency. W1 was originally planned
as one PR with W2; it shipped separately, because a pure extraction can be proven
behaviour-neutral by the existing tests while W2 cannot.

**W0 — what does NOT change.** Worth stating, because it bounds the work.
The JSON-RPC layer is untouched (§5.2 proved a handshake with zero protocol
changes). Gateway packaging is untouched (`docker/Dockerfile` already ships).
Subagents need no separate work — they multiplex onto the parent's runtime
rather than spawning their own process, so covering `AcpRuntime` covers them.
`create_subprocess_limited` (`src/kiro_crew/sandbox.py:4152`) needs no change
either: it forwards all kwargs untouched and returns a real
`asyncio.subprocess.Process`, and a remote session is *still a local
subprocess* (`coder ssh`), so the return contract already fits.

**W1 — the `SessionHost` seam.** Two call sites build argv, wrap it, and launch:

| Step | `AcpClient` | `AcpRuntime` |
|---|---|---|
| method | `_spawn`, `acp/client.py:2282` | `spawn`, `acp/runtime.py:647` |
| sandbox wrap | `:2344` `wrap_argv(...)` | `:686` |
| cgroup wrap | `:2354` `cgroup_scope_argv(argv)` | `:696` |
| launch | `:2424` | `:723` |

Introduce a `SessionHost` with two implementations — `LocalSessionHost` (today's
behaviour, verbatim) and `CoderWorkspaceSessionHost` (builds
`coder ssh <ws> -- env … kiro-cli acp …`). The remote implementation **skips
`wrap_argv` and `cgroup_scope_argv`**: the workspace is the isolation boundary
and its cgroup ceiling replaces them (§5.3). Skipping them must be gated on
"the host is remote", not on a config flag, so the local fail-closed behaviour
cannot be disabled by accident.

Note the two paths do **not** share a transport model and must not be collapsed:
`AcpClient` is pull-based with no reader task (`_read_message`,
`acp/client.py:3120`), while `AcpRuntime` owns an exclusive `_reader_loop`
(`acp/runtime.py:959`). `SessionHost` abstracts *spawning*, not *reading*.

**W2 — env becomes an allowlist (security, ships with W1).** Both paths do
`env = {**os.environ}` (`client.py:2358`, `runtime.py:698`) and then call
`scrub_agent_denied_env` (`sandbox.py:3322`), which covers channel tokens only —
**not** the AWS prefixes handled by `scrub_env` (`sandbox.py:3300`). Locally that
is deliberate; shipping it to another host is not (§7.7). Add an allowlist path
used only by remote hosts. Drop by default; carry only what the remote agent
needs (`KIRO_API_KEY`, `KIROCREW_SESSION_KEY`, model/agent selection). Explicitly
exclude every host-local path and socket: `PATH`, `SSH_AUTH_SOCK`, `KRB5CCNAME`,
`PYTHONPATH`, `PYTHONHOME`, `KIROCREW_HOME`, `KIROCREW_PROJECT_DIR`,
`KIROCREW_WORKSPACE`, `KIRO_HOME`, `KIROCREW_KIRO_BIN`, `KIROCREW_MCP_TARGET_*`,
`TMPDIR`, `XDG_RUNTIME_DIR`, `DBUS_SESSION_BUS_ADDRESS`.

**W3 — two cwds.** The gateway currently creates the work dir and passes it as
the process cwd: `_work_dir.mkdir(...)` at `client.py:2291`, again at
`client.py:2998` on *every* `ensure_ready()`, and `runtime.py:652`; then
`cwd=str(self._work_dir)` at `client.py:2429` / `runtime.py:728`. For a remote
host these are two different values — the local spawn cwd is irrelevant, and the
**protocol** cwd must be the remote path. The protocol side is already
permissive: cwd crosses ACP as a bare `str()` with no validation
(`acp/_dispatch.py`), used in `session/new` (`client.py:2763`) and
`session/load` (`client.py:2878`), plus `runtime.py:1408` / `:1521`.

Two hazards. First, `providers/acp.py` writes `<work_dir>/.kiro/settings/cli.json`
before every (re)spawn (`_write_cli_overlay` and the Tool Search overlay) — those
writes must either move into the workspace or be delivered another way, or the
reasoning-effort and Tool Search toggles silently stop applying remotely.
Second, admission gates reject a non-local path, and **two fail silently**: a
stored cwd that is not a local dir is dropped on resume (`session.py`), and
`default_project_dir()` degrades to `""` (`config/loader.py`). Those need to
learn about remote paths or be bypassed for remote sessions.

**W4 — lifecycle moves from PID tree to workspace.** Everything after launch
assumes a local child: `_track_pid` / `_track_session_pid`
(`session_pid.py:877` / `:151`), `_get_child_pids` (`client.py:1490`), the
killpg escalation, the `KIROCREW_SPAWNED` orphan sweep, the `LivenessOracle`
(`acp/liveness.py`), and the RSS watchdog. Over SSH these all describe the ssh
client (§7.8). Replace them for remote hosts with workspace-level operations:
`coder stop` / `coder delete` as the kill primitive, workspace status as
liveness, and the workspace's own ceiling instead of `cgroup_scope_argv`. Keep
`is_responsive` — it keys on stdio `_last_activity`, which still measures
end-to-end liveness. **Add a remote-side idle guard**: a dropped connection
whose agent keeps running burns credits with no local signal.

**W5 — a per-session execution profile.** None exists. `agent.sandbox`
(`config/loader.py:878`) is a global `["auto","off"]` enum, with
`sandbox_allow_no_isolation` (`:893`) and `sandbox_allow_unsandboxed_exec`
(`:904`) alongside it. Add a per-project/per-session profile that names the host
kind plus, for Coder, the template and its `coder_parameter` values (size,
image). This is what turns "sandboxes work" into "right-sizing works".
**Designed in full in §4.4** — config shape, settings panel, per-session picker,
and default resolution.

**W6 — remote MCP (gates making remote the default).** `mcp_gateway/transport.py`
serves `AF_UNIX` only (`serve`, `:412`); there is no `AF_INET` in the package,
and the auth model is peer-credential based, which does not survive leaving the
host (§7.4). Add an HTTP listener plus a bearer token, and emit `url`-style
entries from `pooled_session_servers`
(`mcp_gateway/session_servers.py:149`) instead of stdio stub commands. The client
half already exists — kiro-cli advertises `mcpCapabilities.http: true` (§5.2) and
KiroCrew already emits HTTP MCP entries for apps.

**W7 — independent prerequisites.** Neither is Coder-specific.
`_RE_5XX_STATUS` (`acp/client.py:890`) matches only `50[0234]|529`, and
`_RE_THROTTLE_NAMED` (`:878`) keys on exception names, so a bare `HTTP 429`
falls through `_is_transient_raw_error` (`:1012`) and is treated as terminal.
Add 429 and honour `Retry-After`. Separately, `inject_xdist_auto_cap`
(`resource_status.py:279`) caps `-n auto` by memory but reads host CPU count;
make it cgroup-aware so a right-sized workspace sizes its own test runs.

**W8 — getting bytes in and out.** Three smaller pieces, none hard, all easy to
forget until a session fails oddly.

*Repo delivery.* The workspace needs the code. Either a `coder_parameter` for
repo URL + ref with a clone in the startup script, or rsync over
`coder config-ssh`. There is no `coder cp`.

*Artifact and outbox return.* `outbox_dir()` (`config/loader.py:428`) and the
`file_send` path write the **gateway's** disk. A remote agent producing a file
has to get it back across the boundary, or `file_send` silently delivers
nothing. Simplest: have the remote write to a known path and pull it over the
same SSH channel after the turn.

*Preview ports.* Local dev servers are assumed to be on gateway loopback, which
is where the `web-preview` marker points the Browser panel. For a remote session
that becomes `coder port-forward`, and the marker needs the forwarded local port
rather than the workspace's.

**Smallest first PR:** W1 + W2 behind a config flag, remote MCP off
(`mcpServers: []`), one session, `LocalSessionHost` as the default so nothing
changes for existing users. That is enough to close §5.4.

### 4.3 Auth

Use **`KIRO_API_KEY`**, the documented portable credential, injected as a
workspace env var from the template. Verified in the POC (§5.2). Note it does
**not appear anywhere in this repo today** — nothing plumbs it.

Gotcha: `KIRO_HOME` does **not** relocate credentials; they resolve from
`HOME` / `XDG_DATA_HOME` / `LOCALAPPDATA`.

### 4.4 Sandbox profiles: configuration and selection

The execution plane needs a user-facing surface. The proposal: **a library of
named sandbox profiles, and a per-session picker that chooses one.** Each profile
is a named binding of a template plus its parameter values; a session runs on
exactly one profile (or locally). This is deliberately *not* multiple sandboxes
per session — see the non-goal at the end.

#### Credentials: store nothing, delegate to the `coder` CLI

This follows the rule `src/kiro_crew/cloud/__init__.py` already states for AWS —
*"Bring-your-own-AWS, store nothing. Every AWS call shells to the `aws` CLI …
so credential resolution stays in the CLI's own provider chain. KiroCrew persists
only a profile name, region, and stack name."*

Apply it verbatim to Coder: shell every call to the `coder` CLI, and persist only
coordinates. The operator runs `coder login <url>` once on the control plane; the
CLI owns its own session store, and KiroCrew never reads, writes, or holds the
token. `coder whoami` is then the read-only verification probe, exactly as
`aws sts get-caller-identity` is for `cloud/` and `kiro-cli whoami` is for
`kiro_prerequisite`.

This is not merely tidy — it sidesteps a real constraint. The credential file is
a **fixed allowlist** (`load_credentials` iterates `_CREDENTIAL_KEYS`), not a
general dotenv, so a Coder token could not be dropped into `.env` without
extending that list. Delegation avoids the question entirely, and matches the
`instances/` registry, which likewise stores *"only connection coordinates"* and
mints credentials at connect time.

The one credential KiroCrew *does* have to convey is `KIRO_API_KEY`, for the agent
inside the workspace. That is supplied as a template variable (`sensitive = true`)
rather than a `coder_parameter` — parameter values are persisted in coderd's
database and readable back through its API.

#### Config: `sandbox_profiles.json`

Mirror the v2 shape `src/kiro_crew/deploy/profiles.py` already uses —
`{"version": 2, "profiles": [...], "default": "<name>"}` — so the loader, the
default-resolution helper and the "unconfigured" state all have a working
precedent to copy.

```json
{
  "version": 2,
  "default": "local",
  "profiles": [
    { "name": "local", "host": "local" },
    { "name": "linux-small", "host": "coder",
      "coder_url": "https://coder.example.com",
      "template": "kirocrew-session",
      "parameters": { "instance_size": "small" } },
    { "name": "linux-build", "host": "coder",
      "coder_url": "https://coder.example.com",
      "template": "kirocrew-session",
      "parameters": { "instance_size": "build" } },
    { "name": "windows-dotnet", "host": "coder",
      "coder_url": "https://coder.example.com",
      "template": "kirocrew-session-windows",
      "parameters": { "instance_size": "medium" } }
  ]
}
```

No secret appears in that file. `coder_url` is a coordinate; authentication is
whatever `coder login` established for it.

A feature flag `coder.enabled` (default `false`) gates the whole surface, matching
how `instances.enabled` gates the Instances feature.

#### Settings UI

A `SandboxesPanel.tsx` in `website/src/pages/settings/`, alongside the existing
`InstancesPanel.tsx` — which is the closest analogue, being the panel that manages
named remote targets. Two parts:

1. **Connection** — the coderd URL, current identity from `coder whoami`, and a
   Verify button. When not logged in, show the exact `coder login <url>` command
   for the operator to run rather than collecting a token in the UI.
2. **Profiles** — list, add, edit, delete, and mark one default. Template and
   parameter choices should be populated from coderd (`GET /api/v2/templates`
   and the template's `coder_parameter` definitions) rather than free-typed, so a
   profile cannot name a template or size that does not exist.

#### Per-session selection

A slot field plus an endpoint mirroring `api_chat_slot_model`
(`src/kiro_crew/dashboard/chat_handlers.py:2688`), surfaced as a dropdown in the
chat header next to the model picker. New sessions inherit the resolved default;
the picker overrides it for that session.

**One asymmetry to design around.** The model picker's docstring notes it
*"prefers an in-place `session/set_model` on the running session and only resets
when that is impossible."* A sandbox picker has **no in-place equivalent** — a
live `kiro-cli` process cannot migrate between hosts, so changing profile means
terminating the process and starting a new one elsewhere.

That is survivable rather than destructive, because of the two-plane split: the
gateway owns the transcript, and the POC confirmed the agent advertises
`loadSession: true` (§5.2). So a mid-session switch is the *same machinery as a
process restart* — tear down, spawn on the new host, resume. The decision to make
explicitly is whether the picker is offered mid-session behind a "this restarts
the agent" affordance, or only at session start. Recommendation: offer it
mid-session, since restart-and-resume already exists and the alternative is a
worse experience (start a new session and lose the thread).

#### Default resolution

Resolve in this order, mirroring how the deploy skill resolves its AWS profile:

1. An explicit per-session pick.
2. A per-project default (so "this repo always builds in the 8-CPU sandbox" is
   configured once).
3. The global `default` from `sandbox_profiles.json`.
4. `local` — which is also the behaviour when `coder.enabled` is false, so
   nothing changes for users who never configure it.

Step 2 is the ergonomic win: the common case should require no picking at all.

#### Non-goal: multiple sandboxes per session

One session runs on one profile. Fanning a single session across several
workspaces would mean breaking subagent runtime multiplexing (§4.2 W0) and
multiplying cold starts, for a use case — heterogeneous verification within one
session — better served later by an auxiliary exec target than by several agent
hosts.

## 5. Proof of concept — measured results

Harness: `docker/coder/`. coderd v2.36.0 on loopback with telemetry disabled, a
1.01 GB workspace image carrying kiro-cli, a template with a size parameter, two
live workspaces.

**Which plane this exercised.** The POC ran the gateway **on the host** and the
agent process **in a workspace** — i.e. exactly the execution plane of the
recommended architecture. The control plane was not containerised during the
POC, because that half already ships (`docker/Dockerfile`); the workspace image
deliberately contains **only kiro-cli**, no KiroCrew. So the results below speak
to the session backend, not to gateway packaging.

### 5.1 The transport works, and is nearly free per frame

`coder ssh <ws> <cmd>` gives the gateway a **local subprocess whose stdin/stdout
are the remote process's stdio** — exactly the shape `AcpClient`/`AcpRuntime`
already consume. JSON frames round-trip bidirectionally.

Across three runs:

| Path | Connect (first frame) | Steady-state median |
|---|---|---|
| `coder ssh <ws> cat` | 155–410 ms | **0.29–0.57 ms** |
| local `cat` (control) | ~0.3 ms | ~0.04 ms |

Cost is a **one-time connection setup of a few hundred milliseconds**, after
which per-frame overhead is sub-millisecond. Caveat: the workspace is a container
on the same host, so per-frame cost on a genuinely remote box will be dominated
by real RTT. What this establishes is that the *protocol layer* adds nothing.

Re-derive any number in this section with `python3 docker/coder/verify.py`.

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

**Not a phase — gateway packaging.** `docker/Dockerfile` already builds a working
gateway image and `docs/guides/docker.md` already recommends it for 24/7 use.
Running the control plane as a long-lived container is a *deployment choice
available today*, not work. It needs a persistent volume for the data home and a
Coder API token. Hosting it inside a Coder workspace instead is also a deployment
choice, and one already built (§3.1) — neither host changes the work below.

**P0 — prerequisites.** The `HTTP 429` misclassification (§7.2) is a hard
requirement: it is rare at today's concurrency and routine once sessions fan out,
and it currently kills a turn instead of retrying. Design the **env allowlist**
(§7.7) in the same pass, since a remote spawn must not inherit the host's cloud
credentials. CPU governance and cgroup-aware `-n auto` (§7.3) matter *less* under
this architecture — agent-triggered work leaves the control plane entirely — but
still apply to gateway-side Dev Fleet builds, which do not move.

**P1 — remote session transport.** The core of the proposal: a `SessionHost`
seam with the local path as implementation #1 and a Coder workspace as #2,
allowlisted env, and `mcpServers: []`. Completes the gap in §5.4. The POC already
validated the transport, the auth mechanism and the ceilings this phase depends
on, so P1 is implementation rather than discovery.

**P2 — remote MCP** as HTTP + a bearer token (§7.4). **This gates making remote
the default.** With `mcpServers: []` a session keeps only the kiro-cli built-ins
and loses the entire KiroCrew capability layer — `spawn_run`, every `cron_*`
tool, `artifact_*`, `learn_add`, `send_message`. Acceptable while proving a
transport; a serious regression as a steady state.

**P3 — profiles and heterogeneity.** The sandbox-profile library, settings panel
and per-session picker as specified in §4.4; then Windows images. macOS stays a
non-goal (§8).

## 10. Risks

| Risk | Severity | Notes |
|---|---|---|
| **Remote MCP is a prerequisite, not an enhancement** | **High** | Under this architecture every session is remote, so `mcpServers: []` would permanently cost the KiroCrew capability layer. Client half exists; the broker needs a TCP listener and a token (§7.4, P2) |
| Cold start makes sessions feel slow | High | Prebuilds are Premium *and* need `ignore_changes` on `user_data`; warm image measured 385 ms to agent-connect, cold image unmeasured |
| Control plane cannot get small enough | Medium | 3.39 GB anonymous floor; embedding offload is the lever (§7.1) |
| Control plane is stateful | Medium | The gateway container owns memory, transcripts and artifacts, so it needs a persistent volume and a backup story. Losing the container is not a no-op |
| Gateway needs a Coder API token | Medium | Workspace-create rights, held inside the control-plane container — a second credential-in-a-container alongside `KIRO_API_KEY` |
| One identity throttles the fleet | Medium | Fan-out gives cores, not throughput (§7.6) |
| Path mapping bugs | Medium | Two admission gates fail *silently* (§4.2) |
| Dropped connection leaves a live remote agent | Medium | No local analogue; burns credits invisibly (§7.8) |
| Credentials leaking to a remote host | Medium | Must allowlist env (§7.7) |
| AGPL vs Premium cost | Medium | Prebuilds, quotas, dormancy, audit log all Premium |
| Interactive latency per frame | Low | Measured ~0.3 ms steady-state. **Co-locating the gateway with coderd keeps it there**; a laptop gateway driving cloud sandboxes would instead pay WAN RTT per frame |

## 11. Open questions

1. Is the real ceiling local cores or one identity's backend throughput?
2. What does MCP-over-network cost to build?
3. Are gate runs children of the session host (they move) or of the gateway via
   Dev Fleet's build path (they do not)? The Dev Fleet build path uses the
   `build` RLIMIT profile from the gateway, so **those do not move**.
4. Does nested subagent spawning have a depth cap? Unresolved.
