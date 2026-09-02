## LLM Provider Abstraction

KiroCrew drives a single LLM backend: `kiro-cli` over ACP. The `LLMProvider`
interface is retained as a thin seam (consumers depend only on the ABC), but
there is exactly one concrete provider — `agent.provider` is fixed to `acp`.

### Architecture

```
┌─────────────────────────────────────────────┐
│  Consumers (handler, gateway, cli, session) │
│  Use LLMProvider interface only             │
└──────────────────┬──────────────────────────┘
                   │
         ┌─────────┴─────────┐
         │   LLMProvider ABC  │
         │   providers/base   │
         └─────────┬─────────┘
                   │
            ┌──────┴──────┐
            │ AcpProvider │
            │ acp.py      │
            │ kiro-cli    │
            └─────────────┘
```

**Note:** the removed Bedrock provider and the removed standalone provider were
**deleted** during de-Amazoning, along with their config fields and the
multi-provider dispatch factory. `acp/client.py` keeps a dormant
`ACP_BACKEND_CLAUDE` seam (`AcpProvider` can in principle drive
`claude-agent-acp`) so an internal companion can re-register a Claude backend,
but the public provider factory never selects it — `kiro-cli` is the only
backend.
See [`../features/claude-code-provider.md`](../features/claude-code-provider.md).

### LLMProvider ABC (`providers/base.py`)

```python
class LLMProvider(ABC):
    async def start() -> None
    async def shutdown() -> None
    async def stream(message: str) -> AsyncIterator[LLMEvent]
    async def approve_tool(request_id) -> None
    async def reject_tool(request_id) -> None
    def context_usage_pct() -> float
    # Optional (have defaults):
    async def stream_command(command: str) -> AsyncIterator[LLMEvent]
    async def compact(context: str = "") -> None
    async def wait_for_compaction(timeout: float = COMPACT_WAIT_TIMEOUT_SECS) -> dict
    async def cancel(*, wait_ack_timeout: float = 0.0) -> CancelOutcome
    def is_alive() -> bool
    def touch_activity() -> None
```

### LLMEvent (`providers/base.py`)

Provider-agnostic event dataclass (aliased from `AcpEvent`):

| Kind | Description |
|------|-------------|
| `text_chunk` | Text output from agent |
| `thinking_chunk` | Extended thinking (Claude 3.7+) |
| `tool_call` | Tool invocation |
| `tool_result` | Tool output |
| `permission_request` | Tool approval request (ACP only) |
| `complete` | End of turn |
| `compaction_status` | Compaction result |
| `clear_status` | Clear display |
| `agent_switched` | Agent mode changed |
| `mcp_oauth_request` | MCP server needs OAuth (has `server_name`, `oauth_url`) |
| `mcp_server_initialized` | MCP server ready after OAuth (has `server_name`) |
| `mcp_server_init_failure` | MCP server OAuth/init failed (has `server_name`, `text`) |

### AcpProvider (`providers/acp.py`)

The sole provider. Spawns a long-lived `kiro-cli acp --agent <name>` subprocess,
locally by default or through the opt-in Coder session host described below,
and speaks JSON-RPC 2.0 over stdio.

**Dormant backend seam:** `AcpProvider`/`AcpClient` retain an `acp_backend`
parameter (`"" ` → kiro-cli; `"claude"` / `ACP_BACKEND_CLAUDE` → `claude-agent-acp`)
so an internal companion can re-register a Claude backend over the same
client. **The public provider factory only ever selects kiro-cli** — the claude
branch is unreachable in this build. Its binary-resolution + config-isolation
details live in [`acp-client.md`](acp-client.md); do not re-add the registration
glue or a provider selector (see the repo-root `CLAUDE.md`).

**Key APIs:**
- `start()` → `AcpClient.ensure_ready()` (spawns process, handshake, session/new)
- `stream()` → maps events from `stream_events()`
- `stream_command()` → native slash command execution
- `approve_tool()`/`reject_tool()` → JSON-RPC response
- `context_usage_pct()` → reads `last_prompt_stats.context_pct`
- `context_window_tokens()` → reads `last_prompt_stats.context_window_tokens` (the real served window from `usage_update.size`, 0 if unknown). Used by the dashboard token text instead of re-deriving the window from the model id. A mid-session `set_model` (live switch on both `AcpClient` and `AcpSessionHandle`) rebases these stats via `AcpPromptStats.rebase_to_window`: the window is re-derived from `model_registry.model_window` (0 on a registry miss), `context_used_tokens` is kept, `context_pct` is recomputed and clamped, and `context_tokens_from_usage` is cleared so the next metadata `contextUsagePercentage` can backfill against the NEW model instead of being gated forever by the old model's `usage_update`. The dashboard model-switch endpoint then broadcasts one `context_usage` WS event with `reset: true` (both live-switch and session-reset paths, single and bulk), which lets the frontend reducer replace or delete its stored per-slot token counts — per-turn events without `reset` never delete. The post-compaction pct-0 broadcast carries the same flag.
- `compact()` → sends `/compact` via `send_command()`
- `cancel()` → sends `session/cancel` notification
- `supports_effort()` / `change_effort(level)` / `clear_effort()` → reasoning-effort control (see below)
- `is_alive()` → `AcpClient.is_responsive()` (600s stale threshold)
- `is_process_alive()` → OS-level process check

**Reasoning effort** (Opus/Sonnet/Fable **and GPT-5.x** — shared vocabulary in `effort.py`: levels `low|medium|high|xhigh|max`, capability via `model_supports_effort`, resolution via `resolve_effort_for_model` with priority slot-override > workspace default > None). Capability is a conservative allowlist of known-capable families (`opus`/`sonnet`/`fable`/`gpt`, minus a hard `haiku` exclusion), verified against kiro-cli 2.12/2.13 over ACP — kiro rejects `/effort` on the other third-party models (deepseek/minimax/glm/qwen/auto) with "Effort configuration is currently not available on <model>". A new model family lands as unsupported until confirmed (safe default: the slider hides). Applied via a workspace `cli.json` overlay at `<work_dir>/.kiro/settings/cli.json` → `chat.modelDefaults.<model>.<key>.effort`, written before every spawn (`_write_cli_overlay`) and recovered on init (`_read_cli_overlay`) for server-restart resilience. The `<key>` sub-object is **family-specific** (`effort_settings_key`): `output_config` for Claude models, `reasoning` for GPT models — kiro silently ignores the wrong key, so a mismatched shape would survive a live push but drop on respawn. `_write_cli_overlay` removes stale effort from the other family key while preserving unrelated settings; `_clear_cli_overlay_effort`/`_read_cli_overlay` sweep both keys. Live change pushes `/effort` with the TuiCommand args form (`send_command(args={"level": …})`). The factory threads `reasoning_effort_override` → `effort_per_model[current_model]`; the dashboard handler routes through `change_effort`/`clear_effort` and only resets the session when there is no live provider. Non-effort-capable models persist the slot value without a live apply or reset.

**MCP Tool Search** (kiro backend only — see https://kiro.dev/docs/cli/mcp/tool-search/): loads MCP tool specs on demand ("search-and-call") instead of sending every tool definition each turn, keeping the context window clear when many MCP servers are configured. Gated by the `agent.tool_search` config toggle (default **on**; auto-surfaces as a Settings toggle since the schema is generated from the dataclass).
- Applied via the **same** workspace `cli.json` overlay used for effort (`<work_dir>/.kiro/settings/cli.json`), written deterministically before every spawn and on each restart by `_write_tool_search_overlay` (called from `AcpProvider.__init__` and `start()`). When enabled it writes the flat keys `toolSearch.enabled=true` plus `toolSearch.minPct`/`toolSearch.minTokens`, taken from `agent.tool_search_min_pct` / `agent.tool_search_min_tokens` (defaults `5` / `50000`, mirroring kiro-cli's own thresholds; clamped to 0-100 and >= 0, non-numeric falls back to the default); when disabled it writes `toolSearch.enabled=false` and drops both thresholds.
- **Why the thresholds are not forced to 0:** deferral costs a round-trip — a deferred tool's spec is absent from the model's tool list, so the first direct call fails with `A tool with the name '<name>' does not exist` and has to be recovered with `tool_search`. That only pays once the specs are genuinely large, which is what the thresholds express (kiro-cli defers when EITHER is exceeded). An earlier build hard-coded both to `0`, imposing the round-trip on every install including ones far below the threshold. Setting both to `0` still restores unconditional deferral for operators who want it. The thresholds are written **explicitly** rather than omitted, so a machine carrying the old forced zeros is actually migrated instead of silently keeping them.
- Writing both `true` and `false` makes the KiroCrew toggle authoritative over any value in the user's global `~/.kiro/settings/cli.json`. The write is merge-safe with the effort `chat.modelDefaults` keys in the same file.
- **claude backend** — no-op. Tool Search is a kiro-cli feature; `_apply_tool_search_overlay` returns early for the claude backend and when no toggle value was threaded in (`tool_search is None`).

- **Resume guard:** `session/load` is attempted only when the session host confirms the prior transcript. Local hosting checks its local Kiro session path; Coder hosting probes the validated id in the workspace and sends the derived remote path. A stale persisted sid falls back to `session/new`.
- **Working dir:** `AcpProvider.cwd` overrides the `LLMProvider` ABC default so `session_map` persists the real workspace path. AcpProvider's work_dir lives on the inner client (`_client._work_dir`), so the prior `getattr(provider, "_work_dir", "")` persisted `""` for all ACP sessions — `provider.cwd` fixes resume-cwd-override. A remote session still persists this control-plane path, while its ACP `session/new.cwd` is the session host's normalized POSIX path.

#### Session environment providers

`session_environment.py` separates managed compute lifecycle from the LLM
provider. `SessionEnvironmentRegistry` resolves an explicit
`SessionEnvironmentSelection(provider, configuration)` to a concrete adapter;
an unavailable persisted provider raises `SessionEnvironmentUnavailable` and
never degrades to local execution. `SessionEnvironmentBinding` is the only
dashboard/history wire object and carries three non-secret strings: provider id,
provider-owned configuration id, and generated resource name. Protected UUIDs,
owners, credentials, templates, presets, and deletion intents never enter it.

Adapters create a `RemoteSessionHost`, project safe binding metadata, persist
stop intent by trusted session key, and may positively opt into the gateway
lifecycle loop. One reconciliation pass obtains one provider inventory and one
protected binding snapshot, renews active workload scopes first, then drains
pending stops and evaluates retention against those same views. Independent
remote mutations run with bounded concurrency and per-operation timeouts; a
renewal phase completes before any stop phase begins. Gateway shutdown cancels
the periodic loop and gives providers a bounded final drain for already-persisted
stop intent. A provider-owned pending stop is therefore retried after a failure
or gateway restart without making shutdown depend on the next periodic tick.
Catalog-shaped objects alone cannot gain periodic mutation authority. Each
adapter keeps its concrete control-plane trust checks at its own boundary. Coder
is the first implementation; another provider can join the registry without
adding common dashboard state or changing ACP runtime dispatch.

Environment health is a separate positive opt-in capability. It accepts only a
trusted durable session key and returns provider id, generated resource name,
the allowlisted state (`starting`, `running`, `stopped`, or `unavailable`), and
optional bounded memory telemetry. The public catalog does not imply health
authority. Unsupported adapters return no sample, and health snapshots never
enter slot metadata, WebSocket frames, transcripts, memory, prompt context, or
MCP results. Provider state and optional telemetry are independent: a telemetry
probe failure omits that telemetry without erasing an already verified
control-plane state.

#### Coder session environment

`acp/session_host.py` makes the process location an explicit runtime boundary.
`LocalSessionHost` preserves the existing local spawn path. An explicit
`session.coder.enabled=true` opts newly constructed parent providers into a
`ManagedCoderWorkspaceSessionHost`; URL, template, optional preset, remote cwd,
warm/autostop/retention limits, capacity, and an opaque name prefix live in
`config.json`, while the session token lives only in the gateway secret vault.
An explicit false is authoritative even when a stale service environment still
contains the legacy variables. Installations that have never saved this setting
may still use `KIROCREW_CODER_WORKSPACE`, `CODER_URL`,
`CODER_SESSION_TOKEN`, and `KIROCREW_CODER_REMOTE_CWD` as a migration fallback.
This applies equally to default and named-agent chat/cron parents; selecting a
named agent does not move the session back to the gateway. Background and
maintenance factories remain local. Descendant
subagents inherit the live parent's host explicitly:
shared children open another session on the remote runtime, while children with
an explicit agent, model, effort, tool set, or bare mode clone the host into a
dedicated runtime in the same workspace. `KIROCREW_CODER_BIN` selects the local
transport binary and the remote cwd defaults to `/home/coder/workspace`.

Settings GET returns only token presence. PUT splits the bearer into vault
storage and coordinates into config, refreshes the factory/warm pool for new
sessions, and leaves active providers untouched. The chat slot serializer asks
the live provider for `execution_location`, so a local session is never labeled
remote merely because the default changed. The owner-only test endpoint probes
candidate coordinates and bearer without saving or returning them.

The Coder environment adapter binds each durable parent session key to one generated Coder
workspace in an owner-only registry. It creates the workspace from the selected
template/preset on first use, starts a stopped binding on resume, and refuses a
new parent when the configured running-workspace cap is reached. Allocation
reserves capacity under a short lock, performs the remote create outside it, and
uses one workspace inventory snapshot for that decision, so unrelated creates
can proceed concurrently without exceeding the cap. Lifecycle passes likewise
fetch one inventory and reconcile locally rather than issuing one list command
per binding. Coder runs at most four independent lifecycle mutations at once,
bounds each at 30 seconds, and gives pending-stop drainage a 10-second gateway
shutdown budget. Descendant
subagents inherit the concrete parent host and stay in that same workspace.
The static environment path remains a migration fallback only; a manually
created bootstrap workspace such as `crew-dogfood` is never enrolled in managed
retention unless it is present in the binding registry.

Dashboard progress follows actual compute work. A new binding reports allocation
and provisioning, a stopped binding reports provisioning while Coder starts it,
and a binding whose workspace is already running skips both and reports only the
short agent-runtime connection. Once Coder confirms the workspace is running,
the host reports the execution environment as ready even while the ACP handshake
finishes; that handshake uses the ordinary turn loader rather than replaying the
billable-compute startup card. A reconstructed host seeds its generated name and
initial phase from the durable binding, so an existing session never flashes an
anonymous allocation state before the Coder status check completes.

Coder health inspection resolves the protected binding by session key, fetches
the current workspace, and revalidates immutable UUID, owner, template, and
generated name before any remote probe. It samples memory only when the control
plane reports that exact workspace running. The fixed stdlib probe runs through
`coder ssh --disable-autostart`, explicitly unsets `CODER_AGENT_TOKEN` and
`CODER_AGENT_TOKEN_FILE`, reads `/proc/meminfo`, and clamps availability to a
finite cgroup v2 or v1 memory limit. Execution time and output are bounded;
numeric output is validated before pressure is classified as elevated at 80%
used or critical at 90%. A control-plane or identity failure becomes
`unavailable`; a memory-probe failure retains the verified workspace state and
omits memory, without returning raw command output or diagnostics. The same
probe reads the root-owned bootstrap status breadcrumb: a validated
`running:<stage>` keeps the environment in `starting`, `failed:<stage>` makes it
`unavailable`, and `complete` leaves the verified control-plane state unchanged.
Workload-scope inspection also uses
`--disable-autostart`, so a status race cannot wake stopped compute.

The runtime invokes `coder ssh <workspace> --remote-forward
<workspace-port>:127.0.0.1:<gateway-loopback-port> -- kiro-cli acp --agent
<name>` (plus the selected model) and preserves ACP JSON-RPC stdio end to end.
Everything after Coder's `--` boundary is serialized as one shell-quoted remote
command after validating every identifier, so model or agent text cannot become
remote shell syntax.
Preparation probes a bounded random set of high workspace-loopback ports; a
forwarding failure aborts startup rather than falling back to local ACP.
Only Coder URL/token, PATH, certificate, and proxy variables reach the transport
process; AWS, SSH, channel, Kiro API-key, and Kiro Crew variables are not copied
from the gateway. Coder's workspace agent injects its own live token into remote
commands, so every gateway-controlled preparation, cleanup, and ACP command
explicitly unsets both `CODER_AGENT_TOKEN` and `CODER_AGENT_TOKEN_FILE` before
the target process starts. Workspace and agent names use a strict identifier
grammar, and the remote cwd must be an absolute normalized POSIX path.

Before spawn, the host materializes the local agent, resolves a `file://` prompt
through the existing sensitive-path-aware resolver, and streams an owner-only
projection to `~/.kiro/agents/<name>.json` in the workspace. The projection
keeps `name`, `description`, `model`, `prompt`, ordinary workspace tools, and
only those `@server` references backed by a gateway relay. Original MCP
commands, args, env, URLs, headers, hooks, unknown fields, and gateway paths are
dropped. `session/new` and `session/load` inject per-session relay entries whose
argv contains only a copied stdlib relay, a workspace loopback port, and an
owner-only capability-file path. Gateway-side target resolution and environment
sanitization happen before minting; the remote peer cannot select a command,
server, caller, or credential.

Enabled stdio MCP servers, including `kirocrew-core`, execute on the gateway and
receive the strict logical `KIROCREW_SESSION_KEY`, restoring immediate memory
updates and user stdio MCP without copying Kiro Crew state into Coder. URL-based
servers use the same relay while the gateway runs the official MCP SDK's
Streamable HTTP or narrow legacy-SSE transport. OAuth discovery, PKCE, callback,
encrypted grants, refresh, and integration disconnect remain gateway-owned; no
URL, configured header, OAuth material, or callback ownership crosses the
boundary. Kiro-cli script hooks are still omitted pending a separate gateway
hook relay.

Remote hosting is positively limited to `ACP_BACKEND_KIRO`; another harness
fails before spawn. The SSH client itself is not wrapped in Kiro Crew's local
agent sandbox or cgroup because the Coder workspace is the execution boundary.
Preparation and cleanup commands have a bounded lifetime and terminate their
process tree on timeout or cancellation. Coder lifecycle stdout and stderr are
drained concurrently into bounded buffers to prevent pipe deadlocks or
unbounded memory use. Logs retain only a redacted, truncated diagnostic;
user-facing preparation errors expose only the operation and exit code, not raw
remote stderr, which may contain credential-bearing diagnostics.

Managed ACP transports have their own short warm timeout. Once the session is
idle and its semaphore is unlocked, the gateway closes the SSH/ACP runtime but
keeps both the Kiro session map and Coder disk. Coder's own autostop then removes
compute. Managed runtimes and their ordinary descendants run in a named
transient systemd user scope. While that exact scope remains active, the
reconciler renews Coder's shutdown deadline at no more than one third of the
configured autostop window; it never treats generic CPU or the permanent Coder
agent as activity. The same bounded loop considers only stopped,
registry-owned bindings for retention. After the configured inactivity period
it records a durable delete intent, re-fetches and re-validates exact identity,
deletes the workspace, and records the tombstone. Active or starting
workspaces, unbound workspaces, and a parent with an in-flight foreground turn
are never retention targets.

The owner-only binding registry fails closed on malformed bytes. Recovery is an
explicit operator action, `kirocrew environment repair --yes`: it moves the
original into the protected owner-only `coder_workspaces.json.corrupt/`
directory and writes a fresh empty registry. Repair never adopts, stops, or
deletes an existing Coder workspace; those rows remain manual operator work
until a new trustworthy binding is allocated.

### Config (`config/loader.py`)

```json
{
  "agent": {
    "provider": "acp",
    "model": "auto"
  }
}
```

- `agent.provider` is fixed to `"acp"` (enum `["acp"]`); there is no provider to choose.
- `create_provider_factory()` returns a `Callable` that creates the kiro-cli `AcpProvider`; for the main interactive agent it resolves the session-host environment. An explicit inherited host override wins for dedicated descendants so remote affinity cannot degrade to local placement.

An agent spec's model is consumed by kiro-cli before Kiro Crew reaches
`session/new`, so the live-session entitlement guard cannot diagnose a wrong
wire spelling at spawn time. Agent create/update validate a pin before
persisting it: they reuse the role-model validator for advertised ids and
`model_registry.acp_id_correction` for the offline positive case where the
registry recognizes a non-ACP spelling and can name its ACP id. Unknown ids are
allowed because they may be valid regional or newly released ids; empty and
`auto` continue to defer. Doctor applies the same correction audit to every
discoverable user- and project-scoped spec.

### MCP Server Registration

MCP servers are passed directly in the `session/new` params. The two managed
servers (`kirocrew-core`, `kirocrew-cron` — see `agent.py:_MANAGED_MCP_SERVERS`)
are always present; user-configured servers from the agent config are merged in.

### SessionManager (`session.py`)

- Provider-agnostic via factory (one provider: kiro-cli `AcpProvider`)
- Calls `repair_agent_configs()` on gateway startup and periodically
- context_info() reports model/agent
- Resume: calls `set_resume_session_id()` before `start()`

### Subagent Approval Mode Inheritance (`subagent.py`)

Subagents inherit the global `approval_mode=auto` config as a final fallback when:
1. No parent session key exists (spawned independently), OR
2. Parent session key exists but the session is no longer in the store (garbage-collected)

If the parent session is alive but returned no policy, deny-by-default applies — the session is intentionally non-auto. This ensures subagents spawned from dashboard sessions still get auto-approval even if the parent session is GC'd before the subagent executes.

### Automatic recovery

Provider-level recovery mechanisms that fire automatically without user intervention:

**Interactive transient-5xx retry** (a270bd1f; post-token recovery c6fe60a): The interactive dashboard/Slack `chat_runner` stream loop retries a transient backend 5xx (InternalServerError / DispatchFailure / ConnectionReset, JSON-RPC `-32603`) through the shared `llm_helpers` transient classifier + backoff, **without** resetting the still-alive session. Auth/validation errors are excluded (fail-fast); on retry-budget exhaustion a clean error surfaces on a still-resumable session. This extends the unattended `stream_and_collect` retry path (previously deferred for the interactive loop) to interactive callers.

A transient 5xx that arrives *after* the turn already emitted output (the `_turn_emitted` guard is set once any assistant token streams or a tool call fires) no longer drops the turn. Instead it **RECOVERS ONCE**: the streamed partial is preserved as a finalized assistant message, a brief recovery notice is appended, and a *continue* instruction (not the original prompt) is re-queued onto the SAME live ACP session — which still holds the interrupted turn's context (original prompt, streamed partial, and any completed tool results) — so the model resumes from where it stopped rather than restarting. The recovery is one-shot per genuine user turn: the allowance is consumed only when a recovery is actually enqueued and is refreshed at the start of the next real user turn, never on the synthetic recovery turn, so a repeated post-token 5xx during recovery surfaces a clean error instead of looping. When Stop is active or the turn is nested (`_prompt_depth != 0`) the partial + notice are still shown but nothing is re-queued (the allowance is left unconsumed). This recovery **also applies to turns that already fired a tool call** — an ACCEPTED TRADEOFF (owner decision), rather than failing fast: a mid-stream 5xx is rare, and the continue instruction tells the model to resume and not re-run tools that already completed. A residual double-execution risk remains only for a side-effecting/destructive tool that was still *in flight* when the 5xx hit; the owner accepts that narrow risk in favor of recovering the turn.

**Compaction-failure notice backoff** (dashboard-chat; `dashboard/chat_utils.py:_broadcast_compaction_result`): Per-turn compaction failures no longer spam the chat. Per slot, `_compaction_fail_streak` counts consecutive failures and the first `_COMPACTION_NOTICE_SHOW_FIRST_N` (=2) are shown verbatim ("❌ Compaction failed: …"); further failures within the `_COMPACTION_FAIL_COOLDOWN_SECS` (60s) `_compaction_fail_cooldown_until` window are suppressed, and when the cooldown elapses a single collapsed "failed Nx in a row … Consider `/compact` manually" message is shown with `/compact` guidance. A `completed` status resets the streak/cooldown. `acp/client.py:_handle_compaction_status` logs the raw failed-compaction notification params at WARNING (kiro-cli carries no dedicated error field on failure). This is a UX/spam guard only — the underlying compaction still runs every turn on kiro-cli's schedule — and is distinct from SessionManager's proactive auto-compact cooldown.

**Compaction resets — then accurately re-reports — the context meter**: a `completed` `_kiro.dev/compaction/status` drops the stale token stats at the provider chokepoints — `AcpClient._handle_compaction_status` (every dispatch loop plus `wait_for_compaction`) and the mirrored sites in `AcpSessionHandle` (prompt dispatch loop and its `wait_for_compaction` queue-drain path) — via `AcpPromptStats.reset_after_compaction()`: `context_used_tokens`/`context_pct` zero out and `context_tokens_from_usage` clears (so fresh metadata can re-derive instead of being gated by the pre-compaction `usage_update`), while `context_window_tokens` is kept (the model did not change, so the served window still holds). kiro-cli then emits a fresh `_kiro.dev/metadata` with the real post-compaction `contextUsagePercentage` about a second after the completed status (live-probe confirmed), so `wait_for_compaction` grace-drains up to `_POST_COMPACTION_METADATA_GRACE_SECS` (5s) for it on `AcpClient`, `AcpSessionHandle`, and `AcpProvider`'s cached mid-turn result path (which delegates to the inner client via the `AcpSessionProvider` pass-through); the drain only ends on a metadata frame actually carrying a `contextUsagePercentage` (a credits-only frame is consumed but does not end it), re-queues non-metadata frames before any poison sentinel, and lets process death (`AcpError`) propagate; `_backfill_context_window` prefers the **kept served window** over the model registry when deriving tokens from that percentage, since the served size can differ from the static entry (e.g. opus served at [1m] vs a 200K registry row). The dashboard's manual `/compact` path then broadcasts the REAL post-compaction numbers when the drain captured them, and only falls back to `context_usage {pct: 0, reset: true}` (the same contract as the threshold auto-compact callback and the in-turn `_broadcast_compaction_result` chokepoint) when no metadata arrived — the meter then self-corrects on the next turn's telemetry. A failed/timed-out compaction leaves the counts untouched and re-sends them as-is. `_context_usage_payload` treats `used == 0` with a known window as "not measured yet" and omits the token fields, so the unconditional end-of-turn broadcast cannot overwrite a reset with a false "0 / W tokens" claim.

### Installation

KiroCrew drives `kiro-cli` over ACP — install it per its own docs, ensure it is
on `PATH`, and run `kiro-cli login`. `kirocrew doctor` reports its status.


## AcpProvider: shared-runtime startup

`AcpProvider.start()` branches on the backend:

- **Runtime-backed harness** → `_start_kiro_runtime()`. This spawns an
  `AcpRuntime` (carrying the provider's sandbox mode, extra env, session host,
  and MCP-gateway overlay/socket), resumes via `runtime.load_session()` when a prior transcript
  exists or otherwise `runtime.create_session()`, applies the configured model,
  and replaces `self._client` with an `AcpSessionProvider` (which implements the
  same interface as `AcpClient`, so downstream callers are unchanged). Any
  failure after `spawn()` kills the runtime so a half-initialised session never
  leaks an orphaned `kiro-cli`.
- **Legacy per-session harness** → `AcpClient.ensure_ready()`.

`AcpProvider.is_session_sharing_eligible` is membership in
`ACP_BACKENDS_SESSION_SHARING` (harness-parity H6), not `not is_claude_backend`:
a capability granted by the absence of one backend is inherited by every backend
added later. It is what `SessionManager.is_session_sharing_eligible()` consults
to decide whether a parent session can host multiplexed subagent sessions. The
invariants governing what an added harness may and may not change are in
[harness-parity.md](harness-parity.md).
