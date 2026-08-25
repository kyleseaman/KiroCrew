# Coder Gateway MCP Proxy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. This repository forbids proactive subagent dispatch and commits, so execution is inline and every commit step is replaced by an uncommitted review checkpoint.

**Goal:** Give Coder-hosted Kiro sessions a conservative, session-scoped proxy to gateway-owned stdio, Streamable HTTP, legacy SSE, and OAuth MCP servers so Crew memory tools, native resume, and all descendant subagents behave like local sessions without placing credentials in Coder.

**Architecture:** A loopback-only TCP proxy on the gateway mints one bearer per logical session and MCP server. A stdlib relay copied into Coder authenticates through an SSH reverse forward, then carries MCP stdio either to a gateway-spawned backend or to an SDK-backed gateway HTTP transport selected exclusively from local configuration. The gateway owns OAuth discovery, PKCE, callback state, encrypted tokens, refresh, and revocation; remote session affinity is inherited by both shared and dedicated subagents, while script-hook proxying remains a separate follow-on milestone.

**Tech Stack:** Python 3.10+, asyncio streams and subprocesses, official MCP Python SDK 2.1.x, httpx2, aiohttp, AES-GCM `SecretVault`, ACP JSON-RPC, MCP stdio/Streamable HTTP/legacy SSE, Coder SSH reverse forwarding, pytest/pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-08-24-coder-remote-session-parity-design.md`

## Global Constraints

- The gateway remains authoritative for MCP processes, credentials, memory, identity, policy, and durable state.
- The workspace receives no `KIROCREW_HOME`, internal HTTP secret, MCP socket, Coder session token, AWS/SSH/channel credential, backend command, backend environment, or raw credential.
- One capability authorizes one logical session to one fixed MCP server and is revoked on session teardown or runtime death.
- Missing bridge state, unsupported transports, invalid configuration, or remote preparation failure fails closed; a remote session never falls back to gateway-local ACP execution.
- The workspace receives no remote URL, configured header, OAuth metadata, registration, authorization code, PKCE verifier, access token, refresh token, or dynamic client secret.
- Streamable HTTP is preferred; legacy SSE is attempted only after an observed 404 or 405 compatibility response to the initial modern request, never after auth, TLS, timeout, redirect-policy, or malformed-response failure.
- OAuth and configured remote MCP URLs require HTTPS except for exact gateway-loopback hosts; callback origins come from configured dashboard or validated Tailscale state, never request headers.
- OAuth grants are keyed by canonical resource URL and client identity, encrypted with `SecretVault`, retained across session revocation, and deleted when the integration is removed.
- Remote HTTP targets are capped at 4 KiB per URL, 32 configured headers, 256 bytes per header name, 8 KiB per header value, and 64 KiB of configured headers in total.
- Relayed MCP JSON lines and non-streaming HTTP bodies are capped at 8 MiB; OAuth callback queries and authorization URLs are capped at 8 KiB; the broker holds at most 64 live attempts for 5 minutes each.
- HTTP connect/write/pool timeouts are 10 seconds, streaming reads are 300 seconds, authorization waits are 5 minutes, and redirects are capped at 3.
- The Coder execution host remains positively limited to `ACP_BACKEND_KIRO`; harness-parity identity checks remain positive.
- All subprocess, signal, owner-only-file, and liveness operations route through `platform_compat` and existing sandbox helpers.
- No user-facing English UI strings are added. New backend non-2xx JSON, if any, includes a machine-readable `code`.
- Tests use `tmp_path`, bind port `0`, stop every task/socket/process in `finally`, and never touch the operator's Coder workspace, data home, or `~/.kiro`.
- Do not commit or push during execution without explicit user authorization.

---

### Task 1: Capability and Target Model (complete)

**Files:**
- Create: `src/kiro_crew/mcp_gateway/remote_proxy.py`
- Create: `test/test_remote_mcp_proxy.py`

**Interfaces:**
- Produces: `RemoteMcpTarget(command: str, args: tuple[str, ...], env: Mapping[str, str], cwd: str, first_party: bool)`.
- Produces: `RemoteMcpGrant(grant_id: str, token: str, capability_id: str)`.
- Produces: `RemoteMcpCapabilityRegistry.mint(session_key: str, target: RemoteMcpTarget) -> RemoteMcpGrant`, `claim(token: str) -> RemoteMcpTargetLease | None`, `release(lease)`, `revoke_grant(grant_id: str)`, and `revoke_all()`.
- Consumes later: the TCP proxy and Coder host use grants without accepting a server selector from the remote peer.

- [ ] **Step 1: Write failing registry tests**

```python
def test_capability_is_bound_to_one_session_and_target():
    registry = RemoteMcpCapabilityRegistry()
    target = RemoteMcpTarget("/usr/bin/server", (), {}, "/work", False)
    grant = registry.mint("dashboard:one", target)

    lease = registry.claim(grant.token)

    assert lease is not None
    assert lease.session_key == "dashboard:one"
    assert lease.target is target
    assert registry.claim("wrong-token") is None
```

Add separate tests proving the registry stores token digests rather than raw tokens, refuses a second simultaneous lease for one capability, allows reconnect after `release`, and denies every claim after `revoke_grant` or `revoke_all`.

- [ ] **Step 2: Run the tests and observe the missing-module failure**

Run: `pytest -n0 test/test_remote_mcp_proxy.py -q`

Expected: collection fails because `kiro_crew.mcp_gateway.remote_proxy` does not exist.

- [ ] **Step 3: Implement the minimal registry**

Use `secrets.token_urlsafe(32)` for the bearer, SHA-256 for the stored lookup key, a lock-protected in-memory record, and an opaque random `capability_id` safe for filenames. Do not add persistence: a gateway restart intentionally invalidates every capability.

```python
@dataclass(frozen=True)
class RemoteMcpTarget:
    command: str
    args: tuple[str, ...]
    env: Mapping[str, str]
    cwd: str
    first_party: bool = False

@dataclass(frozen=True)
class RemoteMcpGrant:
    grant_id: str
    token: str
    capability_id: str
```

- [ ] **Step 4: Run the registry tests**

Run: `pytest -n0 test/test_remote_mcp_proxy.py -q`

Expected: registry tests pass with no sockets or subprocesses left alive.

- [ ] **Step 5: Review checkpoint**

Inspect `git diff --check` and the two files. Leave changes uncommitted.

### Task 2: Loopback Gateway Proxy (complete)

**Files:**
- Modify: `src/kiro_crew/mcp_gateway/remote_proxy.py`
- Modify: `test/test_remote_mcp_proxy.py`

**Interfaces:**
- Consumes: `RemoteMcpCapabilityRegistry` and `RemoteMcpTarget` from Task 1.
- Produces: `RemoteMcpProxy.start()`, `local_port`, `mint()`, `revoke_grant()`, and `close()`.
- Produces: authentication frame `{"version": 1, "token": "<bearer>"}\n`, followed by unmodified MCP stdio bytes.

- [ ] **Step 1: Write failing real-stream tests**

Create a backend script below `tmp_path` that echoes JSON lines. Start the real proxy on port `0`, mint a grant, connect with `asyncio.open_connection`, authenticate, and assert a literal MCP request returns byte-for-byte. Add tests for malformed JSON, oversized authentication, wrong tokens, concurrent reuse, backend spawn failure, and proxy shutdown while a backend is live.

```python
reader, writer = await asyncio.open_connection("127.0.0.1", proxy.local_port)
writer.write(json.dumps({"version": 1, "token": grant.token}).encode() + b"\n")
writer.write(b'{"jsonrpc":"2.0","id":7,"method":"tools/list"}\n')
await writer.drain()
assert await reader.readline() == b'{"jsonrpc":"2.0","id":7,"method":"tools/list"}\n'
```

- [ ] **Step 2: Run the focused tests and observe missing proxy behavior**

Run: `pytest -n0 test/test_remote_mcp_proxy.py -q`

Expected: failures name the absent `RemoteMcpProxy` lifecycle and transport behavior.

- [ ] **Step 3: Implement bounded proxy transport**

Bind only `127.0.0.1` with `asyncio.start_server(..., port=0, limit=<owned constant>)`. Read one bounded authentication line, claim the registry lease, spawn the trusted target through `sandboxed_spawn_argv` plus `create_subprocess_limited`, add `KIROCREW_SESSION_KEY` only after environment scrubbing, and pump bytes in both directions with backpressure. Drain redacted stderr and terminate the whole child tree in `finally` using `platform_compat.kill_process_tree_async`.

The proxy never accepts `server`, `command`, `args`, `env`, `cwd`, or caller fields from the client. Authentication errors close the socket without spawning.

- [ ] **Step 4: Run transport tests in serial and parallel**

Run: `pytest -n0 test/test_remote_mcp_proxy.py -q && pytest test/test_remote_mcp_proxy.py -q`

Expected: both invocations pass and the root residue guard reports no child, socket, or temp residue.

- [ ] **Step 5: Review checkpoint**

Run `python3 scripts/check_subprocess_encoding.py` and inspect the proxy diff. Leave changes uncommitted.

### Task 3: Credential-Free Workspace Relay (complete)

**Files:**
- Create: `src/kiro_crew/acp/remote_mcp_relay.py`
- Create: `test/test_remote_mcp_relay.py`

**Interfaces:**
- Consumes: proxy authentication frame from Task 2.
- Produces: standalone stdlib CLI `python3 remote_mcp_relay.py --port N --cap-file PATH`.
- Produces: credential-free `--unsupported-code CODE` mode that answers MCP
  initialization with a deterministic transport-unavailable error and opens no socket.
- Constraint: the module has no `kiro_crew` imports because its source file is copied and executed in Coder.

- [ ] **Step 1: Write a failing subprocess integration test**

Start a real loopback test server, create a `0600` capability file below `tmp_path`, then run the relay subprocess with `cwd=tmp_path`. Send a literal MCP line on stdin and assert it crosses the connection after the authentication frame and returns on stdout. Assert the bearer is absent from argv and stderr. Run unsupported mode with no proxy and assert an `initialize` request receives a JSON-RPC error whose data code is `remote_mcp_http_unavailable`.

- [ ] **Step 2: Run the relay test and observe the missing script failure**

Run: `pytest -n0 test/test_remote_mcp_relay.py -q`

Expected: collection or spawn fails because the relay does not exist.

- [ ] **Step 3: Implement the standalone relay**

Use `asyncio.open_connection("127.0.0.1", port)`, read the owner-only token file, send the versioned authentication frame, then run stdin-to-socket and socket-to-stdout pumps. Bound the token file and authentication data, never log token contents, and exit nonzero when the proxy disconnects with outstanding transport work. In unsupported mode, parse bounded JSON-RPC lines and answer requests locally without reading a capability file or opening a network connection.

- [ ] **Step 4: Run the relay tests**

Run: `pytest -n0 test/test_remote_mcp_relay.py -q && pytest test/test_remote_mcp_relay.py -q`

Expected: both pass with subprocess cleanup proven by the test `finally` blocks.

- [ ] **Step 5: Review checkpoint**

Confirm `rg '^from kiro_crew|^import kiro_crew' src/kiro_crew/acp/remote_mcp_relay.py` returns no matches. Leave changes uncommitted.

### Task 4: Remote MCP Target Resolution and Projection (complete)

**Files:**
- Modify: `src/kiro_crew/acp/session_host.py`
- Modify: `test/test_acp_session_host.py`

**Interfaces:**
- Consumes: a materialized local agent mapping and gateway environment.
- Produces: `resolve_remote_mcp_targets(spec, *, local_cwd, session_key) -> dict[str, RemoteMcpTarget]` for stdio; Task 10 adds the HTTP target union without changing stdio behavior.
- Produces: `project_remote_agent_spec(spec, available_mcp_servers=frozenset(...))` preserving only bridge-backed `@server` references.
- Produces: relay ACP entries carrying only name, relay command, relay arguments, and non-secret tool policy fields. Until Task 10, enabled HTTP/SSE entries receive unsupported-mode relay entries and no capability.

- [ ] **Step 1: Replace the MCP-free projection test with failing capability-backed cases**

Use a fixture containing managed stdio, user stdio with a seeded secret env value, disabled stdio, HTTP, malformed, and dangling `@server/tool` references. Assert supported stdio and enabled HTTP names survive in `tools`, with HTTP mapped to unsupported mode and no capability; assert no backend command, env value, URL, header, or seeded secret appears anywhere in the serialized projection or relay entry.

- [ ] **Step 2: Run the host tests and observe the old stripping behavior**

Run: `pytest -n0 test/test_acp_session_host.py -q`

Expected: the new `@kirocrew-core` and user-stdio expectations fail because the current projection removes every MCP reference.

- [ ] **Step 3: Implement strict target resolution**

Accept only one stdio transport: non-empty string `command`, string-list `args`, string mapping `env`, and no `url`. Skip `disabled: true`, HTTP/SSE, mixed, and malformed entries. Resolve commands against the gateway MCP search path; sanitize declared environment with existing helpers; preserve supported `autoApprove`, `disabledTools`, and timeout fields only in the relay-facing ACP entry. Do not return original specs to remote callers.

- [ ] **Step 4: Run host tests**

Run: `pytest -n0 test/test_acp_session_host.py -q`

Expected: projection and secret-canary tests pass.

- [ ] **Step 5: Review checkpoint**

Run `python3 scripts/check_harness_parity.py` with `HARNESS_BASE_REF=origin/main`; leave changes uncommitted.

### Task 5: Coder Reverse Forward and Remote Session Files (complete)

**Files:**
- Modify: `src/kiro_crew/acp/session_host.py`
- Modify: `test/test_acp_session_host.py`

**Interfaces:**
- Consumes: `RemoteMcpProxy`, relay source path, grants, and current Coder transport environment.
- Produces: `CoderWorkspaceSessionHost.start_bridge()`, bridge-aware `spawn_argv()`, `prepare_session_capabilities()`, `revoke_session_grant()`, `remote_session_file()`, `session_file_exists()`, and `close()`.

- [ ] **Step 1: Write failing host lifecycle tests**

Assert the host starts a loopback proxy before building argv, adds exactly one `--remote-forward <remote-port>:127.0.0.1:<local-port>`, streams the standalone relay plus token files with `umask 077`, and never sends gateway environment or target details. Add a remote transcript probe test that accepts only a validated ACP session ID and returns no file contents.

- [ ] **Step 2: Run host tests and observe missing bridge methods**

Run: `pytest -n0 test/test_acp_session_host.py -q`

Expected: failures name the absent bridge lifecycle and host-aware transcript contract.

- [ ] **Step 3: Implement host preparation**

Create one random runtime directory below the remote user's `~/.kiro/crew/remote-runtimes` through a fixed positional shell script. Stream the relay and agent projection with owner-only modes. For each logical session, stream token files named only by capability ID. Allocate a high remote loopback port with bounded collision probes and include the reverse forward in the same `coder ssh` process as ACP. `close()` revokes proxy grants first, then removes the remote runtime directory best-effort without exposing remote stderr.

- [ ] **Step 4: Run host lifecycle tests**

Run: `pytest -n0 test/test_acp_session_host.py -q`

Expected: tests pass and canary values are absent from argv, projected JSON, and exception text.

- [ ] **Step 5: Review checkpoint**

Run `git diff --check` and inspect every shell argument for positional quoting. Leave changes uncommitted.

### Task 6: ACP Session Creation, Resume, and Revocation (complete)

**Files:**
- Modify: `src/kiro_crew/acp/runtime.py`
- Modify: `src/kiro_crew/providers/acp.py`
- Modify: `test/test_acp_runtime.py`
- Modify: `test/test_acp_provider.py`
- Modify: `test/test_acp_session_host.py`

**Interfaces:**
- Consumes: Coder host bridge methods from Task 5.
- Changes: `AcpRuntime.create_session(..., session_key: str | None = None)` and `load_session(..., session_key: str | None = None)`.
- Produces: per-session MCP relay injection for both `session/new` and `session/load`, grant binding to ACP session ID, revocation from `terminate_session`, and host closure from runtime `kill`.

- [ ] **Step 1: Write failing runtime tests**

Assert a remote `session/new` receives bridge entries instead of `[]`; a local `session/new` retains its current pooled-server behavior; failed creation revokes the provisional grant; successful creation binds it to the returned ACP session ID; termination revokes it. Assert remote `session/load` uses the validated workspace transcript path and newly rotated bridge entries rather than statting or sending a gateway path.

- [ ] **Step 2: Run focused runtime/provider tests**

Run: `pytest -n0 test/test_acp_runtime.py test/test_acp_provider.py test/test_acp_session_host.py -q`

Expected: new remote MCP and resume tests fail while existing local parity tests remain green.

- [ ] **Step 3: Implement runtime lifecycle changes**

Move remote agent materialization into a reusable per-agent helper invoked before every remote `session/new` and `session/load`. Ask the host for relay entries with the trusted logical session key, bind the grant after the ACP response, and revoke in every exception and teardown path. In `AcpProvider`, pass its configured session key to runtime create/load and ask the host whether the remote transcript exists instead of using `kiro_sessions_dir()`.

- [ ] **Step 4: Run focused tests**

Run: `pytest -n0 test/test_acp_runtime.py test/test_acp_provider.py test/test_acp_session_host.py -q`

Expected: all pass.

- [ ] **Step 5: Review checkpoint**

Run the ACP client, provider, and harness-parity focused gates. Leave changes uncommitted.

### Task 7: Workspace Affinity for Every Subagent (complete)

**Files:**
- Modify: `src/kiro_crew/session.py`
- Modify: `src/kiro_crew/subagent.py`
- Modify: `src/kiro_crew/config/loader.py`
- Modify: `test/test_session_sharing.py`
- Modify: `test/test_subagent_spawn_host_pin.py`
- Modify: `test/test_config_loader.py`

**Interfaces:**
- Consumes: parent `AcpRuntime.session_host` and session-key-aware create-session from Task 6.
- Produces: immutable session execution affinity query on `SessionManager` and an explicit inherited `session_host` factory argument for dedicated children.

- [ ] **Step 1: Write failing placement tests**

Cover both branches:

```python
async def test_shared_child_uses_parent_remote_runtime_and_its_own_session_key(...):
    await manager._create_shared_session(info, "subagent:child", "kirocrew")
    runtime.create_session.assert_awaited_once_with(
        cwd="/home/coder/workspace", agent="kirocrew", session_key="subagent:child"
    )
```

Add a dedicated-model child test proving its provider receives the parent's Coder host, plus a failure test proving a dead remote shared runtime may fall back only to a dedicated runtime in the same workspace, never `LocalSessionHost`.

- [ ] **Step 2: Run focused subagent tests and observe local fallback**

Run: `pytest -n0 test/test_session_sharing.py test/test_subagent_spawn_host_pin.py test/test_config_loader.py -q`

Expected: dedicated child placement fails because the current factory derives remote hosting from agent name/environment and the fallback is local.

- [ ] **Step 3: Implement inherited affinity**

Expose the live parent host through `SessionManager` without serializing capability state. Thread an optional explicit `session_host` through the provider factory; explicit inherited affinity wins over environment discovery. Pass the child logical session key on shared creation. Change the shared-runtime failure branch to request a dedicated provider with inherited affinity and raise if that remote creation fails.

- [ ] **Step 4: Run focused subagent tests**

Run: `pytest -n0 test/test_session_sharing.py test/test_subagent_spawn_host_pin.py test/test_config_loader.py -q`

Expected: shared and dedicated placement tests pass on the fake host without touching Coder.

- [ ] **Step 5: Review checkpoint**

Run the subagent host-pin audit and harness-parity gate. Leave changes uncommitted.

### Task 8: Memory and MCP End-to-End Proxy Proof (complete)

**Files:**
- Modify: `test/test_remote_mcp_proxy.py`
- Modify: `test/test_acp_session_host.py`
- Modify: `test/test_resolve_session_key.py`

**Interfaces:**
- Consumes: complete stdio proxy, relay, remote session injection, and logical caller identity.
- Produces: integration evidence that `kirocrew-core`-style strict session identity and a user stdio server work without credentials crossing the boundary.

- [ ] **Step 1: Write failing end-to-end tests**

Run a real test MCP process through the real relay and proxy. The process echoes its `KIROCREW_SESSION_KEY` only in a controlled test result; assert it equals the child's logical key. Add a managed-core-shaped fake that handles `learn_add` and records the gateway request, then assert the remote side receives the ordinary MCP result while the seeded internal secret is absent from relay files and frames.

- [ ] **Step 2: Run the end-to-end tests**

Run: `pytest -n0 test/test_remote_mcp_proxy.py test/test_resolve_session_key.py -q`

Expected: any remaining identity or environment propagation gap fails with a literal mismatch.

- [ ] **Step 3: Make only the minimal integration corrections**

Correct environment ordering, caller classification, or teardown behavior in the owning modules. Do not add a second memory API: the expected path is still remote relay → gateway MCP subprocess → existing gateway endpoint → existing memory writer.

- [ ] **Step 4: Re-run serial and parallel integration tests**

Run: `pytest -n0 test/test_remote_mcp_proxy.py test/test_resolve_session_key.py -q && pytest test/test_remote_mcp_proxy.py test/test_resolve_session_key.py -q`

Expected: both pass with no temp, process, socket, or repository residue.

- [ ] **Step 5: Review checkpoint**

Inspect the complete diff for duplicate memory paths or secret-bearing diagnostics. Leave changes uncommitted.

### Task 9: Authoritative Specs and Verification (complete; live smoke optional)

**Files:**
- Modify: `docs/system-specs/modules/acp-client.md`
- Modify: `docs/system-specs/modules/providers.md`
- Modify: `docs/architecture/mcp.md`
- Modify: `docs/system-specs/modules/memory-skills-hooks.md`
- Modify: `docs/system-specs/modules/session.md`
- Modify: `docs/system-specs/modules/subagent.md`
- Modify: `docs/system-specs/modules/learn-cron-dashboard.md`
- Modify: `docs/system-specs/common/testing-conventions.md` only if the tests establish a new reusable convention.

**Interfaces:**
- Consumes: verified implementation behavior from Tasks 1–8.
- Produces: current-behavior contracts that distinguish shipped stdio proxy parity from the then-unimplemented HTTP/OAuth and hook milestones; Task 16 updates them again after HTTP/OAuth lands.

- [x] **Step 1: Update the owning specifications**

Document the loopback proxy, reverse SSH forwarding, capability scope, target resolution, secret boundary, remote transcript path, child affinity, cron ownership, failure behavior, and explicit HTTP/OAuth/hook limitations. Remove the POC statements that remote hosting is MCP-free only where the implementation now disproves them.

- [x] **Step 2: Run focused formatting and static checks**

Run:

```bash
black --target-version py310 <changed-python-files>
isort <changed-python-files> <changed-test-files>
python3 scripts/check_black_formatting.py
python3 scripts/check_subprocess_encoding.py
HARNESS_BASE_REF=origin/main python3 scripts/check_harness_parity.py
BRAND_BASE_REF=origin/main python3 scripts/check_brand_name.py
scripts/docs-lint.sh
flake8 <changed-python-files> <changed-test-files>
mypy --platform linux <changed-python-files>
```

Expected: every command exits zero.

- [x] **Step 3: Run focused regression tests**

Run all changed test modules plus the existing MCP gateway, ACP runtime/provider, session sharing, subagent, memory caller-identity, and Coder host suites with the repository's xdist defaults.

Expected: zero failures.

- [x] **Step 4: Run the full backend gate**

Run: `python -m pytest`

Expected: zero new failures. If the known cached macOS RSS failure recurs, reproduce it deterministically and report it separately rather than weakening or rerunning the assertion.

- [ ] **Step 5: Optional live dogfood smoke test**

Without printing any token, start one Coder-hosted session and verify: `kirocrew-core` initializes through the proxy; `learn_add` writes a canary lesson; a later turn recalls it; a shared child and a dedicated-model child both report the Coder workspace; gateway restart rotates capabilities and resumes the remote transcript; teardown removes the runtime directory. Remove only the canary lesson and test session state created by this smoke test.

- [x] **Step 6: Final review checkpoint**

Run `git diff --check`, `git status --short`, and a secret-canary scan of the complete diff. Present the uncommitted result for review. Do not commit or push without explicit authorization.

### Task 10: Trusted HTTP Target Model and SDK Dependency

**Files:**
- Modify: `setup.cfg`
- Modify: `src/kiro_crew/mcp_gateway/remote_proxy.py`
- Modify: `src/kiro_crew/acp/session_host.py`
- Modify: `test/test_remote_mcp_proxy.py`
- Modify: `test/test_acp_session_host.py`

**Interfaces:**
- Produces: `RemoteHttpMcpTarget(server_name: str, url: str, headers: Mapping[str, str], scopes: tuple[str, ...], client_id: str)`.
- Produces: `RemoteMcpServiceTarget = RemoteMcpTarget | RemoteHttpMcpTarget` as the capability registry's fixed target type.
- Produces: `resolve_remote_http_mcp_targets(spec, *, session_key) -> dict[str, RemoteHttpMcpTarget]`.
- Consumes later: `RemoteMcpProxy` dispatches the HTTP member to `GatewayMcpHttpAdapter`; the sandbox still receives only the existing relay argv and bearer file.

- [ ] **Step 1: Write failing target-resolution tests**

Add literal configurations covering a valid HTTPS URL, exact loopback HTTP, non-loopback HTTP, userinfo, query, fragment, mixed command+URL, malformed headers/scopes/client ID, disabled entries, and a seeded static header. The production break these tests catch is accepting an ambiguous or secret-bearing remote target, or leaking it into projected state.

```python
targets = resolve_remote_http_mcp_targets(
    {
        "mcpServers": {
            "linear": {
                "url": "https://mcp.linear.example/mcp",
                "headers": {"X-Workspace": "gateway-only-canary"},
                "scopes": ["read", "write"],
                "clientId": "kiro-crew",
            }
        }
    },
    session_key="dashboard:one",
)

assert targets["linear"] == RemoteHttpMcpTarget(
    server_name="linear",
    url="https://mcp.linear.example/mcp",
    headers={"X-Workspace": "gateway-only-canary"},
    scopes=("read", "write"),
    client_id="kiro-crew",
)
assert "gateway-only-canary" not in json.dumps(projected_spec)
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `pytest -n0 test/test_remote_mcp_proxy.py test/test_acp_session_host.py -q`

Expected: failures name the absent `RemoteHttpMcpTarget` and HTTP resolver; the existing unsupported relay behavior stays green.

- [ ] **Step 3: Add the pinned SDK and minimal target union**

Add `mcp>=2.1,<2.2` to `install_requires`; this pins the reviewed 2.1 API while permitting patch fixes and preserves Python 3.10 support. Keep `RemoteMcpTarget` as the stdio type to avoid widening existing callers. Validate HTTP targets with `urllib.parse.urlsplit`: require a safe server name, no command, no userinfo/query/fragment, HTTPS except exact `localhost`/`127.0.0.1`/`::1`, the global 4 KiB URL and 32/256-byte/8-KiB/64-KiB header bounds, and the existing `mcp_oauth_scopes` / `mcp_oauth_client_id` helpers.

Change the registry annotations only:

```python
@dataclass(frozen=True)
class RemoteHttpMcpTarget:
    server_name: str
    url: str
    headers: Mapping[str, str]
    scopes: tuple[str, ...] = ()
    client_id: str = ""


RemoteMcpServiceTarget = RemoteMcpTarget | RemoteHttpMcpTarget
```

Mint capabilities and relay entries for both target types. Delete the HTTP name from the unsupported-name set only after its trusted target resolves; malformed HTTP entries continue to fail closed locally.

- [ ] **Step 4: Run dependency and target tests GREEN**

Run: `.venv/bin/python -m pip install -e . && pytest -n0 test/test_remote_mcp_proxy.py test/test_acp_session_host.py -q`

Expected: the SDK imports as version 2.1.x, valid HTTP servers receive ordinary capability-backed relay entries, invalid servers receive no target, and no URL/header appears in workspace artifacts.

- [ ] **Step 5: Review checkpoint**

Run `pytest -n0 test/test_pip_deps_consistency.py test/test_dep_sync.py -q`, then build a wheel with `.venv/bin/python -m build --wheel`. Inspect its metadata for `mcp>=2.1,<2.2`; leave changes uncommitted.

### Task 11: Raw Streamable HTTP Adapter

**Files:**
- Create: `src/kiro_crew/mcp_gateway/remote_http.py`
- Create: `test/test_remote_mcp_http.py`
- Modify: `src/kiro_crew/mcp_gateway/remote_proxy.py`
- Modify: `test/test_remote_mcp_proxy.py`

**Interfaces:**
- Produces: `GatewayMcpHttpAdapter.run(target: RemoteHttpMcpTarget, session_key: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None`.
- Produces: `RemoteMcpHttpError(code: str)` whose public codes are `remote_mcp_transport_failed`, `remote_mcp_oauth_required`, and `remote_mcp_oauth_failed` and whose string form contains no upstream body or credential.
- Consumes: `mcp.client.streamable_http.streamable_http_client`, `mcp_types.jsonrpc_message_adapter`, and `mcp.shared.message.SessionMessage`.
- Preserves: raw JSON-RPC IDs and bodies while adding only HTTP transport metadata derived from the negotiated MCP messages.

- [ ] **Step 1: Write a failing real Streamable HTTP bridge test**

Start an aiohttp server on port `0` whose POST handler returns a hand-written initialize result and an SSE-framed `tools/list` result. Drive the real TCP proxy and real workspace relay with literal JSON-RPC lines. Assert initialize, notifications, requests, server notifications, and cancellation cross the adapter, and that the server receives the static header while the relay does not.

```python
relay_writer.write(
    b'{"jsonrpc":"2.0","id":7,"method":"initialize",'
    b'"params":{"protocolVersion":"2025-06-18","capabilities":{},'
    b'"clientInfo":{"name":"kiro-cli","version":"test"}}}\n'
)
await relay_writer.drain()
reply = json.loads(await relay_reader.readline())
assert reply["id"] == 7
assert reply["result"]["protocolVersion"] == "2025-06-18"
```

Add separate cases for malformed relay JSON, an upstream exception, an over-8-MiB line, an over-8-MiB JSON or OAuth metadata body, EOF, adapter cancellation, and capability revocation. Each test closes its aiohttp runner, proxy, streams, and relay subprocess in `finally`.

- [ ] **Step 2: Run HTTP tests and verify RED**

Run: `pytest -n0 test/test_remote_mcp_http.py test/test_remote_mcp_proxy.py -q`

Expected: collection fails for the absent adapter, then behavior tests fail until the proxy dispatches HTTP targets.

- [ ] **Step 3: Implement the minimal SDK stream adapter**

Frame relay input with chunked `StreamReader.read` rather than `readline`, so the existing 8 KiB authentication limit stays intact while MCP lines may reach the separate 8 MiB ceiling. Parse each complete line with `jsonrpc_message_adapter.validate_json`, wrap it in `SessionMessage`, and send it to the SDK transport. Serialize inbound `SessionMessage.message` with aliases and unset fields excluded. Forward SDK `Exception` items as a bounded JSON-RPC error tied to the triggering request rather than serializing the exception.

Track the negotiated protocol and attach `ClientMessageMetadata.headers` on subsequent HTTP writes. Derive `MCP-Protocol-Version`, `Mcp-Method`, and `Mcp-Name` from the message using the SDK constants. Cache `x-mcp-header` mappings only from real `tools/list` responses and derive `Mcp-Param-*` values with the SDK helper; never accept a header name or value directly from relay metadata.

```python
message = jsonrpc_message_adapter.validate_json(line, by_name=False)
metadata = ClientMessageMetadata(headers=self._headers_for(message))
await sdk_write.send(SessionMessage(message, metadata=metadata))
```

Construct `httpx2.AsyncClient` with 10-second connect/write/pool timeouts, a 300-second read timeout, TLS verification, `follow_redirects=True`, and `max_redirects=3`. Inject configured headers in a request hook only when the outgoing request targets the exact configured MCP resource. A response hook rejects any cross-origin redirect from a request carrying Authorization, Cookie, or a configured header and wraps non-streaming response bodies in an 8 MiB capped `AsyncByteStream` before the SDK reads them. Never include URL query, headers, response body, or exception text in the public error.

Update `RemoteMcpProxy._handle_connection` to branch positively on `RemoteHttpMcpTarget`; stdio retains the current subprocess path byte-for-byte. Both branches keep the same capability lease and active-task cancellation lifecycle.

- [ ] **Step 4: Run Streamable HTTP tests GREEN**

Run: `pytest -n0 test/test_remote_mcp_http.py test/test_remote_mcp_proxy.py -q && pytest test/test_remote_mcp_http.py test/test_remote_mcp_proxy.py -q`

Expected: serial and xdist runs pass with no socket, task, subprocess, or temp residue.

- [ ] **Step 5: Review checkpoint**

Confirm seeded headers and URLs are absent from relay argv/files/frames and all exception strings. Leave changes uncommitted.

### Task 12: Narrow Legacy SSE Compatibility

**Files:**
- Modify: `src/kiro_crew/mcp_gateway/remote_http.py`
- Modify: `test/test_remote_mcp_http.py`

**Interfaces:**
- Produces: one first-message transport-selection state machine inside `GatewayMcpHttpAdapter`.
- Consumes: `mcp.client.sse.sse_client` only after a recorded initial Streamable HTTP POST status of 404 or 405.
- Guarantees: the original initialize message is replayed once to legacy SSE and no modern error is exposed before selection completes.

- [ ] **Step 1: Write failing compatibility tests**

Build a real aiohttp legacy server whose POST to the configured URL returns 404, whose GET emits one `endpoint` event, and whose endpoint POST returns a `message` event. Assert the original request receives one successful reply and the legacy endpoint sees it exactly once. Add independent tests proving 405 falls back while 400, 401, 403, 429, 500, TLS failure, timeout, malformed JSON, and cross-origin redirect never call the legacy GET handler.

```python
assert modern_posts == 1
assert legacy_gets == 1
assert legacy_posts == 1
assert json.loads(await relay_reader.readline())["id"] == 7
```

- [ ] **Step 2: Run compatibility tests and verify RED**

Run: `pytest -n0 test/test_remote_mcp_http.py -k 'legacy or fallback' -q`

Expected: 404/405 cases return the modern transport error because no selection state exists.

- [ ] **Step 3: Implement first-request selection**

Use an httpx2 response hook to record only the configured endpoint's initial POST status. Buffer the first outbound MCP message until the first SDK read resolves. When and only when the status is 404 or 405 and the SDK result is the transport-generated failure for that request, close the modern context, open `sse_client` with the same bounded static-header and redirect policy, resend the buffered message, and commit the connection to legacy mode. Every other outcome commits to modern mode or fails.

Keep selection state connection-local. Do not cache legacy mode across sessions: a server upgrade should take effect on the next connection without gateway restart.

- [ ] **Step 4: Run all HTTP transport tests GREEN**

Run: `pytest -n0 test/test_remote_mcp_http.py -q && pytest test/test_remote_mcp_http.py -q`

Expected: all modern, legacy, and no-fallback matrix cases pass.

- [ ] **Step 5: Review checkpoint**

Mutation-check the fallback predicate by temporarily changing `{404, 405}` to `{401}` and confirm the 401 no-fallback test fails, then restore with `apply_patch`. Leave changes uncommitted.

### Task 13: Vault-Backed OAuth Broker

**Files:**
- Create: `src/kiro_crew/mcp_gateway/oauth.py`
- Create: `test/test_remote_mcp_oauth.py`
- Modify: `src/kiro_crew/mcp_gateway/remote_http.py`
- Modify: `test/test_remote_mcp_http.py`

**Interfaces:**
- Produces: `OAuthCredentialIdentity.for_target(target) -> OAuthCredentialIdentity`, hashing canonical resource URL plus client identity for vault names without storing either in a filename.
- Produces: `VaultOAuthTokenStorage(TokenStorage)` with `get_tokens`, `set_tokens`, `get_client_info`, `set_client_info`, and `delete`.
- Produces: `RemoteMcpOAuthBroker.begin(session_key, server_name, authorization_url) -> OAuthAttempt`, `wait(attempt) -> AuthorizationCodeResult`, `complete(query) -> bool`, `fail(attempt, code)`, `disconnect(identity)`, and `close()`.
- Produces: `RemoteMcpOAuthEvent(session_key, server_name, state, authorization_url, outcome, code)` with no token-bearing fields.
- Consumes: `OAuthClientProvider`, `OAuthClientMetadata`, `OAuthFlowError`, and `AuthorizationCodeResult` from the official SDK.

- [ ] **Step 1: Write failing identity, vault, and one-shot attempt tests**

Use a `SecretVault(tmp_path)` with seeded access token, refresh token, and dynamic client secret. Assert round-trip through SDK Pydantic types, opaque vault names, URL/client separation, display-name reuse, deletion, and redacted repr/errors. Create a broker attempt from a literal authorization URL, complete it once, and assert replay, unknown state, oversized query, control characters, expiry, and cancellation cannot complete it.

```python
attempt = broker.begin(
    "dashboard:one",
    "linear",
    "https://auth.example/authorize?state=state-one&code_challenge=challenge",
)
assert broker.complete({"code": "code-one", "state": "state-one", "iss": "https://auth.example"})
assert await broker.wait(attempt) == AuthorizationCodeResult(
    code="code-one", state="state-one", iss="https://auth.example"
)
assert not broker.complete({"code": "replay", "state": "state-one"})
```

- [ ] **Step 2: Run OAuth unit tests and verify RED**

Run: `pytest -n0 test/test_remote_mcp_oauth.py -q`

Expected: collection fails because the OAuth broker and token storage do not exist.

- [ ] **Step 3: Implement minimal encrypted storage and attempt broker**

Serialize `OAuthToken` and `OAuthClientInformationFull` as JSON inside separate encrypted vault entries whose names contain only a versioned prefix and SHA-256 identity digest. Call synchronous `SecretVault.get` through `asyncio.to_thread`; use its async `set`/`delete` methods for writes. Parse failures fail closed and never return the raw stored value.

The broker accepts authorization URLs and callback queries up to 8 KiB, extracts exactly one `state` from the SDK-built URL, and stores a future plus session/server identity with a five-minute monotonic expiry. It holds at most 64 attempts, evicting only expired attempts and otherwise refusing new authorization. It publishes the URL through an injected async event sink and accepts only `code`, `state`, `iss`, `error`, and `error_description` on callback. It removes the attempt before resolving it. Provider denial resolves the future with a bounded `OAuthFlowError`; success returns `AuthorizationCodeResult` for the SDK to validate again.

- [ ] **Step 4: Integrate the SDK OAuth provider**

For targets without an explicit static `Authorization` header, create or reuse one `OAuthClientProvider` per `OAuthCredentialIdentity`. Reuse is required because the SDK provider owns an `anyio.Lock` around initialization, refresh, discovery, registration, and authorization; this serializes refresh safely across concurrent sessions while ordinary transports retain independent capabilities and lifecycles.

Use a task-local interaction context around each adapter connection so the shared provider's fixed redirect and callback handlers resolve the correct session and server. Build the redirect URI from the broker's trusted configured origin plus `/api/mcp/oauth/callback`; use configured scopes and client ID in `OAuthClientMetadata`. Attach the provider to each connection's gateway-only `httpx2.AsyncClient`. Catch `OAuthFlowError` separately from network/transport failures and emit only the three bounded remote MCP codes.

- [ ] **Step 5: Run OAuth and HTTP tests GREEN**

Run: `pytest -n0 test/test_remote_mcp_oauth.py test/test_remote_mcp_http.py -q && pytest test/test_remote_mcp_oauth.py test/test_remote_mcp_http.py -q`

Expected: vault, attempt, auth classification, concurrent refresh, session independence, and cleanup tests pass in serial and xdist modes.

- [ ] **Step 6: Review checkpoint**

Search the complete test output, exception reprs, SEL fakes, and diff for every seeded token/client secret. Leave changes uncommitted.

### Task 14: Trusted Callback and Existing OAuth Banner

**Files:**
- Create: `src/kiro_crew/dashboard/handlers/remote_mcp_oauth.py`
- Create: `test/test_remote_mcp_oauth_callback.py`
- Modify: `src/kiro_crew/dashboard/routes/agent_config.py`
- Modify: `src/kiro_crew/dashboard/token_auth.py`
- Modify: `src/kiro_crew/dashboard/server.py`
- Modify: `src/kiro_crew/dashboard/state.py`
- Modify: `test/test_token_auth.py`
- Modify: `test/test_mcp_oauth_banner.py`

**Interfaces:**
- Produces: fixed `REMOTE_MCP_OAUTH_CALLBACK_PATH = "/api/mcp/oauth/callback"` and `GET` handler `api_remote_mcp_oauth_callback`.
- Produces: `DashboardState.publish_remote_mcp_oauth_event(event) -> None`, resolving parent, channel-linked, cron, and subagent session keys to the owning visible slot.
- Consumes: existing `_emit_mcp_oauth_request` and `_mark_mcp_oauth_completed`; no second banner schema or frontend component is introduced.
- Configures: broker callback origin from `dashboard_origin(config.dashboard.url)` or the startup-validated `tailnet_host`, never from request headers.

- [ ] **Step 1: Write failing callback and banner-routing tests**

Register the real aiohttp route with a broker holding one attempt. Assert a bounded success callback returns 204 and wakes the waiter; replay/unknown/missing/multiple/oversized state returns JSON with a machine-readable code and no code/state echo. Assert direct dashboard, linked channel, cron, and `subagent:<id>` events update the correct existing banner, while a session with no visible slot remains an auth failure without creating a tab.

Add middleware tests proving only `GET` on the exact callback path bypasses dashboard token auth. `POST`, sibling paths, and wildcard lookalikes remain denied. Host validation remains active.

- [ ] **Step 2: Run callback/banner tests and verify RED**

Run: `pytest -n0 test/test_remote_mcp_oauth_callback.py test/test_token_auth.py test/test_mcp_oauth_banner.py -q`

Expected: the route is absent, the token middleware denies the callback, and no broker event reaches a slot.

- [ ] **Step 3: Add the self-authenticating callback route**

Add the callback path to `_BYPASS_EXACT_METHODS` for `GET` only, with a separate named method set and security comment explaining that one-shot high-entropy state is the handler credential. Do not add it to the Host-validation bypass or CSRF exemption map. The handler validates the bounded query through the broker, answers 204 on accepted delivery, and otherwise returns bounded JSON such as `{"error": "OAuth callback is not live", "code": "oauth_callback_not_live"}` without reflecting input.

- [ ] **Step 4: Wire trusted origin and existing banner sink**

During dashboard startup, after Tailscale resolution, select the callback origin in this order: explicit valid HTTPS `dashboard.url`; validated `https://<tailnet_host>`; exact loopback dashboard origin for local development. Refuse remote plain HTTP. Configure the process broker with that origin and `state.publish_remote_mcp_oauth_event`; register an app cleanup hook that cancels outstanding attempts.

`DashboardState.publish_remote_mcp_oauth_event` uses `dashboard_slot_key`, `get_linked_slot`, and `SubagentManager.get(...).parent_session_key` to find an existing slot. It function-locally imports the two banner helpers to avoid the current module cycle. Pending emits the normal authorization banner; success/failure marks it terminal with bounded codes only.

- [ ] **Step 5: Run callback/banner tests GREEN**

Run: `pytest -n0 test/test_remote_mcp_oauth_callback.py test/test_token_auth.py test/test_mcp_oauth_banner.py -q && pytest test/test_remote_mcp_oauth_callback.py test/test_mcp_oauth_banner.py -q`

Expected: exact-method perimeter tests, origin-selection tests, one-shot callback, and all visible-session routing cases pass.

- [ ] **Step 6: Review checkpoint**

Run the dashboard route-table security tests and inspect the bypass diff as a security-sensitive change. Leave changes uncommitted.

### Task 15: Real OAuth Flow, Grant Retention, and Integration Disconnect

**Files:**
- Modify: `test/test_remote_mcp_http.py`
- Modify: `test/test_remote_mcp_oauth.py`
- Modify: `src/kiro_crew/dashboard/handlers/mcp.py`
- Modify: `test/test_handlers_mcp_coverage.py`
- Modify: `test/test_gateway_appkit_endpoints.py`

**Interfaces:**
- Consumes: gateway HTTP adapter, SDK OAuth provider, callback broker, vault storage, and existing MCP remove handlers.
- Produces: `RemoteMcpOAuthBroker.disconnect_target(target) -> None`, deleting tokens/client registration locally and, when the SDK-discovered metadata advertises a revocation endpoint, using the provider context's `prepare_token_auth` to attempt RFC 7009 revocation without retaining local material on failure.
- Guarantees: session capability revocation closes transport but does not call `disconnect_target`; integration removal does.

- [ ] **Step 1: Write a failing real OAuth integration test**

Run in-process aiohttp protected-resource and authorization-server apps on port `0`. Implement the real protocol boundaries: initial 401 with protected-resource metadata, PRM discovery, authorization-server metadata, dynamic registration, authorization URL, callback delivery, PKCE token exchange, authenticated initialize replay, access-token expiry, refresh, and a second session using the stored grant. Assert the relay sees only MCP JSON-RPC and the auth server sees a valid S256 verifier relationship.

```python
assert registration_requests == 1
assert authorization_requests == 1
assert token_grants == ["authorization_code", "refresh_token"]
assert second_session_authorization_requests == 0
assert "access-canary" not in relay_frames
```

Add failures for issuer mismatch, state mismatch, resource mismatch, non-loopback HTTP, cross-origin credential redirect, metadata over budget, callback timeout, denial, and refresh rejection. Assert each returns the correct bounded MCP data code and no fallback request reaches legacy SSE.

- [ ] **Step 2: Run end-to-end OAuth tests and verify RED**

Run: `pytest -n0 test/test_remote_mcp_http.py -k oauth -q`

Expected: the first protected request cannot complete the full discovery/callback/vault/refresh path.

- [ ] **Step 3: Make minimal end-to-end corrections**

Correct only boundary integration defects exposed by the real flow: SDK metadata construction, task-local attempt association, response-hook classification, token persistence, refresh reuse, and teardown. Do not implement OAuth state, PKCE, discovery, registration, issuer validation, or token parsing outside the SDK.

- [ ] **Step 4: Write and implement disconnect lifecycle tests**

Seed a target grant, revoke one session capability, and assert the vault still returns it. Then remove that MCP integration through both `POST /api/mcp/remove` and `DELETE /api/mcp/servers/{name}` and assert `disconnect_target` received the pre-removal trusted HTTP spec, local tokens/client info are gone, and a failing remote revocation endpoint does not restore them.

Resolve the target before mutating configuration; never let the request body provide the URL or credential identity. Add machine-readable `code` fields to any new non-2xx JSON response touched in these handlers.

- [ ] **Step 5: Run lifecycle and OAuth tests GREEN**

Run: `pytest -n0 test/test_remote_mcp_http.py test/test_remote_mcp_oauth.py test/test_handlers_mcp_coverage.py test/test_gateway_appkit_endpoints.py -q && pytest test/test_remote_mcp_http.py test/test_handlers_mcp_coverage.py test/test_gateway_appkit_endpoints.py -q`

Expected: real authorization/refresh/reuse and both disconnect paths pass with no live sockets, attempts, tasks, or secrets after teardown.

- [ ] **Step 6: Review checkpoint**

Mutation-check that moving disconnect onto session revocation fails the retention test, restore with `apply_patch`, and leave changes uncommitted.

### Task 16: Authoritative Docs and Full Verification

**Files:**
- Modify: `docs/architecture/mcp.md`
- Modify: `docs/system-specs/modules/acp-client.md`
- Modify: `docs/system-specs/modules/providers.md`
- Modify: `docs/system-specs/modules/session.md`
- Modify: `docs/system-specs/modules/learn-cron-dashboard.md`
- Modify: `docs/superpowers/specs/2026-08-24-coder-remote-session-parity-design.md` only if verified implementation differs from the approved contract.

**Interfaces:**
- Consumes: verified behavior from Tasks 10–15.
- Produces: authoritative current-behavior documentation for gateway-owned HTTP/SSE/OAuth, callback trust, vault identity, failure codes, capability/grant lifecycle, and remaining hook-relay gap.

- [ ] **Step 1: Update owning specifications**

Replace the HTTP/OAuth unsupported statements with the verified transport matrix and security boundary. Document the official SDK ownership, no-sandbox-network rule, exact fallback predicate, callback origin source, one-shot public callback, encrypted credential key, concurrent refresh serialization, session revocation, and integration disconnect. Keep hook relay explicitly unimplemented.

- [ ] **Step 2: Run focused formatting and static gates**

Run:

```bash
black --target-version py310 <changed-python-files>
isort <changed-python-files> <changed-test-files>
python3 scripts/check_black_formatting.py
python3 scripts/check_subprocess_encoding.py
HARNESS_BASE_REF=origin/main python3 scripts/check_harness_parity.py
BRAND_BASE_REF=origin/main python3 scripts/check_brand_name.py
scripts/docs-lint.sh
flake8 <changed-python-files> <changed-test-files>
mypy --platform linux <changed-python-files>
git diff --check
```

Expected: every command exits zero without formatting unrelated baseline files.

- [ ] **Step 3: Run the complete remote MCP matrix**

Run all changed test modules plus existing ACP runtime/provider, MCP banner, token-auth, Coder host, session-sharing, subagent, memory identity, connections, vault, security, and gateway suites with repository xdist defaults.

Expected: zero failures and no residue warnings.

- [ ] **Step 4: Run the full backend gate**

Run: `env PATH=/home/user/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin .venv/bin/python -m pytest -q`

Expected: zero failures. Diagnose any failure deterministically; never rerun, lengthen a sleep, weaken an assertion, or skip.

- [ ] **Step 5: Optional live dogfood OAuth smoke**

Authorize one OAuth-protected MCP server from a Coder-hosted session over the Tailscale dashboard, call one read-only tool, end and recreate the session to prove encrypted grant reuse, then disconnect the integration and prove reauthorization is required. Record identifiers/outcomes only and do not capture authorization URLs, codes, headers, or tokens.

- [ ] **Step 6: Final uncommitted review checkpoint**

Run `git status --short`, a seeded-canary scan over the complete diff and generated test output, and the repository scrub/secret gates. Present the result without committing or pushing.
