# Session Environment Health Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show live, provider-owned memory pressure in the expanded session environment panel without leaking gateway telemetry into remote sessions or waking stopped workspaces.

**Architecture:** Add a positive opt-in environment-health capability beside the existing lifecycle capability. Coder resolves and revalidates its protected binding, samples a running workspace through a fixed credential-scrubbed `coder ssh --disable-autostart` probe, and exposes only bounded numeric data through an owner-only slot endpoint. The frontend queries that endpoint only while the detail popover is open.

**Tech Stack:** Python 3.10+, aiohttp, Coder CLI, React 18, TypeScript, TanStack Query, Vitest, pytest.

**Spec:** `docs/task-specs/2026/09/session-environment-health/design.md`

## Global Constraints

- Telemetry describes the managed session environment, never the gateway host.
- Health data remains outside session metadata, WebSocket slot frames, transcripts, memory, prompt context, and MCP output.
- Provider tokens, Coder UUIDs, owner ids, commands, stderr, paths, and agent tokens never enter the response.
- A read-only inspection must not start stopped compute; every Coder SSH probe uses `--disable-autostart`.
- `CODER_AGENT_TOKEN` and `CODER_AGENT_TOKEN_FILE` are unset in the remote probe process.
- Providers without health support degrade by omitting memory data.
- All new user-facing strings use the i18n catalog and all numeric formatting names the active locale.
- Do not commit or push without explicit user authorization.

---

### Task 1: Bounded Coder workspace memory probe

**Files:**
- Modify: `src/kiro_crew/coder/client.py`
- Test: `test/test_coder_client.py`

**Interfaces:**
- Produces: `CoderWorkspaceMemory(available_gb: float, total_gb: float, used_percent: float, pressure: str)`.
- Produces: `CoderClient.workspace_memory(name: str) -> Awaitable[CoderWorkspaceMemory]`.
- Preserves: existing `Runner` injection used by Coder client unit tests.

- [x] **Step 1: Write failing argv and parser tests**

Add tests that drive the real public method through the fake runner and assert:

```python
memory = await client.workspace_memory("crew-abc123")
assert memory.available_gb == 0.4
assert memory.total_gb == 2.0
assert memory.used_percent == 80.0
assert memory.pressure == "elevated"
assert calls[-1][0][1:4] == ["ssh", "--disable-autostart", "crew-abc123"]
assert "CODER_AGENT_TOKEN" not in calls[-1][1]
```

Cover 80% and 90% boundaries, malformed JSON, non-finite/negative values,
available greater than total, and oversized output. Extend the existing scope
test to require `--disable-autostart`.

- [x] **Step 2: Run tests and verify RED**

Run:

```bash
python3 -m pytest test/test_coder_client.py -q
```

Expected: failures because `workspace_memory` and `CoderWorkspaceMemory` do not exist and the scope argv lacks `--disable-autostart`.

- [x] **Step 3: Implement the fixed probe and strict parser**

Add owned constants for the probe timeout/output bound and 80/90 pressure thresholds. Add a stdlib-only remote Python literal that reads `MemTotal`/`MemAvailable`, clamps them against cgroup v2 (`memory.max`/`memory.current`) or v1 limits when finite, and prints only numeric JSON. Call it with:

```python
await self._call(
    "ssh",
    "--disable-autostart",
    workspace,
    "--",
    "env",
    "-u", "CODER_AGENT_TOKEN",
    "-u", "CODER_AGENT_TOKEN_FILE",
    "python3", "-c", _WORKSPACE_MEMORY_SCRIPT,
)
```

Validate the workspace name before constructing argv. Parse only one bounded
object, require finite `0 <= available <= total`, round display values, and derive
pressure in Python. Add `--disable-autostart` to `has_active_workload_scope`.

- [x] **Step 4: Run tests and verify GREEN**

Run `python3 -m pytest test/test_coder_client.py -q` and expect all tests to pass.

---

### Task 2: Provider-neutral health contract and protected Coder mapping

**Files:**
- Modify: `src/kiro_crew/session_environment.py`
- Modify: `src/kiro_crew/coder/manager.py`
- Test: `test/test_session_environment.py`
- Test: `test/test_coder_lifecycle.py`

**Interfaces:**
- Consumes: `CoderClient.workspace_memory(name)` from Task 1.
- Produces: `SessionEnvironmentMemoryHealth` and `SessionEnvironmentHealth`, each with strict `to_dict()` output.
- Produces: positive opt-in `SessionEnvironmentHealthProvider.health_for_session(session_key)`.
- Produces: `CoderWorkspaceManager.inspect_session_health(session_key)` which returns provider state plus optional Coder memory.

- [x] **Step 1: Write failing contract and identity tests**

Test strict serialization, unsupported-provider detection, a stopped binding that
returns `state="stopped"` without invoking memory, a running binding that reuses
`_verify_destructive_identity` before invoking memory, and mismatched identity
that raises before any probe.

- [x] **Step 2: Run tests and verify RED**

Run:

```bash
python3 -m pytest test/test_session_environment.py test/test_coder_lifecycle.py -q
```

Expected: failures for missing health types and methods.

- [x] **Step 3: Implement the optional capability**

Add immutable provider-neutral health dataclasses with allowlisted states and
pressure values. Add a marker/capability class rather than extending the base
catalog protocol. Implement manager lookup as:

```python
binding = await asyncio.to_thread(self.registry.get_by_session, session_key)
workspace = await self.client.get_workspace(binding.workspace_name)
self._verify_destructive_identity(binding, workspace)
if workspace.status != "running":
    return CoderEnvironmentHealth(state=workspace.status, memory=None)
memory = await self.client.workspace_memory(workspace.name)
return CoderEnvironmentHealth(state="running", memory=memory)
```

The Coder provider maps this provider-specific record into the generic snapshot.
It treats probe failures as `state="unavailable"` with no memory and never
returns raw exception text.

- [x] **Step 4: Run tests and verify GREEN**

Run the two targeted pytest modules and expect all tests to pass.

---

### Task 3: Owner-only slot health endpoint

**Files:**
- Modify: `src/kiro_crew/dashboard/chat_handlers.py`
- Modify: `src/kiro_crew/dashboard/chat.py`
- Modify: `src/kiro_crew/dashboard/routes/chat.py`
- Test: `test/test_dashboard_chat_handlers.py` or the existing focused chat-handler test module selected during implementation.

**Interfaces:**
- Consumes: `SessionEnvironmentHealthProvider.health_for_session(session_key)` from Task 2.
- Produces: `GET /api/chat/slots/{slot}/environment/health`.

- [x] **Step 1: Write failing handler tests**

Cover owner-only denial, missing slot (`slot_not_found`), no binding, unsupported
provider, running memory response, stopped response, and provider exception. Prove
the handler passes `effective_session_key(slot)` and never accepts provider or
resource identity from query/body data.

- [x] **Step 2: Run tests and verify RED**

Run the focused handler test node and expect 404/missing handler failures.

- [x] **Step 3: Implement and export the handler**

Resolve the slot from server state, its durable `slot.environment`, and the
provider from `state.sessions.environment_registry()`. Call health only for a
positive `SessionEnvironmentHealthProvider` instance. Return a bounded snapshot
for supported, unsupported, and unavailable states; use machine-readable codes
for owner-only and unknown-slot errors. Register the GET route and export it
through the chat facade.

- [x] **Step 4: Run tests and verify GREEN**

Run the focused handler tests and expect all tests to pass.

---

### Task 4: Expanded environment memory UI

**Files:**
- Modify: `website/src/types/index.ts`
- Modify: `website/src/api/client.ts`
- Modify: `website/src/components/SessionEnvironmentControl.tsx`
- Modify: `website/src/components/ExecutionLocationBadge.tsx`
- Modify: `website/src/components/CoderExecutionControl.test.tsx`
- Modify: `website/src/i18n/locales/en.json`
- Modify: `website/src/i18n/en.context.json`

**Interfaces:**
- Consumes: `GET /api/chat/slots/{slot}/environment/health` from Task 3.
- Produces: `SessionEnvironmentHealth` TypeScript type and `api.getSessionEnvironmentHealth(slot)`.
- Produces: an open-state callback from `ExecutionLocationBadge` so the parent query is enabled only while expanded.

- [x] **Step 1: Write failing component tests**

Mock `getSessionEnvironmentHealth`. Assert no call while collapsed, one call after
opening, localized status/memory text and meter at 80%, omission when `memory` is
absent, and no memory text in the compact trigger. Use fake timers to prove the
ten-second refresh stops after closing.

- [x] **Step 2: Run tests and verify RED**

Run:

```bash
cd website && npx vitest run src/components/CoderExecutionControl.test.tsx
```

Expected: failures because the API method, types, open callback, and rows do not exist.

- [x] **Step 3: Implement query and panel rows**

Track popover open state in `SessionEnvironmentControl`, enable a React Query
request only for a bound slot with an expanded panel, set a ten-second interval,
and keep background refetch disabled. Pass the snapshot into
`ExecutionLocationBadge`. Render localized Status and Memory pressure rows in the
existing `<dl>`, format numbers through the active i18n locale seam, use Lucide
icons and existing design tokens, and expose progress semantics on the meter.

- [x] **Step 4: Run tests and verify GREEN**

Run the targeted Vitest file and expect all tests to pass.

---

### Task 5: Specifications and verification

**Files:**
- Modify: `docs/system-specs/modules/providers.md`
- Modify: `docs/system-specs/modules/session.md`

**Interfaces:**
- Documents the final capability, security boundary, endpoint behavior, and UI lifecycle implemented by Tasks 1–4.

- [x] **Step 1: Update owning specifications**

Document positive provider opt-in, trusted binding lookup, Coder identity
revalidation, fixed credential-scrubbed probe, `--disable-autostart`, panel-only
polling, and exclusion from model/session state.

- [x] **Step 2: Run focused verification**

```bash
python3 -m pytest test/test_coder_client.py test/test_session_environment.py test/test_coder_lifecycle.py -q
cd website && npx vitest run src/components/CoderExecutionControl.test.tsx
```

- [x] **Step 3: Run frontend and documentation gates**

```bash
cd website && npm run build && npm run test
cd .. && scripts/docs-lint.sh && git diff --check
```

- [x] **Step 4: Inspect the final diff**

Confirm no secrets, gateway metrics, provider-specific frontend branches, raw
English UI strings, model-context fields, or unrelated changes entered the diff.
Leave the worktree uncommitted until the user explicitly requests a commit.
