"""Gateway coordinator for one verified Coder workspace per parent session."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from kiro_crew.coder.client import CoderClient, CoderClientError, CoderWorkspace
from kiro_crew.coder.registry import WorkspaceBinding, WorkspaceBindingRegistry


class CoderWorkspaceIdentityError(CoderClientError):
    """Coder returned a workspace that does not match the durable binding."""


class CoderCapacityError(CoderClientError):
    """Starting another managed workspace would exceed the operator's ceiling."""


@dataclass(frozen=True)
class ManagedWorkspacePolicy:
    template: str
    preset: str
    prefix: str
    stop_after_minutes: int
    delete_after_days: int
    max_running: int


class CoderWorkspaceManager:
    def __init__(
        self,
        *,
        registry: WorkspaceBindingRegistry,
        client: CoderClient,
        policy: ManagedWorkspacePolicy,
    ) -> None:
        self.registry = registry
        self.client = client
        self.policy = policy
        self._locks: dict[str, asyncio.Lock] = {}
        self._start_slots = asyncio.Semaphore(policy.max_running)
        self._capacity_lock = asyncio.Lock()

    def _lock_for(self, session_key: str) -> asyncio.Lock:
        lock = self._locks.get(session_key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_key] = lock
        return lock

    async def ensure_ready(
        self,
        session_key: str,
        *,
        template: str | None = None,
        preset: str | None = None,
    ) -> CoderWorkspace:
        selected_template = self.policy.template if template is None else template
        selected_preset = self.policy.preset if preset is None else preset
        owner_name, _owner_id = await self.client.current_user()
        binding = await asyncio.to_thread(
            self.registry.allocate,
            session_key,
            template=selected_template,
            preset=selected_preset,
            prefix=self.policy.prefix,
            owner_name=owner_name,
        )
        async with self._lock_for(session_key):
            current_binding = await asyncio.to_thread(self.registry.get, binding.binding_id)
            if current_binding is None:
                raise CoderWorkspaceIdentityError("Coder workspace binding disappeared")
            binding = current_binding
            workspace = await self.client.get_workspace(binding.workspace_name)
            if workspace is None:
                if binding.workspace_uuid:
                    binding = await asyncio.to_thread(
                        self.registry.replace,
                        replace(
                            binding,
                            workspace_uuid="",
                            owner_id="",
                            generation=binding.generation + 1,
                            state="allocated",
                            deletion_due_at="",
                            deleted_at="",
                        ),
                    )
                async with self._capacity_lock:
                    await self._require_capacity(binding.workspace_name)
                    async with self._start_slots:
                        workspace = await self.client.create_workspace(
                            name=binding.workspace_name,
                            template=binding.template,
                            preset=binding.preset,
                            stop_after_minutes=self.policy.stop_after_minutes,
                        )
            elif workspace.status == "stopped":
                async with self._capacity_lock:
                    await self._require_capacity(binding.workspace_name)
                    async with self._start_slots:
                        workspace = await self.client.start_workspace(binding.workspace_name)
            if workspace.name != binding.workspace_name or workspace.template != binding.template:
                raise CoderWorkspaceIdentityError("Coder workspace identity does not match binding")
            if binding.workspace_uuid and workspace.uuid != binding.workspace_uuid:
                raise CoderWorkspaceIdentityError("Coder workspace UUID does not match binding")
            if binding.owner_id and workspace.owner != binding.owner_id:
                raise CoderWorkspaceIdentityError("Coder workspace owner does not match binding")
            await asyncio.to_thread(
                self.registry.replace,
                replace(
                    binding,
                    workspace_uuid=workspace.uuid,
                    owner_id=workspace.owner,
                    state=workspace.status,
                    last_activity_at=workspace.last_used_at or binding.last_activity_at,
                    failure_code="",
                ),
            )
            return workspace

    async def _require_capacity(self, requested_name: str) -> None:
        bindings = await asyncio.to_thread(self.registry.list_bindings)
        owned_names = {binding.workspace_name for binding in bindings}
        active_states = {"pending", "starting", "running"}
        workspaces = await self.client.list_workspaces()
        running = sum(
            1
            for workspace in workspaces
            if workspace.name in owned_names
            and workspace.name != requested_name
            and workspace.status in active_states
        )
        if running >= self.policy.max_running:
            raise CoderCapacityError(
                f"Managed Coder workspace capacity reached ({running}/{self.policy.max_running})"
            )

    async def stop_for_session(self, session_key: str) -> str | None:
        """Stop the exact registry-owned workspace bound to a parent session."""
        snapshot = await asyncio.to_thread(self.registry.get_by_session, session_key)
        if snapshot is None or not snapshot.workspace_uuid or snapshot.state == "deleted":
            return None
        async with self._lock_for(session_key):
            binding = await asyncio.to_thread(self.registry.get, snapshot.binding_id)
            if binding is None or not binding.workspace_uuid or binding.state == "deleted":
                return None
            workspace = await self.client.get_workspace(binding.workspace_name)
            if workspace is None:
                return binding.workspace_name
            self._verify_destructive_identity(binding, workspace)
            if workspace.status != "stopped":
                workspace = await self.client.stop_workspace(binding.workspace_name)
                self._verify_destructive_identity(binding, workspace)
                if workspace.status != "stopped":
                    raise CoderWorkspaceIdentityError("Coder workspace did not stop")
            await asyncio.to_thread(
                self.registry.replace,
                replace(binding, state="stopped"),
            )
            return binding.workspace_name

    @staticmethod
    def _parse_time(value: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise CoderWorkspaceIdentityError("Coder workspace timestamp is invalid") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    async def reconcile_retention(self, *, now: str | None = None) -> tuple[str, ...]:
        """Delete only exact, stopped, registry-owned workspaces past retention."""
        now_dt = self._parse_time(now) if now else datetime.now(timezone.utc)
        deleted: list[str] = []
        for snapshot in await asyncio.to_thread(self.registry.list_bindings):
            async with self._lock_for(snapshot.session_key):
                binding = await asyncio.to_thread(self.registry.get, snapshot.binding_id)
                if binding is None or not binding.workspace_uuid or binding.state == "deleted":
                    continue
                workspace = await self.client.get_workspace(binding.workspace_name)
                if workspace is None or workspace.status != "stopped":
                    continue
                self._verify_destructive_identity(binding, workspace)
                activity = max(
                    self._parse_time(binding.last_activity_at),
                    self._parse_time(workspace.last_used_at or binding.last_activity_at),
                )
                due = activity + timedelta(days=self.policy.delete_after_days)
                due_text = due.isoformat().replace("+00:00", "Z")
                if now_dt < due:
                    if binding.deletion_due_at != due_text or binding.state != "stopped":
                        await asyncio.to_thread(
                            self.registry.replace,
                            replace(
                                binding,
                                state="stopped",
                                last_activity_at=activity.isoformat().replace("+00:00", "Z"),
                                deletion_due_at=due_text,
                            ),
                        )
                    continue
                pending = await asyncio.to_thread(
                    self.registry.replace,
                    replace(binding, state="delete_pending", deletion_due_at=due_text),
                )
                current = await self.client.get_workspace(pending.workspace_name)
                if current is None or current.status != "stopped":
                    continue
                self._verify_destructive_identity(pending, current)
                await self.client.delete_workspace(pending.workspace_name)
                deleted_at = now_dt.isoformat().replace("+00:00", "Z")
                await asyncio.to_thread(
                    self.registry.replace,
                    replace(pending, state="deleted", deleted_at=deleted_at),
                )
                deleted.append(pending.workspace_name)
        return tuple(deleted)

    @property
    def scope_reconcile_interval_seconds(self) -> int:
        """Heartbeat often enough to retain two thirds of the autostop margin."""
        return max(60, self.policy.stop_after_minutes * 60 // 3)

    async def reconcile_active_scopes(self, *, now: str | None = None) -> tuple[str, ...]:
        """Renew Coder only while an exact managed systemd workload scope is active."""
        now_dt = self._parse_time(now) if now else datetime.now(timezone.utc)
        activity = now_dt.isoformat().replace("+00:00", "Z")
        renewed: list[str] = []
        for binding in await asyncio.to_thread(self.registry.list_bindings):
            if not binding.workspace_uuid or binding.state == "deleted":
                continue
            workspace = await self.client.get_workspace(binding.workspace_name)
            if workspace is None or workspace.status != "running":
                continue
            self._verify_destructive_identity(binding, workspace)
            if not await self.client.has_active_workload_scope(workspace.name):
                continue
            await self.client.extend_workspace_deadline(
                workspace.name,
                self.policy.stop_after_minutes,
            )
            await asyncio.to_thread(
                self.registry.replace,
                replace(binding, state="running", last_activity_at=activity),
            )
            renewed.append(workspace.name)
        return tuple(renewed)

    @staticmethod
    def _verify_destructive_identity(binding: WorkspaceBinding, workspace: CoderWorkspace) -> None:
        if (
            workspace.uuid != binding.workspace_uuid
            or workspace.name != binding.workspace_name
            or workspace.owner != binding.owner_id
            or workspace.template != binding.template
        ):
            raise CoderWorkspaceIdentityError("Coder workspace identity does not match binding")
