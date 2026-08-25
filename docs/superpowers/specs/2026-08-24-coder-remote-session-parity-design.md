# Coder Remote Session Parity

**Status:** Stdio MCP, memory, resume, and subagent-affinity milestone implemented; gateway-owned HTTP/OAuth and hook relays remain
**Date:** 2026-08-24

> Workspace cardinality and lifecycle are governed by
> [Coder Per-Session Workspace Lifecycle](2026-08-25-coder-per-session-workspace-lifecycle-design.md).
> One durable parent session tree owns one workspace; the static shared
> `crew-dogfood` POC is not the target topology.

## Decision

A Kiro Crew gateway may host a Kiro ACP session in a Coder workspace while
retaining ownership of MCP processes, credentials, memory, policy, hooks, cron,
and durable control-plane state on the gateway. The workspace receives only
short-lived capabilities bound to one logical session and one service. Those
capabilities travel through reverse forwards on the same encrypted Coder SSH
transport that carries ACP.

Execution placement follows the session tree:

```text
Gateway
  - cron scheduler and dispatch
  - memory, history, and durable session metadata
  - MCP backends and credentials
  - policy and hook execution
  - capability registry and bridge ingress
                  |
                  | coder ssh: ACP stdio + reverse forwards
                  v
Coder workspace
  - parent kiro-cli ACP runtime
  - built-in file, shell, and code tools
  - every subagent descended from the parent session
  - credential-free MCP and hook relay shims
```

Cron remains a gateway responsibility. When a cron firing starts a Coder-hosted
session, the gateway performs the dispatch, the parent agent executes in Coder,
and every subagent spawned by that parent inherits the same workspace affinity.
Unrelated gateway maintenance, consolidation, probes, and background one-liners
remain local unless a caller explicitly starts a Coder-hosted session.

## Motivation

The current Coder proof of concept makes the process-location seam explicit and
keeps gateway secrets out of the workspace, but deliberately projects an
MCP-free agent. That validates remote ACP execution without validating the Crew
experience. A remote session can receive gateway-built memory in its prompt, but
cannot call `learn_add`, use Crew or user MCP tools, run configured kiro-cli
hooks, spawn every form of subagent in the workspace, or reliably resume a
transcript whose file exists only in Coder.

Installing Kiro Crew, its data home, and its credentials into the workspace
would remove those gaps at the cost of making the workspace a second control
plane. This design keeps one control plane and extends its existing broker
boundary over SSH.

## Goals

- Make a Coder-hosted session behave like a local session for gateway-managed
  MCP tools, memory recall and updates, hooks, session resume, and subagents.
- Keep MCP backend processes, static credentials, Kiro Crew memory, security
  policy, governance files, and audit state on the gateway.
- Run the parent ACP session and all of its subagents in the same Coder
  workspace, including subagents that need a dedicated runtime for a model,
  effort, tool, or agent override.
- Preserve the local ACP and MCP behavior when no Coder host is selected.
- Fail closed when the bridge or workspace cannot be prepared. A remote session
  must never fall back to local execution or receive raw backend configuration.
- Reuse the current MCP gateway's caller-context and backend lifecycle contracts
  instead of creating a second implementation of Crew tools or memory.

## Non-goals

- Moving the Kiro Crew gateway, scheduler, database, or memory store into Coder.
- Giving the workspace direct access to the gateway data home, its Unix socket,
  its internal HTTP secret, or the Coder control-plane token.
- Synchronizing `~/.kiro/crew`, AWS credentials, SSH credentials, channel tokens,
  or MCP credential-bearing configuration into the workspace.
- Running unrelated cron scheduling, memory consolidation, MCP discovery, or
  gateway maintenance inside the workspace.
- Supporting non-Kiro ACP harnesses through the Coder host. The existing
  positive `ACP_BACKEND_KIRO` restriction remains.
- Treating the workspace as trusted enough to select arbitrary callers, server
  names, backend commands, environment variables, URLs, or headers.

## Current Gaps

| Surface | Local session | Current Coder POC | Target |
|---|---|---|---|
| Prompt memory recall | Gateway context builder | Works | Preserve |
| Immediate memory update | `kirocrew-core` MCP | Missing | Gateway MCP bridge |
| User and managed stdio MCP | Local process or gatewayd | Missing | Gateway MCP bridge |
| Streamable HTTP MCP | Direct from kiro-cli | Missing | Gateway HTTP transport adapter |
| Legacy HTTP+SSE MCP | Direct from kiro-cli | Missing | Gateway compatibility fallback |
| OAuth-protected remote MCP | Kiro CLI owns opaque token | Missing | Gateway OAuth broker and vault |
| Kiro Crew prompt hooks | Gateway | Works | Preserve |
| kiro-cli script hooks | Local agent config | Stripped | Gateway hook bridge |
| Tool permissions and policy | Gateway ACP permission loop | Partial | Preserve and test end to end |
| Native session resume | Local transcript guard | Local guard misses remote file | Host-aware resume probe |
| Shared-runtime subagents | Parent runtime | Some cases inherit | Always inherit workspace affinity |
| Dedicated subagents | New local provider | Run locally | New runtime in parent workspace |

## Architecture

### 1. Session execution affinity

Process placement becomes an explicit, immutable property of a logical session,
represented by a session execution affinity rather than inferred repeatedly
from the agent name or global environment.

An affinity contains the host kind, Coder workspace, normalized remote working
directory, and the runtime bridge configuration. It contains no capability
secret and no MCP credential. The gateway persists enough non-secret affinity
metadata with the session to reconstruct a host after restart.

The provider factory uses the selected affinity for the parent session. A child
spawn inherits its parent's affinity before choosing shared or dedicated ACP
execution:

- A session-sharing-eligible child calls `session/new` on the parent's remote
  `AcpRuntime`.
- A child requiring a different model, effort, allowed-tool set, bare mode, or a
  dedicated continuation starts another remote runtime in the same workspace.
- A failure to create either remote path fails the child. It does not trigger
  the existing local-provider fallback.
- A spawn without a live or persisted remote parent uses the ordinary local
  placement rules.

This changes the fallback boundary, not the session-sharing policy. Sharing
continues to be an opt-in harness capability and keeps the existing eligibility
checks.

### 2. Runtime bridge

Each remote ACP runtime owns a `CoderSessionBridge` on the gateway. The bridge:

1. binds an ephemeral TCP ingress to gateway loopback only;
2. allocates a workspace loopback port and adds an SSH reverse forward from that
   port to the ingress;
3. mints capabilities for logical sessions and their projected services;
4. retains the trusted service descriptors and caller identities server-side;
5. revokes the runtime's capabilities before closing the SSH transport.

The runtime retries bounded remote-port collisions during preparation. A
forwarding failure aborts session startup before `session/new`. The ingress is
not advertised through Tailscale, the dashboard listener, or a public network
interface; Coder SSH is the only network path to it.

The ACP subprocess and the reverse forward share one `coder ssh` lifecycle.
Losing SSH therefore kills ACP and disconnects every relay together, allowing
the existing session recovery path to restart or resume one coherent runtime
instead of recovering control and tools independently.

### 3. Capabilities

A capability is an unguessable random bearer minted for exactly one logical ACP
session and one bridge service. Examples are one MCP server or the hook runner.
The gateway stores only a digest and authoritative metadata:

- runtime identity;
- logical session key and session type;
- service kind and fixed server identity;
- expiry and revocation state;
- request-size and concurrency policy.

The raw bearer is written to an owner-only file below a runtime-specific
directory in the workspace. It is never placed in argv, an agent JSON field, an
environment variable, an ACP frame, a log line, or an SEL resource string. The
relay reads the file for each connection and sends a bounded authentication
frame before any protocol data.

The remote side supplies no trusted caller or backend fields. A successful
lookup supplies those fields from the registry. A capability cannot pivot to a
different session or server because no selector is accepted after
authentication. Capabilities expire with their logical session, are revoked on
session destruction, and are rotated on runtime reconstruction or resume.

Possession grants the workspace the ability to make the same calls that the
bound session could make while it is alive. That is the deliberate workspace
authority. It does not grant credential disclosure, arbitrary gateway HTTP,
another session's identity, or a new MCP backend.

### 4. Credential-free workspace relay

The gateway streams a small versioned relay bundle into an owner-only directory
in the workspace during preparation. The relay uses the workspace's Python
runtime and standard library only; Kiro Crew is not installed remotely.

For each enabled MCP server, `session/new` injects a stdio entry whose command
starts the relay with a server-specific capability-file path. The projected
agent retains only the matching `@server` tool references. It contains no
backend command, arguments, working directory, URL, headers, environment, pool
key, socket path, or gateway address.

The relay frames MCP stdio bytes over the reverse-forwarded connection. It is a
transport shim, not an MCP implementation: request IDs, notifications, streaming
results, cancellation, progress, elicitation, and binary-safe UTF-8 lines pass
through without semantic rewriting. Framing has explicit line, aggregate,
pending-request, and connection bounds owned by the bridge module.

The same bundle contains a one-shot hook relay. A projected kiro-cli hook keeps
its event and matcher but replaces its command with the relay. The relay sends
the original hook event JSON to the hook capability and returns the gateway
hook result. The gateway executes the configured command through the existing
hook runner and platform sandbox. Remote paths in event payloads remain remote
workspace paths; hook authors must treat them as identifiers unless a future
hook explicitly requests a bounded workspace file operation.

### 5. Gateway MCP adapters

The bridge resolves every MCP service from the gateway's effective materialized
agent configuration. Remote input never participates in that resolution.

For stdio servers, the bridge enters the existing MCP gateway path with a
gateway-constructed registration and caller context. Normally poolable servers
retain their pool behavior. Servers that are not poolable get an isolated
backend keyed to the remote connection, preserving local process isolation
without spawning them in Coder. Managed servers such as `kirocrew-core` and
`kirocrew-cron` use the same path, so all existing strict session-key handling,
gateway endpoint forwarding, policy, image budgeting, audit stamping, and
backend lifecycle behavior remain in force.

HTTP, SSE, and OAuth are gateway transport concerns. The workspace still sees
the same local stdio relay described above; it does not run an HTTP client and
does not receive a remote URL, headers, OAuth metadata, registration, token,
refresh token, authorization code, or PKCE verifier.

#### 5.1 Remote HTTP transport

The bridge stores an HTTP target server-side containing the canonical resource
URL, validated static headers, requested scopes, configured OAuth client
identity, and transport policy. After authenticating the session capability,
the gateway translates the relayed newline-delimited MCP messages into the
official MCP Python SDK's client session and translates replies back to the
stdio stream. This preserves MCP request IDs, notifications, progress,
cancellation, elicitation, and streaming semantics without teaching the relay
HTTP or OAuth.

The gateway prefers the current Streamable HTTP transport. It attempts the
legacy HTTP+SSE client only when the upstream gives a protocol-compatibility
response that means the modern endpoint is unsupported, such as HTTP 404 or
405. It never falls back after an authorization response, TLS or hostname
failure, timeout, malformed response, or policy denial. A fallback must not turn
a secure failure into a request made with different credential behavior.

Static configured headers are injected only by the gateway at the final
upstream request. They are target state, not session capability metadata, and
are never copied into the projected agent, relay files, error details, logs, or
downstream MCP messages. The adapter applies bounded connection, response,
redirect, message, and shutdown limits and closes the upstream transport when
the capability or session is revoked.

The implementation pins an official MCP Python SDK v2 release that supports
Streamable HTTP, legacy SSE, OAuth discovery, dynamic client registration,
PKCE, refresh, and custom token storage. Kiro Crew wraps that SDK at its
transport and persistence seams; it does not implement OAuth protocol state or
maintain a bespoke HTTP+SSE parser. The SDK is a gateway dependency only and is
never installed in the workspace.

#### 5.2 Gateway OAuth broker

Kiro CLI's native OAuth token store remains opaque and is not copied or reused.
For a remote HTTP MCP target, Kiro Crew is the OAuth client and owns the entire
authorization lifecycle on the gateway:

1. The SDK discovers the protected-resource and authorization-server metadata,
   validates the advertised relationship, and uses the configured client or
   dynamic client registration as permitted by the server.
2. The gateway creates an authorization attempt bound to the MCP target,
   logical session, cryptographic state, PKCE verifier, expiry, and trusted
   callback URI.
3. The existing MCP OAuth banner receives a session-scoped pending event and
   shows the authorization link while server initialization waits for a bounded
   period. It later receives success or failure without exposing token data.
4. The user's browser authorizes with the upstream provider. The callback
   terminates at the gateway's configured dashboard or Tailscale origin.
5. The SDK validates state and issuer context, exchanges the code with PKCE,
   and persists the resulting registration and token through the gateway vault.
6. The waiting transport resumes initialization. Later refreshes happen on the
   gateway and are serialized per credential identity to prevent refresh races
   across concurrent sessions.

The callback origin comes from an explicitly configured dashboard URL or the
gateway's trusted Tailscale origin. It is never derived from the inbound `Host`,
`Forwarded`, or `X-Forwarded-*` headers. Redirect URIs are exact registered
values. The callback accepts only a bounded query, consumes its one-shot state
exactly once, and returns no token or verifier to the browser. OAuth is allowed
only over HTTPS, except for an upstream or callback on gateway loopback
(`localhost`, `127.0.0.1`, or `::1`) used for local development.

OAuth credentials are keyed by the canonical MCP resource URL and OAuth client
identity. The MCP server name is a display label, not credential identity, so a
server rename may keep the same grant while a resource URL or client change
cannot accidentally reuse one. Tokens and dynamic registration data are stored
as encrypted gateway secrets through `SecretVault`; state and PKCE verifiers
are short-lived authorization-attempt state and are never durable session data.

Metadata and redirect fetches use bounded sizes, timeouts, and redirect counts.
Credentials are sent only to the validated resource or authorization endpoint
for which they were issued. A redirect that would forward an Authorization
header, cookie, token, or configured credential to a different origin fails
closed. TLS verification remains enabled. URLs in diagnostics are reduced to
non-secret origin and bounded path context, with query and fragment removed.

Session capability lifecycle and OAuth grant lifecycle are intentionally
separate. Ending or revoking a remote session closes its HTTP connection and
invalidates its capability, but retains the user's reusable gateway grant.
Disconnecting the MCP integration deletes its stored tokens and dynamic client
registration and attempts standards-based token or client revocation when the
provider advertises it. Failure to revoke remotely does not preserve the local
secret.

An authorization requirement or failure affects only that MCP server. The
gateway returns a bounded machine-readable `remote_mcp_oauth_required` or
`remote_mcp_oauth_failed` result and never falls back to direct sandbox network
access, sandbox-side OAuth, static credential forwarding, or local ACP
execution. Non-authentication transport failures use
`remote_mcp_transport_failed` with redacted details.

### 6. Agent projection

Projection changes from an MCP-free allowlist to a capability-backed allowlist.
For every agent selected by a remote parent or child, the host materializes the
local definition, resolves its prompt through the current sensitive-path gate,
and projects:

- name, description, model, prompt, and supported non-secret agent fields;
- built-in workspace tools and allowed-tool restrictions;
- `@server` references only when a matching bridge capability was created;
- bridge MCP entries injected per logical session rather than serialized into
  the shared agent file;
- relayed hook entries with their original event and matcher.

Unknown fields remain dropped. Original MCP definitions, original hook command
paths, `file://` resources, secrets, and gateway filesystem paths never enter
the workspace. Projection is idempotent and runs on demand before every
`session/new`, so custom agents selected by subagents do not depend on the
runtime's initial parent projection.

### 7. Memory behavior

Memory remains one gateway-owned system:

- Initial and per-turn recall continues through `ContextBuilder`; no memory
  database is synchronized into Coder.
- `learn_add`, lesson removal, memory search, and other Crew memory tools reach
  the existing gateway implementations through `kirocrew-core`.
- The bridge stamps the logical caller identity, so a subagent mutation is
  attributed to `subagent:<id>` rather than its parent slot.
- Transcript capture and the existing preference, project, episodic, semantic,
  lesson, and skill consolidation paths continue on the gateway.
- A resumed remote session skips duplicate native thread-history injection but
  still receives current Crew memory, lessons, and cross-session context exactly
  like a local resumed session.

No new memory write path is introduced. The bridge restores access to the
existing single write paths.

### 8. Session resume

Transcript existence is a host concern. `LocalSessionHost` keeps the current
local file guard. `CoderWorkspaceSessionHost` performs a bounded remote metadata
probe for the exact validated session ID without reading or returning transcript
content. The host also derives the corresponding remote transcript path from
that validated ID; `session/load` receives the workspace path in its private
metadata, never the gateway's `~/.kiro/sessions` path and never an arbitrary
caller-supplied path.

On resume, the gateway first rebuilds SSH forwarding, rotates capabilities, and
reprojects the selected agent. It then calls `session/load`. The loaded Kiro
session reinitializes MCP against newly injected relay entries. A missing or
rejected remote transcript falls back to a fresh parent session under the
existing interactive rules; a subagent continuation keeps its existing
fail-closed rule and refuses to run without prior context.

### 9. Cron and subagents

Cron ownership and execution placement are deliberately distinct:

- The gateway owns schedules, wakeups, admission, retries, and durable run
  records.
- A due job configured for Coder asks the gateway session manager to create a
  Coder-hosted parent session.
- The parent and every descendant execute in the parent session's bound
  workspace.
- Calls to `kirocrew-cron` from that remote tree return through the gateway MCP
  bridge and mutate gateway cron state.
- Consolidation and scheduler housekeeping remain gateway-local.

Workspace affinity is transitive for the lifetime of a session tree. A child
cannot request a different host through an MCP argument. A future explicit
cross-workspace spawn would require a separate admission-controlled design.

## Security Properties

The design preserves the following invariants:

1. **One control plane.** The gateway is authoritative for credentials, memory,
   policy, identity, schedules, and durable state.
2. **No ambient gateway authority.** The workspace gets no data-home mount,
   internal HTTP secret, local MCP socket, AWS/SSH/channel credentials, or Coder
   session token.
3. **Least-authority capabilities.** One bearer authorizes one live logical
   session to one fixed service. The gateway ignores client-supplied identity
   and backend selection.
4. **Encrypted and loopback-only transport.** The bridge listener and workspace
   endpoint bind loopback; SSH supplies the only network path.
5. **Fail closed.** Missing, expired, replayed after revocation, malformed, or
   over-budget connections receive no backend. Remote preparation never falls
   back to local agent execution.
6. **Existing policy remains authoritative.** MCP caller stamping, strict
   session-key resolution, governance, sensitive-path checks, permission
   requests, and SEL auditing stay gateway-side.
7. **No secret-bearing diagnostics.** Errors name the phase, workspace, logical
   service, and bounded exit status, but never tokens, upstream headers, raw
   remote stderr, or backend environment.
8. **Bounded blast radius.** Revoking one session leaves sibling sessions and
   pooled backends intact; losing one runtime invalidates only that runtime's
   capabilities.
9. **Gateway-only remote credentials.** Static headers, OAuth discovery,
   registration, authorization attempts, tokens, refresh, and revocation remain
   on the gateway. The workspace receives only the fixed session capability for
   its local relay.
10. **Origin-bound OAuth.** Callback origins are trusted configuration rather
    than request headers, credential identity includes the canonical resource
    and client, and credentials never follow a cross-origin redirect.

The accepted residual risk is that code executing as the workspace user can
read a live capability file and invoke the exact service already granted to that
session until revocation. Preventing that would make MCP unusable to the remote
agent itself. Server-side binding prevents that authority from widening.

## Failure and Recovery

- **Preparation failure:** abort before ACP initialization and report a remote
  host error without stderr contents.
- **Bridge authentication failure:** close the connection, emit a bounded SEL
  denial, and do not start or attach a backend.
- **Backend failure:** preserve the existing MCP gateway restart or per-session
  failure behavior; never run the backend remotely as a fallback.
- **SSH loss:** ACP and all reverse forwards fail together. Revoke capabilities,
  terminate the runtime tree, and enter the existing restart/resume path.
- **Gateway restart:** the in-memory capability registry starts empty. Surviving
  remote relays cannot reconnect; reconstructed sessions receive new bearers.
- **Subagent shared-runtime failure:** retry only by creating a dedicated runtime
  with the inherited Coder affinity. If that fails, fail the child honestly.
- **Remote OAuth requirement:** publish a session-scoped banner event and wait
  for a bounded gateway callback. Timeout, denial, invalid state, discovery
  failure, or exchange failure fails only the affected MCP server with a
  machine-readable redacted error.
- **Expired access token:** serialize refresh for that credential identity and
  retry through the SDK's OAuth flow. A refresh failure may require a new
  gateway authorization but never moves authentication into the workspace.
- **Transport compatibility:** fall back from Streamable HTTP to legacy SSE only
  for an explicit protocol-compatibility response. Authentication, TLS,
  timeout, redirect-policy, and malformed-response failures do not fall back.
- **Session revocation:** close the live HTTP/SSE transport and capability while
  retaining the encrypted OAuth grant for another authorized session.
- **Integration disconnect:** remove the local vault material and attempt remote
  revocation when advertised, even if the remote revocation request fails.
- **Shutdown:** destroy logical sessions first, revoke their capabilities, close
  relay connections, stop isolated backends, remove remote capability files,
  and finally terminate SSH. Every step is idempotent.

## Verification Strategy

### Unit tests

- Affinity selection, persistence, inheritance, and refusal of remote-to-local
  fallback.
- Capability minting, digest lookup, service binding, expiry, revocation,
  post-revocation replay denial, bounds, and redacted errors.
- Agent projection for managed, user, custom-agent, disabled, and malformed MCP
  entries; no secret material in projected JSON, argv, or environment.
- Host-aware transcript probing and resume decisions.
- Shared and dedicated subagent placement, including model and effort overrides.
- Hook relay event/matcher preservation and gateway-side execution.

### Integration tests

- A loopback reverse-forward substitute drives the real relay and bridge without
  requiring a Coder account.
- Managed `kirocrew-core` calls exercise memory add, recall/list, strict caller
  identity, and result delivery through the existing gateway endpoint.
- Poolable and isolated stdio MCP servers preserve process topology and caller
  attribution across parent and sibling subagent sessions.
- Static-auth HTTP and streaming MCP calls originate on the gateway and expose
  no configured header to the relay.
- Streamable HTTP handles JSON and SSE response forms, session identifiers,
  server notifications, cancellation, and reconnect cleanup through the
  official SDK adapter.
- A protocol-compatible rejection selects legacy HTTP+SSE, while 401, 403, TLS,
  timeout, redirect-policy, and malformed-response failures do not.
- A 401 challenge drives protected-resource and authorization-server discovery,
  the existing session-scoped OAuth banner, callback state and PKCE validation,
  encrypted token persistence, initialization resumption, and later refresh.
- Concurrent sessions sharing one credential identity serialize refresh but
  retain independent session capabilities and transport lifecycles.
- Bridge disconnect, gateway restart, ACP restart, resume, cancellation, and
  oversized-frame paths terminate within bounded time.
- Cron dispatch starts a remote parent while scheduler state and cron MCP
  mutations remain gateway-side.

### Security regression tests

- Search projected files, process argv, child environments, logs, exceptions,
  ACP frames, and SEL resources for seeded canary credentials and capabilities.
- Attempt session, server, caller, command, URL, and header pivoting from a
  compromised relay.
- Confirm projected files, relay traffic, ACP frames, errors, logs, and SEL
  records contain no static header, OAuth metadata secret, authorization code,
  PKCE verifier, access token, refresh token, or dynamic client secret.
- Confirm a resource URL or OAuth client identity change cannot reuse an
  existing grant, while a display-name-only change can.
- Reject callback-origin spoofing, state mismatch, non-loopback plain HTTP,
  cross-origin credential redirects, untrusted discovery relationships, and
  over-budget metadata.
- Attempt capability reuse after child completion, parent reset, SSH loss, and
  gateway restart.
- Confirm the bridge binds only loopback and is unreachable through the
  dashboard and Tailscale listeners.
- Run the existing denied-command, sensitive-path, governance, MCP statelessness,
  and harness-parity gates unchanged.

### Optional live smoke test

Against the dogfood Coder workspace, verify one parent turn, immediate
`learn_add`, a later recall, a managed MCP call, a user stdio MCP call, a shared
subagent, a dedicated-model subagent, cron-triggered session placement, gateway
restart plus resume, an OAuth-protected HTTP MCP authorization and refresh, and
capability cleanup. The test records identifiers and outcomes only; it never
captures tokens or agent configuration containing credentials.

## Delivery Sequence

1. Introduce session affinity and host-aware resume without changing local
   behavior.
2. Add the capability registry, loopback ingress, reverse forwarding, and the
   credential-free relay protocol.
3. Bridge managed and user stdio MCP servers, restoring immediate memory writes
   and Crew tools.
4. Make shared and dedicated subagents inherit Coder affinity and project custom
   agents per session.
5. Add the gateway hook relay and verify permission/governance parity.
6. Add the SDK-backed gateway Streamable HTTP adapter with narrowly triggered
   legacy SSE compatibility fallback.
7. Add the gateway OAuth broker, trusted callback route, encrypted SDK token
   storage, banner events, refresh serialization, and integration disconnect.
8. Run the local parity matrix and the optional live Coder and OAuth smoke
   tests.

Each sequence item must leave local hosting unchanged and keep remote hosting
fail-closed. The implementation updates the authoritative ACP, provider, MCP,
memory/hooks, subagent, session, cron, security, and testing specifications in
the same commits as the behavior they document.

## Acceptance Criteria

The design is complete when all of the following are true:

- A parent remote session can call managed and user MCP tools without receiving
  backend commands or credentials.
- Streamable HTTP and legacy HTTP+SSE MCP servers connect from the gateway while
  the workspace uses only the local stdio relay.
- An OAuth-protected remote MCP server can be authorized through the existing
  dashboard banner and trusted gateway callback, then reconnect and refresh
  without putting OAuth material in the workspace.
- `learn_add` changes gateway memory immediately and a later remote turn recalls
  the lesson through the ordinary context path.
- Parent, shared subagent, and dedicated subagent MCP calls carry their own
  verified logical caller identities.
- Every descendant of a remote parent executes in the same Coder workspace;
  unrelated gateway work remains local.
- Cron remains gateway-owned and can dispatch a remote session tree.
- Remote native resume reuses the Coder transcript and refreshes bridge
  capabilities without duplicating thread history.
- Gateway and user kiro-cli hooks run at the intended gateway boundary.
- Revoked capabilities, bridge loss, or remote preparation failure cannot widen
  access or trigger local execution.
- No credential or raw capability appears in projected configuration, argv,
  environment, logs, ACP traffic, or audit metadata.
- Session revocation closes active HTTP/SSE transport without deleting the
  reusable gateway OAuth grant; disconnecting the integration deletes the local
  grant and attempts remote revocation.
- OAuth, transport compatibility, redirect, and discovery failures remain
  bounded to one MCP server and cannot cause sandbox-side networking, secret
  forwarding, or local ACP fallback.
