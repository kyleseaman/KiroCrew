# Session Environment Health — Design

**Date:** 2026-09-01 · **Branch:** `feat/coder-aws-dogfood-poc` · **Status:** approved

## Problem

The session environment detail panel identifies the provider, immutable
configuration, and generated resource, but it cannot answer whether that resource
has enough memory for the next build or tool call. The gateway already reports its
own resource pressure, but using that reading for a managed session is incorrect:
the model and tools run inside the session environment, whose resource envelope is
independent of the gateway.

The live POC also exposed lifecycle drift. One visible session had a running
workspace and an active Kiro Crew workload scope. An archived session's protected
binding said stopped while Coder reported the workspace running without a Kiro
Crew workload scope and attributed the start to an SSH connection. The workspace
retained a Coder deadline and would autostop, but it was temporarily consuming
compute that the active-session UI could not explain.

## Decision

Add a provider-neutral, on-demand environment-health capability. The dashboard
queries it only while the environment detail panel is expanded. Coder implements
the capability with a fixed, credential-scrubbed remote memory probe against the
already-running, registry-owned workspace. Providers that do not implement the
capability omit the health rows.

The same change makes every gateway-owned Coder inspection pass
`--disable-autostart`. An observation must never start a stopped workspace, even
if the workspace changes state between the control-plane status check and the
remote command. Externally started workspaces without a Kiro Crew workload remain
subject to Coder's own autostop deadline; this feature does not terminate an
operator's manual terminal session.

Rejected alternatives:

- **Gateway memory in the panel** — cheap but describes the wrong machine and
  repeats the context leak the remote boundary intentionally removed.
- **Continuous metrics in slot WebSocket frames** — instant, but adds permanent
  background traffic and broadens persisted/live session state for a detail that
  is normally hidden.
- **Coder-only frontend endpoint** — faster to add, but hard-codes the first
  provider into a surface that is already provider-neutral.

## Provider contract

Introduce a positive opt-in capability separate from the provider catalog and
lifecycle mutation authority. A provider implementing environment health accepts
only a trusted durable session key and returns a bounded snapshot:

```json
{
  "provider": "coder",
  "resource_name": "crew-session-user-opaque",
  "state": "running",
  "memory": {
    "available_gb": 0.4,
    "total_gb": 1.9,
    "used_percent": 78.9,
    "pressure": "normal"
  }
}
```

`state` is `starting`, `running`, `stopped`, or `unavailable`. `memory` is absent
unless the provider obtained a valid live sample. `pressure` is `normal`,
`elevated`, or `critical`; for the Coder Linux probe it is derived from cgroup-
clamped available memory: elevated at 80% used and critical at 90% used. These
thresholds are owned constants in the Coder health adapter, not UI magic numbers.

The optional capability cannot be inferred from catalog shape. This preserves the
existing rule that public provider metadata cannot gain control-plane authority.

## Coder probe and security boundary

The Coder adapter resolves the session through `WorkspaceBindingRegistry`, fetches
the current workspace, and revalidates its immutable UUID, owner, template, and
generated name before probing. A stopped or missing workspace returns state only;
it is never started for telemetry.

For a running workspace, the gateway executes one fixed stdlib-only Python probe
through `coder ssh --disable-autostart`. The probe reads `/proc/meminfo` and cgroup
v2/v1 memory limits, clamps host availability to the workspace's effective cgroup
envelope, and emits one bounded JSON object. The command, script, and arguments are
owned by the adapter; no request value becomes an executable argument.

`CODER_AGENT_TOKEN` and `CODER_AGENT_TOKEN_FILE` are explicitly unset before the
probe starts, matching ACP preparation and cleanup. The gateway bounds execution
time and output bytes, ignores stderr, validates every numeric field, and maps any
failure to `unavailable` without returning diagnostics that could contain secrets.
The response contains no provider token, UUID, owner id, command output, process
list, or filesystem path.

All read-only Coder SSH inspections, including workload-scope checks, add
`--disable-autostart`. This closes the status-check race in which a workspace can
stop after a list response but before the SSH command begins.

## Dashboard API and UI

Add an authenticated owner-only endpoint for a slot's environment health. The
handler resolves the active slot and its durable environment binding, looks up the
provider by the trusted provider id, and calls the optional capability. It never
accepts a resource name or provider id from the browser. Unsupported providers and
unavailable samples return a successful bounded state response so the detail panel
can degrade quietly; malformed slot keys and unknown slots retain ordinary
machine-readable non-2xx errors.

The environment detail popover uses React Query and enables the query only while
open. It refreshes every ten seconds while visible and stops immediately when
closed or when the browser tab is hidden. The existing compact environment control
does not grow another badge or pressure pill.

The expanded panel adds:

- **Status** — provider control-plane state.
- **Memory pressure** — used percent, available and total GB, and a compact meter. Normal
  is neutral, elevated is warning, and critical uses the existing danger token.

No health snapshot enters slot metadata, WebSocket frames, transcripts, memory,
prompt context, or model-visible MCP results. The reading is browser diagnostics
only.

## Lifecycle behavior

Archiving remains stop-before-hide and preserves the workspace for the configured
30-day retention period. A stopped retained workspace has no memory sample and the
panel reports it as stopped if the session is restored before compute starts.

A workspace started outside Kiro Crew may temporarily disagree with the binding's
stopped intent. The gateway reports the provider's live state but does not treat an
external start as session activity, renew its deadline, or overwrite the durable
stop intent unless an exact Kiro Crew workload scope is active. Coder's deadline
then stops the externally started workspace. Automatic termination of manual
operator activity is deliberately out of scope.

## Testing

- Coder client tests pin `--disable-autostart` on scope and health SSH commands.
- Probe parser tests cover cgroup-clamped totals, valid snapshots, threshold
  boundaries, malformed/oversized output, and unavailable probes.
- Provider tests prove protected binding lookup and immutable identity validation
  happen before remote execution and that stopped workspaces are not probed.
- Dashboard handler tests cover active, stopped, unsupported, unknown-slot, and
  provider-failure responses without exposing internal diagnostics.
- Component tests prove the query is disabled while collapsed, polls while open,
  renders localized memory pressure, and omits the row when unavailable.
- Existing frontend build, typecheck, i18n, backend unit, and docs gates remain
  required.

## Out of scope

CPU and disk telemetry; historical graphs; telemetry in the compact control or
session list; model-facing resource context; automatically starting a workspace to
measure it; automatically stopping a workspace started manually outside Kiro Crew;
provider-specific UI branches; Kubernetes pod telemetry.
