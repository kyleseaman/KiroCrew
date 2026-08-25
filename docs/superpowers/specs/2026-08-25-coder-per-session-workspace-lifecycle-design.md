# Coder Per-Session Workspace Lifecycle

**Status:** Approved; implementation in progress  
**Date:** 2026-08-25

## Decision

Each durable Kiro Crew parent session owns one Coder workspace. Every subagent
and dedicated ACP runtime descended from that parent executes in the same
workspace. No two unrelated parent sessions share a workspace.

The gateway remains the only always-on Kiro Crew control plane. It owns session
history, memory, MCP and OAuth credentials, cron, policy, the Coder automation
token, workspace bindings, and lifecycle reconciliation. Coder workspaces own
only session compute, the remote Kiro transcript, and that session's persistent
filesystem.

```text
Gateway (small, always on)
  |
  +-- session A binding --> crew-4m6p2j8r --> running only while active
  |                           +-- parent ACP runtime
  |                           +-- all A subagents
  |                           +-- A filesystem and transcript
  |
  +-- session B binding --> crew-v9q3k7dw --> stopped, disk retained
  |
  +-- session C binding --> absent after 30 inactive days
```

Workspaces target a 30-minute inactivity window through Coder's activity-aware
autostop by default. A stopped workspace keeps its disk and restarts when its
session resumes. A gateway reconciler deletes a stopped workspace after 30 days
of inactivity by default. Resuming after deletion creates a fresh workspace
while retaining gateway history and memory.

This supersedes the static POC assumption that every remote session uses the
configured `crew-dogfood` workspace. That workspace remains a useful deployment
smoke target, but it is not the target runtime topology.

## Why the Static Workspace Is Insufficient

A shared workspace violates the intended boundary in three ways:

- unrelated sessions share files and process visibility;
- one instance must be sized for aggregate peak concurrency;
- keeping any session available keeps the entire shared compute node running.

Per-session workspaces make isolation and cost scale together. An active
session pays for one instance; an inactive resumable session pays only for its
persistent resources; an expired session pays nothing for Coder resources.

## Goals

- Give each durable parent session its own Coder workspace and filesystem.
- Reuse the same workspace when that session resumes.
- Keep every descendant subagent in its parent's workspace.
- Start compute lazily and use Coder's connection-aware autostop.
- Protect long agent turns, builds, and descendant processes from idle stop.
- Delete stopped workspaces after 30 inactive days by default.
- Keep history, memory, MCP, OAuth, policy, and lifecycle authority on the
  gateway.
- Fail closed: lifecycle failures never fall back to local ACP execution or a
  sibling session's workspace.
- Bound simultaneous workspace starts and running workspaces for cost safety.
- Preserve the existing local-session path when Coder hosting is disabled.

## Non-Goals

- Moving the gateway or its durable stores into a workspace.
- One workspace per subagent. Subagents share their parent session's boundary.
- Sharing a workspace pool between unrelated sessions.
- Cloning a parent workspace filesystem when a chat is forked.
- Treating arbitrary CPU use or arbitrary process names as proof of activity.
- Protecting a manually detached terminal job that deliberately escapes the
  managed workload scope after every Coder connection closes.
- Using Coder Premium dormancy. The gateway implements retention so the OSS
  deployment has the same lifecycle.
- Automatically deleting the existing operator-created `crew-dogfood`
  workspace during migration.

## Terminology and Ownership

### Parent session tree

A parent session tree is one user-visible durable session plus all of its
descendants. Dashboard, channel, and cron surfaces must resolve a durable parent
identity before requesting a workspace. `_bg`, heartbeat, consolidation,
discovery, and unrelated gateway maintenance remain local.

A cron firing owns a workspace through the durable session/run it creates. A
retry of that run reuses the binding; a distinct run creates a distinct parent
binding. A chat fork is a new parent and receives a fresh workspace.

### Workspace binding

A `WorkspaceBinding` is durable, non-secret gateway state:

- opaque binding id;
- workspace UUID and safe generated name;
- Coder owner, organization, template, preset, and generation;
- creation, last-activity, stop, deletion-deadline, and deletion timestamps;
- last observed Coder transition and bounded failure code.

The user-visible session metadata stores only the opaque binding id and a
filesystem-reset generation. The authoritative binding registry lives at
`config_dir()/coder_workspaces.json`, is written atomically, and is added to the
Crew data-home keystone deny list because changing its workspace UUID could
redirect a destructive stop or delete. The Coder token remains separately
encrypted in `SecretVault`.

Workspace names contain no title, prompt, channel id, email, repository, or raw
session key. They use the configured safe prefix plus an encoded random binding
id, for example `crew-4m6p2j8r`. All destructive operations use the persisted
Coder UUID and revalidate owner and binding name; a prefix match alone never
authorizes deletion.

### Managed workload lease

A managed workload lease means the workspace still owns useful work. Lease
holders include:

- a parent turn in flight;
- a shared or dedicated subagent runtime;
- an ACP initialization, resume, compaction, or teardown in progress;
- a tracked descendant remaining in the session workload scope;
- a future explicit gateway-managed background job.

Leases are gateway-owned, reference-counted, and keyed by binding id. They are
not serialized into the workspace and do not grant Coder authority.

## Configuration and Settings

The Settings panel describes how to create session workspaces, not the name of
one shared workspace:

| Setting | Default | Meaning |
|---|---:|---|
| `session.coder.enabled` | `false` after first save | Use managed Coder workspaces for new parent sessions |
| `session.coder.url` | empty | Trusted Coder base URL |
| `session.coder.template` | `kirocrew-arm` in the sample | Template used for newly created workspaces |
| `session.coder.preset` | empty | Optional Coder template preset; empty uses template defaults |
| `session.coder.remote_cwd` | `/home/coder/workspace` | Working directory within every workspace |
| `session.coder.runtime_warm_minutes` | `5` | Keep an idle remote ACP runtime warm before closing its SSH connection |
| `session.coder.stop_after_minutes` | `30` | Coder activity-aware autostop duration |
| `session.coder.delete_after_days` | `30` | Retention after meaningful activity |
| `session.coder.max_running` | `3` | Cost guard for concurrently running managed workspaces |
| `session.coder.workspace_prefix` | `crew` | Safe operator-visible name prefix |

The template or preset owns provider-specific choices such as AWS region,
instance type, disk size, subnet, and instance profile. Kiro Crew does not grow
AWS-specific settings in its generic Coder panel.

The connection test verifies URL and token authentication, current owner,
template and preset resolution, and Coder CLI availability for SSH. It does not
create billable compute and therefore cannot claim that the runtime template
contract works. A separate explicit smoke action creates and deletes a test
workspace when the operator wants an end-to-end provisioning and contract test.

Changing template, preset, stop duration, or prefix affects new workspaces.
Existing bindings keep the values recorded at creation until an explicit
workspace update flow is added. Changing the Coder URL or token refreshes the
manager for future operations but never silently rebinds an existing UUID from
another deployment.

## Workspace Template Contract

A managed template must provide:

- Linux, Python 3, `kiro-cli`, and the Coder agent;
- an owner-only, traversable remote working directory;
- persistent storage across stop/start;
- no Coder automation token, gateway credentials, AWS credentials, or MCP
  credentials in the workspace;
- a functioning systemd user manager and transient user scopes for the
  conservative background-work guarantee;
- outbound connectivity required by the coding workload and Kiro service.

The sample AWS template enables lingering for `coder`, exposes the user D-Bus
runtime needed by `systemd-run --user`, and verifies a transient scope during
bootstrap. The non-billable Settings probe verifies authentication and template
visibility; the explicit provisioning smoke is what verifies this runtime
contract before an operator relies on detached-build safety.

This requirement is limited to managed Coder hosting. Local Kiro Crew remains
cross-platform, and the legacy static Coder transport may continue without the
lifecycle guarantee during migration.

## Lifecycle State Machine

The lifecycle coordinator is a gateway component distinct from the ACP
transport host. It owns Coder API/CLI operations, durable bindings, leases,
retention, and reconciliation. `CoderWorkspaceSessionHost` continues to own SSH,
remote preparation, reverse forwards, and credential-free relays after the
coordinator supplies a ready workspace.

```text
unbound
   | allocate binding
   v
creating --> starting --> ready --> running work
   |            |          |            |
   | failure    | failure  | lost       | leases/connections end
   v            v          v            v
failed <--------------------------- autostop pending
                                         |
                                         v
                                      stopped
                                      /     \
                             resume /         \ 30 inactive days
                                  v             v
                              starting       deleting --> deleted
```

### Allocate

The first execution of a durable parent allocates and persists an opaque binding
before making a Coder request. A per-binding async lock and cross-process file
lock make allocation idempotent. Concurrent messages, restored slots, or cron
retries either await the same operation or reuse its completed result.

Subagents never allocate. They receive the parent's live or persisted binding
through the existing execution-affinity seam.

### Create or start

Under the binding lock, the coordinator:

1. fetches the bound workspace by UUID when one exists;
2. verifies Coder deployment, owner, generated name, and recorded template;
3. starts a stopped workspace or creates an absent generation from the
   configured template/preset;
4. sets the workspace's 30-minute default stop duration;
5. watches the Coder build and agent connection to a bounded ready state;
6. probes the remote working directory and workload-scope contract;
7. returns a concrete `CoderWorkspaceSessionHost` to the ACP runtime.

Workspace provisioning is asynchronous. The session UI reports creating,
starting, waiting for agent, or failed instead of looking like a stalled model
turn. A bounded global semaphore limits simultaneous create/start builds.

At most `max_running` managed workspaces may be running or starting. A new
interactive request receives a clear capacity error naming the stopped/running
counts; a cron run remains queued with a retryable capacity reason. The gateway
does not stop another session implicitly to make room.

### Active work and builds

The gateway starts each remote ACP runtime inside a transient systemd user scope
named from the binding and runtime ids. Shell tools, builds, and ordinary
background descendants inherit that cgroup. Forking, `setsid`, and `nohup` do
not move a process out of its cgroup.

The existing session semaphore remains the authoritative in-flight-turn signal:
the idle sweep cannot reap a locked session. The Coder SSH connection remains
open for the ACP runtime and is activity Coder already understands. Subagent and
runtime leases cover dedicated descendants and lifecycle work.

When the turn queue and subagent tree become idle, the gateway keeps the remote
ACP runtime warm for `runtime_warm_minutes`, then closes its SSH connection. This
is deliberately shorter than the workspace autostop window: leaving an idle ACP
SSH session open would make Coder correctly classify the workspace as active
forever. If no descendants remain, the transient scope becomes inactive. If a
build descendant remains, the scope stays active after the ACP parent exits. The
lifecycle reconciler observes that exact managed scope and posts a bounded Coder
usage heartbeat until it empties. It does not infer activity from host CPU,
process names, or the permanent Coder agent. The heartbeat interval is bounded
below one third of the configured autostop duration, so one delayed sweep cannot
consume the entire safety margin.

An unprivileged process cannot move into another cgroup without explicitly
starting a different user unit. A command that deliberately escapes the managed
scope is outside the automatic guarantee. A later gateway-managed background
job tool may create its own lease-bearing scope; no natural-language or shell
regex is used to guess that intent.

### Stop

Coder's native autostop is the stop authority because it knows about active
VS Code, JetBrains, terminal, SSH, and reported AI-task sessions. The gateway
does not issue a blind stop based only on chat timestamps.

While a managed workload scope is active without an ACP SSH connection, the
gateway renews Coder activity. Once managed leases/scopes end, it stops renewing.
Coder then waits the configured activity duration and stops the workspace only
when its own connection checks also consider it inactive. The sample template's
activity bump is configured to the same 30-minute default. Actual shutdown may
trail the last activity by Coder's bounded scheduler granularity; the setting is
an inactivity target, not a wall-clock kill deadline.

Stopping destroys ephemeral compute according to the template but preserves
persistent resources. A stop does not close gateway history, memory, or the
binding. Resume starts the same UUID and filesystem.

### Thirty-day deletion

A gateway reconciler periodically lists only registry-owned bindings and reads
their current Coder records. Effective last activity is the maximum of:

- gateway session/lease activity;
- the registry's last successful use;
- Coder's `last_used_at`, which covers operator IDE, terminal, and SSH use.

The default deletion deadline is effective last activity plus 30 days. A
workspace is eligible only when it is stopped, has no lease or active managed
scope, is still the exact UUID/owner/name recorded by the binding, and remains
eligible after reacquiring its binding lock and refetching Coder state.

Deletion is two-phase: persist `delete_pending`, refetch and revalidate, request
Coder deletion, watch the Terraform destroy build, then persist `deleted`.
Failure leaves the binding retryable and never removes the sole record of a
possibly billable resource.

Permanent deletion of a Kiro Crew history session requests immediate workspace
deletion through the same two-phase path. If that attempt fails, the registry
retains an orphan-cleanup record. Closing or archiving a session is not
permanent deletion and uses the normal 30-day policy.

On later resume, a deleted binding creates a new generation with a fresh Coder
UUID. The gateway history and memory remain, but native remote transcript and
workspace files are gone. The chat receives one explicit filesystem-reset
notice before the new turn.

## Coder Integration Boundary

Lifecycle state uses Coder's authenticated, structured API for workspace,
build, agent, and `last_used_at` records. The token is attached only as the
Coder session header by the gateway client. Requests have fixed origins,
timeouts, response limits, no cross-origin credential redirects, and redacted
errors.

The pinned Coder CLI remains the transport implementation for `coder ssh` and
may be used for creation only where the REST surface cannot safely express a
template parameter. Tokens are environment-only, never argv. CLI output is not
the authoritative lifecycle database; every mutation is resolved back to a
structured Coder record before the binding is updated.

Coder's activity-aware autostop is available in the OSS lifecycle. Native
dormancy and automatic deletion are Premium features, so the 30-day reconciler
is intentionally gateway-owned. See Coder's
[workspace scheduling](https://coder.com/docs/user-guides/workspace-scheduling)
and [workspace lifecycle](https://coder.com/docs/user-guides/workspace-lifecycle)
contracts.

## Startup Reconciliation

On gateway startup, the coordinator loads the registry and reconciles every
non-deleted binding with Coder under bounded concurrency:

- missing workspace: mark the generation absent; recreate only on resume;
- stopped workspace: retain it and recompute deletion eligibility;
- running workspace with no reconstructed lease: stop renewing activity and let
  Coder autostop; do not kill a human connection;
- pending create/start/stop/delete build: resume watching it;
- failed build: retain a bounded failure and expose repair/delete actions;
- workspace identity mismatch: quarantine the binding and perform no mutation.

A corrupt or unreadable registry fails closed for destructive operations.
Sessions may report lifecycle state unavailable, but the gateway never guesses
ownership from a prefix and never sweeps all Coder workspaces.

## Failure and Race Handling

- **Concurrent first turns:** one binding and one create build; all callers
  await the same result.
- **Resume during autostop:** the binding lock serializes start against local
  lifecycle bookkeeping; Coder's build state decides the transition.
- **Resume during deletion eligibility:** renewed activity is persisted before
  the lock is released, invalidating deletion on its mandatory refetch.
- **Resume after delete request:** wait for destroy completion, create the next
  generation, and report filesystem reset.
- **Gateway loss during a mutation:** startup reconciliation resumes from the
  persisted intent plus Coder's actual latest build.
- **Coder unavailable:** fail the session start with a retryable host error; do
  not run locally and do not allocate another workspace name.
- **Template failure:** keep the failed workspace visible with repair/delete
  actions; bounded retries never create sibling resources silently.
- **Workspace manually stopped:** the next session action starts the same
  workspace.
- **Workspace manually deleted:** mark the generation deleted and create a
  fresh one only when the session runs again.
- **Token or URL changed:** existing UUIDs are quarantined until the new Coder
  deployment proves the same owner and workspace identity.
- **Capacity reached:** interactive start fails clearly; cron waits and retries;
  no active session is preempted automatically.

## Security Properties

1. **One session tree, one workspace.** An unrelated parent can neither select
   nor inherit another binding.
2. **No workspace Coder authority.** The automation token remains in the
   gateway vault and transport environment only.
3. **Integrity-protected destructive targets.** The binding registry is a
   keystone path, and stop/delete revalidate UUID, owner, and generated name.
4. **No local fallback.** Create, start, probe, or resume failure never moves
   the parent or descendants onto the gateway host.
5. **Existing capability boundary.** MCP, OAuth, hooks, memory, policy, and
   audit remain gateway-owned exactly as defined by the remote-parity design.
6. **No sensitive names.** Workspace names and lifecycle logs contain opaque
   identifiers, not session titles, prompts, channels, repositories, or tokens.
7. **Activity cannot widen authority.** Workload leases can delay autostop but
   cannot call Coder or select a different binding.
8. **Fail-closed reconciliation.** Missing or ambiguous ownership prevents a
   mutation, even if that leaves a resource for manual cleanup.

## User Experience

The Coder Settings panel replaces the shared workspace field with template,
preset, idle-stop, retention, running-limit, prefix, and remote-directory
fields. In-page docs explain that the Coder dashboard will show many session
workspaces over time, but normally only active sessions are running.

Every Kiro Crew slot exposes:

- execution location and Coder workspace link;
- lifecycle state (`creating`, `starting`, `running`, `stopped`, `failed`,
  `deleting`, or `deleted`);
- last activity and scheduled deletion time;
- whether a managed build/job is holding activity;
- a filesystem-reset notice after recreation.

Settings also exposes a managed-workspace table with start, stop, open, and
delete actions. Destructive deletion requires confirmation and uses the same
binding checks as the reconciler.

## Migration from the Static POC

The unshipped Settings implementation currently persists `workspace` and sends
every parent there. It is replaced before PR:

- `template` and optional `preset` replace the shared workspace field;
- existing `crew-dogfood` is not adopted, renamed, stopped, or deleted
  automatically;
- the connection test changes from executing inside that workspace to checking
  owner, template, preset, and CLI readiness;
- new sessions allocate managed bindings;
- already-live static sessions keep their host until reset and then require an
  explicit migration decision rather than silently changing filesystems.

Legacy environment-based static hosting remains a compatibility path during
one deprecation window. It is visibly labeled static/shared and does not claim
per-session isolation or automatic retention. Persisted managed Settings are
authoritative over that environment path.

## Verification Strategy

### State-machine and persistence tests

- One durable parent allocates one binding across concurrent calls and restart.
- Resuming reuses UUID and filesystem generation; a fork allocates another.
- Shared and dedicated subagents inherit the exact parent binding.
- Corrupt registry, identity mismatch, or unknown prefix authorizes no mutation.
- Atomic intent survives cancellation between every Coder transition and local
  registry write.
- Injected clocks prove 30-minute stop configuration and 30-day eligibility
  without sleeps.

### Coder client tests

- Fake structured API covers create, start, stop observation, delete, build
  watch, agent readiness, `last_used_at`, error bounds, and token redaction.
- Token appears only in the authenticated request header or CLI environment.
- Redirect, oversized body, malformed JSON, timeout, and deployment identity
  changes fail closed.
- CLI creation, where used, is reconciled back to the expected UUID and owner.

### Workload tests

- A long foreground tool call remains leased past idle thresholds.
- Parent, shared child, and dedicated child leases compose correctly.
- An ordinary `nohup` descendant remains in the transient systemd scope after
  ACP teardown, keeps Coder activity renewed, and releases it on exit.
- An empty scope stops renewal; Coder activity from an IDE/SSH user remains
  authoritative.
- Permanent Coder agent and system daemons never count as managed work.
- A deliberately escaped user unit is not misclassified as managed activity.

### Lifecycle race tests

- Concurrent create, start, resume, stop observation, and delete eligibility.
- Resume invalidates deletion before the destructive refetch.
- Gateway restart recovers each pending transition idempotently.
- Capacity accounting includes starting and running workspaces exactly once.
- Cron capacity refusal remains retryable; interactive refusal is visible.

### Live AWS dogfood

1. Preserve `crew-dogfood` as an unmanaged smoke workspace.
2. Start two Kiro Crew sessions and observe two distinct Coder workspaces and
   EC2 instances.
3. Verify parent and subagents in each see only their own filesystem.
4. Run a build past the idle threshold and prove its instance remains running.
5. End the workload and connections and observe Coder autostop.
6. Resume and verify the same disk and remote native transcript.
7. Use an accelerated test retention to verify two-phase deletion and fresh
   generation on later resume.
8. Confirm gateway memory recall, update, stdio MCP, HTTP/OAuth MCP, and cron
   remain gateway-owned throughout.

## Delivery Sequence

1. Add the integrity-protected binding registry and structured Coder lifecycle
   client with deterministic state-machine tests.
2. Replace static Settings with template/preset and lifecycle policy, retaining
   the explicit legacy-static compatibility path.
3. Make parent-session host creation lazy and async through the lifecycle
   coordinator; persist binding identity with durable session metadata.
4. Preserve binding inheritance across shared and dedicated subagents and cron
   parents.
5. Add the systemd workload scope contract, lease heartbeats, and Coder
   autostop integration.
6. Add startup reconciliation, capacity admission, 30-day deletion, and
   permanent-session-delete cleanup.
7. Add lifecycle status/actions to the dashboard and update operator docs and
   the AWS sample template.
8. Run the full local parity matrix and live AWS dogfood sequence before PR.

Every behavior change updates the owning session, ACP, provider, subagent,
security, cron/dashboard, and build/install specifications in the same commit.

## Acceptance Criteria

- Two unrelated parent sessions never resolve to the same workspace UUID.
- A resumed parent reuses its prior UUID and filesystem until retention deletes
  it.
- All parent descendants execute in that workspace and cannot request another.
- Long turns and ordinary background descendants cannot be stopped by chat
  inactivity.
- Coder human connections remain authoritative for autostop.
- Inactive compute targets a 30-minute Coder autostop window by default without
  a blind gateway stop.
- A stopped workspace is deleted after 30 inactive days by default, with
  identity revalidation immediately before deletion.
- Resume after deletion creates a fresh generation and clearly reports lost
  sandbox files while preserving gateway history and memory.
- Lifecycle failures never fall back to local execution, another workspace, or
  an unverified destructive target.
- The workspace receives no Coder token, MCP/OAuth credential, gateway secret,
  or lifecycle registry.
- The Settings panel and sample deploy explain the per-session cost and
  retention model.
- The live POC demonstrates two concurrent isolated session workspaces,
  protected long-running work, autostop, resume, and cleanup before PR.
