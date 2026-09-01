"""Gateway coordinator for one verified Coder workspace per parent session."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from kiro_crew.coder.client import (
    CoderClient,
    CoderClientError,
    CoderWorkspace,
    CoderWorkspaceMemory,
)
from kiro_crew.coder.registry import WorkspaceBinding, WorkspaceBindingRegistry
from kiro_crew.constants import (
    EXECUTION_PHASE_ALLOCATING,
    EXECUTION_PHASE_CONNECTING,
    EXECUTION_PHASE_PROVISIONING,
)
from kiro_crew.session_environment import SessionEnvironmentPrefetchUnavailable

logger = logging.getLogger(__name__)

WorkspaceProgressCallback = Callable[[str, str], None]


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
        self._starting_names: set[str] = set()
        self._stop_tasks: dict[str, asyncio.Task[None]] = {}

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
        on_progress: WorkspaceProgressCallback | None = None,
        allow_start: bool = True,
    ) -> CoderWorkspace:
        ready_started = time.monotonic()
        identity_lookup = False
        last_progress: tuple[str, str] | None = None

        def report(phase: str, workspace: str) -> None:
            nonlocal last_progress
            progress = (phase, workspace)
            if progress == last_progress:
                return
            last_progress = progress
            self._report_progress(on_progress, phase, workspace)

        selected_template = self.policy.template if template is None else template
        selected_preset = self.policy.preset if preset is None else preset
        existing = await asyncio.to_thread(self.registry.get_by_session, session_key)
        if existing is not None and existing.state == "stop_pending":
            if not allow_start:
                raise SessionEnvironmentPrefetchUnavailable("Coder workspace stop is still pending")
            await self.stop_for_session(session_key)
            existing = await asyncio.to_thread(self.registry.get_by_session, session_key)
        if existing is None and not allow_start:
            raise SessionEnvironmentPrefetchUnavailable(
                "Coder workspace is not allocated for this session"
            )
        if existing is None:
            report(EXECUTION_PHASE_ALLOCATING, "")
        elif existing.state == "stopped" or not existing.workspace_uuid:
            report(EXECUTION_PHASE_PROVISIONING, existing.workspace_name)
        else:
            report(EXECUTION_PHASE_CONNECTING, existing.workspace_name)
        owner_name = ""
        if existing is None:
            owner_name, _owner_id = await self.client.current_user()
            identity_lookup = True
            binding = await asyncio.to_thread(
                self.registry.allocate,
                session_key,
                template=selected_template,
                preset=selected_preset,
                prefix=self.policy.prefix,
                owner_name=owner_name,
            )
        else:
            binding = existing
        async with self._lock_for(session_key):
            current_binding = await asyncio.to_thread(self.registry.get, binding.binding_id)
            if current_binding is None:
                raise CoderWorkspaceIdentityError("Coder workspace binding disappeared")
            if not current_binding.workspace_uuid and not owner_name:
                if not allow_start:
                    raise SessionEnvironmentPrefetchUnavailable(
                        "Coder workspace is not provisioned for this session"
                    )
                owner_name, _owner_id = await self.client.current_user()
                identity_lookup = True
            binding = await asyncio.to_thread(
                self.registry.repair_unprovisioned_name,
                current_binding.binding_id,
                prefix=self.policy.prefix,
                owner_name=owner_name,
            )
            if binding.workspace_name != current_binding.workspace_name:
                report(EXECUTION_PHASE_PROVISIONING, binding.workspace_name)
            workspaces = await self.client.list_workspaces()
            workspace = next(
                (item for item in workspaces if item.name == binding.workspace_name),
                None,
            )
            if workspace is None:
                if not allow_start:
                    raise SessionEnvironmentPrefetchUnavailable(
                        "Coder workspace is no longer available"
                    )
                report(EXECUTION_PHASE_PROVISIONING, binding.workspace_name)
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
                await self._reserve_capacity(binding.workspace_name, workspaces)
                try:
                    async with self._start_slots:
                        workspace = await self.client.create_workspace(
                            name=binding.workspace_name,
                            template=binding.template,
                            preset=binding.preset,
                            stop_after_minutes=self.policy.stop_after_minutes,
                        )
                finally:
                    await self._release_capacity(binding.workspace_name)
            elif not allow_start and workspace.status != "running":
                raise SessionEnvironmentPrefetchUnavailable(
                    "Coder workspace is not already running"
                )
            elif workspace.status == "stopped":
                report(EXECUTION_PHASE_PROVISIONING, binding.workspace_name)
                await self._reserve_capacity(binding.workspace_name, workspaces)
                try:
                    async with self._start_slots:
                        workspace = await self.client.start_workspace(binding.workspace_name)
                finally:
                    await self._release_capacity(binding.workspace_name)
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
            report(EXECUTION_PHASE_CONNECTING, workspace.name)
            logger.info(
                "Coder workspace ready name=%s prefetch=%s identity_lookup=%s "
                "control_plane_ms=%.0f",
                workspace.name,
                not allow_start,
                identity_lookup,
                (time.monotonic() - ready_started) * 1000.0,
            )
            return workspace

    @staticmethod
    def _report_progress(
        callback: WorkspaceProgressCallback | None,
        phase: str,
        workspace: str,
    ) -> None:
        """Publish non-secret lifecycle progress without risking provisioning."""
        if callback is None:
            return
        try:
            callback(phase, workspace)
        except Exception:
            logger.debug("Coder workspace progress callback failed", exc_info=True)

    async def _reserve_capacity(
        self,
        requested_name: str,
        workspaces: tuple[CoderWorkspace, ...],
    ) -> None:
        bindings = await asyncio.to_thread(self.registry.list_bindings)
        owned_names = {binding.workspace_name for binding in bindings}
        active_states = {"pending", "starting", "running"}
        async with self._capacity_lock:
            reserved_names = self._starting_names - {requested_name}
            running = sum(
                1
                for workspace in workspaces
                if workspace.name in owned_names
                and workspace.name != requested_name
                and workspace.name not in reserved_names
                and workspace.status in active_states
            )
            reserved = len(reserved_names)
            used = running + reserved
            if used >= self.policy.max_running:
                raise CoderCapacityError(
                    f"Managed Coder workspace capacity reached ({used}/{self.policy.max_running})"
                )
            self._starting_names.add(requested_name)

    async def _release_capacity(self, requested_name: str) -> None:
        async with self._capacity_lock:
            self._starting_names.discard(requested_name)

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
                await asyncio.to_thread(
                    self.registry.replace,
                    replace(binding, state="stopped"),
                )
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

    async def request_stop_for_session(self, session_key: str) -> str | None:
        """Persist stop intent, then drain it without blocking the archive request."""
        snapshot = await asyncio.to_thread(self.registry.get_by_session, session_key)
        if snapshot is None or snapshot.state == "deleted":
            return None
        async with self._lock_for(session_key):
            binding = await asyncio.to_thread(self.registry.get, snapshot.binding_id)
            if binding is None or binding.state == "deleted":
                return None
            if not binding.workspace_uuid:
                await asyncio.to_thread(
                    self.registry.replace,
                    replace(binding, state="stopped"),
                )
                return binding.workspace_name
            if binding.state != "stopped":
                await asyncio.to_thread(
                    self.registry.replace,
                    replace(binding, state="stop_pending"),
                )
        self._schedule_stop(session_key)
        return snapshot.workspace_name

    def _schedule_stop(self, session_key: str) -> None:
        current = self._stop_tasks.get(session_key)
        if current is not None and not current.done():
            return
        task = asyncio.create_task(self._drain_stop_request(session_key))
        self._stop_tasks[session_key] = task

        def forget(completed: asyncio.Task[None]) -> None:
            self._forget_stop_task(session_key, completed)

        task.add_done_callback(forget)

    def _forget_stop_task(self, session_key: str, task: asyncio.Task[None]) -> None:
        if self._stop_tasks.get(session_key) is task:
            self._stop_tasks.pop(session_key, None)

    async def _drain_stop_request(self, session_key: str) -> None:
        try:
            await self.stop_for_session(session_key)
        except Exception:
            logger.warning("Managed Coder workspace stop remains pending", exc_info=True)

    async def wait_for_pending_stops(self) -> None:
        """Wait for stop tasks already scheduled by this manager."""
        tasks = tuple(self._stop_tasks.values())
        if tasks:
            await asyncio.gather(*tasks)

    async def reconcile_stop_requests(self) -> tuple[str, ...]:
        """Retry durable stop intents left by archive or an earlier outage."""
        stopped: list[str] = []
        for binding in await asyncio.to_thread(self.registry.list_bindings):
            if binding.state != "stop_pending":
                continue
            try:
                workspace = await self.stop_for_session(binding.session_key)
            except Exception:
                logger.warning("Managed Coder workspace stop retry failed", exc_info=True)
                continue
            if workspace is not None:
                stopped.append(workspace)
        return tuple(stopped)

    async def inspect_session_health(
        self, session_key: str
    ) -> tuple[CoderWorkspace | None, CoderWorkspaceMemory | None]:
        """Inspect an exact bound workspace without creating or starting compute."""
        snapshot = await asyncio.to_thread(self.registry.get_by_session, session_key)
        if snapshot is None or not snapshot.workspace_uuid or snapshot.state == "deleted":
            return None, None
        async with self._lock_for(session_key):
            binding = await asyncio.to_thread(self.registry.get, snapshot.binding_id)
            if binding is None or not binding.workspace_uuid or binding.state == "deleted":
                return None, None
            workspace = await self.client.get_workspace(binding.workspace_name)
            if workspace is None:
                return None, None
            self._verify_destructive_identity(binding, workspace)
            if workspace.status != "running":
                return workspace, None
            try:
                memory = await self.client.workspace_memory(workspace.name)
            except CoderClientError:
                # Memory is an optional diagnostic. A failed in-workspace probe
                # must not erase the independently verified control-plane state.
                memory = None
            return workspace, memory

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
        workspaces = {
            workspace.name: workspace for workspace in await self.client.list_workspaces()
        }
        for snapshot in await asyncio.to_thread(self.registry.list_bindings):
            async with self._lock_for(snapshot.session_key):
                binding = await asyncio.to_thread(self.registry.get, snapshot.binding_id)
                if binding is None or not binding.workspace_uuid or binding.state == "deleted":
                    continue
                workspace = workspaces.get(binding.workspace_name)
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
                current = workspaces.get(pending.workspace_name)
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
        workspaces = {
            workspace.name: workspace for workspace in await self.client.list_workspaces()
        }
        for binding in await asyncio.to_thread(self.registry.list_bindings):
            if not binding.workspace_uuid or binding.state != "running":
                continue
            workspace = workspaces.get(binding.workspace_name)
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
