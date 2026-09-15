# Implementation Plans

Dated, task-by-task execution plans for documents in
[../](../README.md). A plan is the *how*; its RFC stays the *what* and the *why*.
Each plan names its RFC as its spec, so the two are read together.

Checkbox state is a progress record, not a status: read the RFC's `status` for
that, and read the **State** column here for whether a plan is being worked.

| Plan | Spec | State |
|---|---|---|
| [2026-08-22-durable-run-coordinator.md](2026-08-22-durable-run-coordinator.md) | [rfc-durable-run-coordinator.md](../rfc-durable-run-coordinator.md) | **Dormant.** 0 of 53 steps done and `RunCoordinator` has zero hits in `src/` and `test/`. The plan is unstarted, not obsolete — it is still the execution plan for a live RFC. |
| [2026-08-27-agentcore-identity-gateway.md](2026-08-27-agentcore-identity-gateway.md) | [rfc-agentcore-identity-gateway.md](../rfc-agentcore-identity-gateway.md) | **Live.** 11 of 30 steps done. `platform/agentcore_schema.py` ships and `AgentIdentityProvider` is real across `platform/interfaces.py`, `context.py`, `defaults.py` and `bootstrap.py`. |
| [2026-08-24-coder-gateway-mcp-proxy.md](2026-08-24-coder-gateway-mcp-proxy.md) | [specs/2026-08-24-coder-remote-session-parity-design.md](../specs/2026-08-24-coder-remote-session-parity-design.md) | **Live.** Coder-hosted session parity: gateway MCP proxy, memory, resume and subagents. Carried on the `coder-aws-dogfood` branch, not on main. |
| [2026-08-25-coder-per-session-workspaces.md](2026-08-25-coder-per-session-workspaces.md) | [specs/2026-08-25-coder-per-session-workspace-lifecycle-design.md](../specs/2026-08-25-coder-per-session-workspace-lifecycle-design.md) | **Live.** One lifecycle-managed Coder workspace per durable parent session tree. Carried on the `coder-aws-dogfood` branch, not on main. |
| [2026-09-01-session-environment-health.md](2026-09-01-session-environment-health.md) | — (plan predates its RFC) | **Live.** Provider-neutral, on-demand health telemetry for managed session environments. Carried on the `coder-aws-dogfood` branch, not on main. |

Indexed from [../README.md](../README.md).
