# Coder Per-Session Workspaces Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:executing-plans to implement this plan task-by-task. This plan is
> executed inline because the repository instructions prohibit proactive
> subagent dispatch. The user has explicitly authorized commits, pushing the
> fork, and updating the live test deployment.

**Goal:** Replace the shared Coder workspace POC with one durable,
lifecycle-managed workspace per Kiro Crew parent session while keeping every
descendant in that workspace and preserving gateway-owned memory, MCP, OAuth,
history, cron, and policy.

**Architecture:** A gateway lifecycle manager allocates opaque binding records,
uses a structured Coder client to create or start the bound workspace, then
hands the existing SSH/MCP transport a verified concrete workspace name. Parent
session metadata persists only the binding id and filesystem generation;
subagents inherit the resolved host. Coder owns connection-aware autostop and a
gateway reconciler owns OSS-compatible 30-day retention using exact UUID,
owner, and name validation before deletion.

**Tech Stack:** Python 3.10+, asyncio, aiohttp, Coder REST API and pinned CLI,
atomic JSON persistence, `SecretVault`, React/TypeScript/Vite, Terraform Coder
templates, pytest/pytest-asyncio/Vitest.

**Spec:**
`docs/superpowers/specs/2026-08-25-coder-per-session-workspace-lifecycle-design.md`

## Global Constraints

- `crew-dogfood` remains an unmanaged static smoke workspace. Migration never
  adopts, stops, renames, or deletes it.
- Workspace names contain only the configured safe prefix and random binding
  id. Session titles, prompts, repositories, channel ids, and user identifiers
  never enter names.
- The Coder token remains in gateway `SecretVault`; it is allowed only in the
  Coder HTTP session header or CLI environment and never in workspace files,
  argv, logs, config JSON, or test output.
- A lifecycle failure fails closed. A managed session never executes locally or
  in a sibling workspace.
- Destructive operations require the persisted workspace UUID plus an exact
  owner/name match after a locked refetch. A name prefix alone grants nothing.
- The binding registry is a Crew data-home keystone path and is written
  atomically. Corruption disables destructive lifecycle operations.
- One durable parent session tree has one binding. A fork has a new binding.
  Shared and dedicated subagents receive the parent's binding and may not
  allocate.
- Coder native activity-aware autostop remains the stop authority. The gateway
  renews activity only for an explicit managed workload lease or non-empty
  managed systemd scope.
- Defaults are five warm minutes, 30 stop minutes, 30 retention days, and three
  simultaneous running/starting managed workspaces.
- Tests inject clocks and fake transports; they do not contact the operator's
  Coder deployment or touch the operator's Crew/Kiro homes.
- No user-facing English string is hardcoded outside the i18n catalog. New
  backend non-2xx responses include a stable `code`.
- No model id is hardcoded, and Kiro harness identity checks remain positive.
- Do not modify `CHANGELOG.md`.

---

### Task 1: Managed Coder Configuration and Compatibility Boundary

**Files:**
- Modify: `src/kiro_crew/config/loader.py`
- Modify: `src/kiro_crew/constants.py`
- Modify: `config-baseline.json`
- Modify: `test/test_config_loader.py`
- Modify: `test/test_coder_settings.py`

**Interfaces:**
- Replace the persisted shared `workspace` field with `template`, `preset`,
  `runtime_warm_minutes`, `stop_after_minutes`, `delete_after_days`,
  `max_running`, and `workspace_prefix` on `CoderSessionConfig`.
- Preserve the environment-only static path as an explicitly separate
  compatibility mode for one deprecation window.
- Validate URLs, names, positive bounds, and safe prefixes at load and Settings
  update time. Persist no token.

- [ ] Write failing loader and handler tests for defaults, validation, unknown
  key rejection, no token persistence, and static compatibility isolation.
- [ ] Run `pytest -n0 test/test_config_loader.py test/test_coder_settings.py -q`
  and confirm failures name the missing managed fields.
- [ ] Implement only the config/schema migration and validation needed by the
  tests.
- [ ] Rerun the focused tests and inspect `git diff --check`.

### Task 2: Integrity-Protected Workspace Binding Registry

**Files:**
- Create: `src/kiro_crew/coder/__init__.py`
- Create: `src/kiro_crew/coder/registry.py`
- Create: `test/test_coder_workspace_registry.py`
- Modify: `src/kiro_crew/security.py`
- Modify: `test/test_sensitive_paths_security.py`
- Modify: `docs/system-specs/modules/security.md`

**Interfaces:**
- Produce immutable `WorkspaceBinding` records with opaque `binding_id`,
  `session_key`, deployment fingerprint, UUID, safe name, owner, organization,
  template, preset, generation, lifecycle state, timestamps, and bounded failure
  code.
- Produce `WorkspaceBindingRegistry.allocate(session_key, policy)`,
  `get_by_session`, `get`, `replace`, `mark_activity`, and `list_bindings`.
- Store at `config_dir()/coder_workspaces.json` with owner-only permissions,
  atomic replace, schema versioning, a cross-process lock, and fail-closed corrupt
  reads.

- [ ] Write failing tests for concurrent idempotent allocation, distinct parent
  allocation, opaque safe names, atomic reload, owner-only mode, corrupt input,
  compare-and-replace generation, and bounded persisted errors.
- [ ] Add a failing security test proving the agent cannot read, write, move,
  archive, or extract over `coder_workspaces.json` or its lock.
- [ ] Run `pytest -n0 test/test_coder_workspace_registry.py test/test_sensitive_paths_security.py -q`.
- [ ] Implement the registry with existing platform-compatible lock and
  owner-only helpers; add the registry paths to the keystone set.
- [ ] Rerun the focused tests and `python3 scripts/docs-lint.sh`.

### Task 3: Structured Coder Client

**Files:**
- Create: `src/kiro_crew/coder/client.py`
- Create: `test/test_coder_client.py`
- Modify: `src/kiro_crew/dashboard/handlers/coder.py`

**Interfaces:**
- Produce bounded records for deployment, user, template, preset, workspace,
  build, and agent state.
- Produce async `probe`, `create_workspace`, `start_workspace`,
  `update_autostop`, `watch_ready`, `bump_activity`, `delete_workspace`, and
  `watch_deleted` methods.
- Use fixed-origin REST requests with the token only in the Coder session
  header. Use the CLI only when a template parameter cannot be expressed safely,
  passing the token via environment and resolving the result back through REST.

- [ ] Write fake-server tests for authentication, template/preset resolution,
  create/start/delete watches, agent readiness, activity timestamps, redirects,
  timeout, oversized/malformed bodies, error redaction, and deployment mismatch.
- [ ] Run `pytest -n0 test/test_coder_client.py -q` and observe the missing
  client failures.
- [ ] Implement the smallest bounded HTTP client and optional injected CLI
  runner that passes the tests.
- [ ] Rerun the test serially and under the default parallel configuration;
  verify token canaries appear nowhere in captured logs or exceptions.

### Task 4: Lifecycle Coordinator, Capacity, and Reconciliation

**Files:**
- Create: `src/kiro_crew/coder/manager.py`
- Create: `test/test_coder_lifecycle.py`
- Modify: `src/kiro_crew/dashboard/state.py`
- Modify: `src/kiro_crew/dashboard/server.py`
- Modify: `docs/system-specs/modules/learn-cron-dashboard.md`

**Interfaces:**
- Produce `CoderWorkspaceManager.ensure_ready(parent_session_key)`,
  `acquire_lease`, `release_lease`, `record_activity`, `reconcile_startup`,
  `reconcile_retention`, `request_delete`, and `close`.
- Serialize each binding with an async lock, bound create/start work with a
  global semaphore, and count starting/running records against `max_running`.
- Return a verified concrete workspace descriptor to the transport; never
  return a local fallback.
- Reconcile only registry records. Delete only stopped, lease-free records after
  effective activity plus retention and a locked UUID/owner/name refetch.

- [ ] Write injected-clock/state-machine tests for concurrent first turns,
  restart, stopped resume, missing/manual deletion, failed build, capacity,
  deployment quarantine, resume-vs-delete, two-phase delete, and corrupt
  registry.
- [ ] Run `pytest -n0 test/test_coder_lifecycle.py -q` and observe missing
  coordinator behavior.
- [ ] Implement allocation, readiness, leases, capacity, startup reconciliation,
  periodic reconciliation, and two-phase deletion in that order.
- [ ] Rerun the state-machine tests without sleeps and inspect cancellation
  cleanup.

### Task 5: Lazy Per-Parent Host Resolution and Descendant Inheritance

**Files:**
- Modify: `src/kiro_crew/acp/session_host.py`
- Modify: `src/kiro_crew/acp/client.py`
- Modify: `src/kiro_crew/session.py`
- Modify: `src/kiro_crew/history.py`
- Modify: `src/kiro_crew/subagent.py`
- Modify: `test/test_acp_session_host.py`
- Modify: `test/test_session.py`
- Modify: `test/test_history.py`
- Modify: `test/test_subagent.py`
- Modify: `docs/system-specs/modules/acp-client.md`
- Modify: `docs/system-specs/modules/providers.md`
- Modify: `docs/system-specs/modules/session.md`
- Modify: `docs/system-specs/modules/history.md`
- Modify: `docs/system-specs/modules/subagent.md`

**Interfaces:**
- Persist only `coder_binding_id` and `coder_filesystem_generation` in parent
  history metadata.
- Add a managed session host that resolves the parent binding asynchronously
  during the existing remote prepare step, then delegates SSH, MCP reverse
  forwarding, and remote preparation to `CoderWorkspaceSessionHost`.
- Pass the exact resolved host through existing shared and dedicated subagent
  affinity seams. A child without parent affinity fails instead of allocating.
- A fork receives no binding metadata and allocates its own binding.

- [ ] Write failing tests proving same-parent resume reuse, unrelated parent and
  fork separation, concurrent first-turn idempotence, shared/dedicated child
  inheritance, and fail-closed host errors.
- [ ] Run the five focused backend test modules and confirm the static shared
  workspace behavior fails the new assertions.
- [ ] Implement metadata persistence and lazy managed-host resolution without
  changing the local or explicit static-host paths.
- [ ] Rerun focused tests and
  `HARNESS_BASE_REF=origin/main python3 scripts/check_harness_parity.py`.

### Task 6: Conservative Runtime Warmth and Managed Workload Scopes

**Files:**
- Create: `src/kiro_crew/coder/workload.py`
- Create: `test/test_coder_workload.py`
- Modify: `src/kiro_crew/acp/session_host.py`
- Modify: `src/kiro_crew/acp/client.py`
- Modify: `src/kiro_crew/session.py`
- Modify: `deploy/coder-aws/workspace/cloud-init.sh.tftpl`
- Modify: `test/test_coder_aws_poc.py`

**Interfaces:**
- Wrap managed remote ACP processes in a transient `systemd-run --user --scope`
  named only from binding/runtime ids.
- Close an idle managed ACP SSH transport after `runtime_warm_minutes`; never
  reap a session whose semaphore or lifecycle lease is active.
- Probe the exact managed user scope. While it remains active after ACP exit,
  hold a lease and post bounded Coder activity; stop renewal when empty.
- Make the AWS sample enable user lingering, provide the user bus, verify a
  transient scope, and create an owner-only traversable working directory.

- [ ] Write command-construction and fake-probe tests for foreground lease,
  ordinary `nohup` descendants, empty scope release, bounded heartbeat, and no
  permanent-agent/CPU inference.
- [ ] Run the workload and AWS template tests and observe the missing scope
  contract.
- [ ] Implement the scope wrapper, warm timer, probe, and activity renewal with
  injected clocks and runners.
- [ ] Rerun focused tests and subprocess/platform compatibility gates.

### Task 7: Settings, Lifecycle Status, Actions, and Operator Documentation

**Files:**
- Modify: `src/kiro_crew/dashboard/handlers/coder.py`
- Modify: `src/kiro_crew/dashboard/server.py`
- Modify: `website/src/api/client.ts`
- Modify: `website/src/types/index.ts`
- Modify: `website/src/pages/settings/CoderPanel.tsx`
- Modify: `website/src/pages/settings/CoderPanel.test.tsx`
- Modify: `website/src/components/CoderExecutionBadge.tsx`
- Modify: `website/src/components/CoderExecutionBadge.test.tsx`
- Modify: `website/src/i18n/locales/*.json`
- Modify: `docs/guides/remote-and-mobile.md`
- Modify: `deploy/coder-aws/control-plane/README.md`
- Modify: `deploy/coder-aws/workspace/README.md`

**Interfaces:**
- Settings GET/PUT exposes managed template/preset/policy values and a
  non-billable connection probe that validates auth, owner, template, preset,
  CLI availability, and deployment identity.
- Lifecycle list/status endpoints expose only safe workspace metadata. Start,
  stop/open, and confirmed delete actions resolve through the manager and stable
  machine-readable error codes.
- UI explains many retained workspaces versus few running workspaces, the
  30-minute/30-day defaults, subagent inheritance, gateway ownership, static
  compatibility, and AWS template parameters.

- [ ] Replace static-workspace UI expectations with failing managed-policy,
  status, confirmation, accessibility, and redaction tests.
- [ ] Run focused backend and frontend tests.
- [ ] Implement the handlers, typed client, translated panel, lifecycle badge,
  actions, expansion docs, and sample deployment instructions.
- [ ] Run `cd website && npm run build && npm run test` plus i18n/settings
  extraction gates.

### Task 8: Permanent Session Deletion and Cron Semantics

**Files:**
- Modify: `src/kiro_crew/dashboard/handlers/history.py`
- Modify: `src/kiro_crew/cron.py`
- Modify: `src/kiro_crew/session.py`
- Modify: `test/test_dashboard_history.py`
- Modify: `test/test_cron.py`
- Modify: `test/test_session.py`
- Modify: `docs/system-specs/modules/learn-cron-dashboard.md`

**Interfaces:**
- Permanent history deletion submits immediate two-phase workspace deletion but
  retains an orphan cleanup record if Coder is unavailable.
- Archive/close does not delete early and continues normal retention.
- A distinct cron run owns a distinct parent binding; a retry of the same
  durable run reuses it. Capacity failure is retryable and never preempts an
  interactive session.

- [ ] Write failing tests for permanent-delete intent, archive retention,
  orphan cleanup, distinct cron runs, retry reuse, and retryable capacity.
- [ ] Run focused history/cron/session tests.
- [ ] Implement the hooks through the lifecycle manager without adding
  per-caller state to any MCP server.
- [ ] Rerun focused tests and injected-message/documentation gates.

### Task 9: Local Verification, Commits, Fork Push, and Live AWS Dogfood

**Files:**
- Verify all files above; do not edit `CHANGELOG.md`.
- Update owning specs in the same logical commits if the final API differs from
  this plan.

- [ ] Run focused backend suites for config, registry, client, lifecycle, host,
  session/history/subagent/cron/dashboard, workload, security, and AWS template.
- [ ] Run `python3 scripts/check_black_formatting.py`, subprocess encoding,
  isort check/fix only touched files, flake8 on touched Python, Linux-platform
  mypy on touched package paths, docs lint, scrub lint, brand gate, and harness
  parity.
- [ ] Run `python -m pytest` and `cd website && npm run build && npm run test`;
  document any pre-existing unrelated failure rather than weakening it.
- [ ] Review the complete diff for secrets, unrelated changes, generated
  artifacts, and accidental `crew-dogfood` ownership.
- [ ] Create conventional commits grouped by docs/design, backend lifecycle,
  UI/operator experience, and deployment template; push the current feature
  branch to the user's fork.
- [ ] In the dogfood AWS account in `us-east-2`, snapshot the current gateway
  service/drop-in and Coder template versions, build/install the pushed branch
  into a fresh venv, migrate Settings without exposing the vault token, restart,
  and prove health before moving the rollback pointer.
- [ ] Push the updated `kirocrew-arm` template and update the retained
  `crew-dogfood` smoke workspace without enrolling it in managed retention.
- [ ] Create two new parent sessions and prove two distinct workspace UUIDs and
  EC2 instances, parent/subagent filesystem affinity, gateway memory/MCP/OAuth
  parity, protected long-running work, autostop, same-disk resume, accelerated
  two-phase deletion, and fresh-generation resume. Restore production defaults
  after accelerated tests.
- [ ] Report pushed commit ids, live versions, retained rollback path, managed
  workspace names/UUIDs (no secrets), and any acceptance criterion not yet
  proven.

## Acceptance Gate

- Two unrelated durable parents cannot resolve to the same workspace UUID.
- Parent resume reuses its UUID until verified retention deletion; a fork gets a
  new UUID.
- Every descendant runs in the parent's workspace and cannot select another.
- Long turns and managed descendants preserve activity; idle ACP SSH does not
  keep compute running forever.
- Human Coder connections remain authoritative for autostop.
- Retention deletion requires stopped state, no lease/scope, elapsed policy,
  exact UUID/owner/name refetch, and a persisted two-phase intent.
- Gateway history, memory, MCP, OAuth, policy, and cron work across stop/resume;
  no Coder or service credential enters a workspace.
- Static `crew-dogfood` remains available and unmanaged.
- The pushed fork and the live deployment run the same verified code.
