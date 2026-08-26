"""Per-parent Coder allocation and lifecycle coordination."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from kiro_crew.coder.client import CoderWorkspace
from kiro_crew.coder.manager import (
    CoderCapacityError,
    CoderWorkspaceManager,
    ManagedWorkspacePolicy,
)
from kiro_crew.coder.registry import WorkspaceBindingRegistry


class _FakeClient:
    def __init__(self) -> None:
        self.workspaces: dict[str, CoderWorkspace] = {}
        self.created: list[str] = []
        self.created_specs: list[tuple[str, str, str]] = []
        self.started: list[str] = []
        self.stopped: list[str] = []
        self.active_scopes: set[str] = set()
        self.extended: list[tuple[str, int]] = []

    async def current_user(self) -> tuple[str, str]:
        return "kyleseaman", "owner-kyleseaman"

    async def get_workspace(self, name: str) -> CoderWorkspace | None:
        return self.workspaces.get(name)

    async def list_workspaces(self) -> tuple[CoderWorkspace, ...]:
        return tuple(self.workspaces.values())

    async def create_workspace(
        self, *, name: str, template: str, preset: str, stop_after_minutes: int
    ) -> CoderWorkspace:
        await asyncio.sleep(0)
        self.created.append(name)
        self.created_specs.append((name, template, preset))
        workspace = CoderWorkspace(
            uuid=f"uuid-{name}",
            name=name,
            owner="kyleseaman",
            template=template,
            status="running",
            last_used_at="2026-08-25T12:00:00Z",
        )
        self.workspaces[name] = workspace
        return workspace

    async def start_workspace(self, name: str) -> CoderWorkspace:
        self.started.append(name)
        current = self.workspaces[name]
        running = CoderWorkspace(**{**current.__dict__, "status": "running"})
        self.workspaces[name] = running
        return running

    async def stop_workspace(self, name: str) -> CoderWorkspace:
        self.stopped.append(name)
        current = self.workspaces[name]
        stopped = CoderWorkspace(**{**current.__dict__, "status": "stopped"})
        self.workspaces[name] = stopped
        return stopped

    async def delete_workspace(self, name: str) -> None:
        self.workspaces.pop(name, None)

    async def has_active_workload_scope(self, name: str) -> bool:
        return name in self.active_scopes

    async def extend_workspace_deadline(self, name: str, minutes: int) -> None:
        self.extended.append((name, minutes))


def _manager(tmp_path: Path, client: _FakeClient) -> CoderWorkspaceManager:
    return CoderWorkspaceManager(
        registry=WorkspaceBindingRegistry(tmp_path / "coder_workspaces.json"),
        client=client,
        policy=ManagedWorkspacePolicy(
            template="kirocrew-arm",
            preset="arm-small",
            prefix="crew-session",
            stop_after_minutes=30,
            delete_after_days=30,
            max_running=3,
        ),
    )


@pytest.mark.asyncio
async def test_concurrent_first_turn_creates_exactly_one_workspace(tmp_path: Path) -> None:
    client = _FakeClient()
    manager = _manager(tmp_path, client)

    first, second = await asyncio.gather(
        manager.ensure_ready("dashboard:one"),
        manager.ensure_ready("dashboard:one"),
    )

    assert first == second
    assert len(client.created) == 1
    binding = manager.registry.get_by_session("dashboard:one")
    assert binding is not None
    assert binding.workspace_uuid == first.uuid
    assert binding.owner_id == "kyleseaman"


@pytest.mark.asyncio
async def test_unrelated_parents_get_distinct_workspaces(tmp_path: Path) -> None:
    client = _FakeClient()
    manager = _manager(tmp_path, client)

    first = await manager.ensure_ready("dashboard:one")
    second = await manager.ensure_ready("dashboard:two")

    assert first.uuid != second.uuid
    assert first.name != second.name
    assert first.name.startswith("crew-session-kyleseaman-")
    assert second.name.startswith("crew-session-kyleseaman-")


@pytest.mark.asyncio
async def test_each_parent_can_allocate_from_a_different_profile(tmp_path: Path) -> None:
    client = _FakeClient()
    manager = _manager(tmp_path, client)

    first = await manager.ensure_ready("dashboard:one", template="kirocrew-arm", preset="arm-small")
    second = await manager.ensure_ready(
        "dashboard:two", template="kirocrew-gpu", preset="gpu-medium"
    )

    assert client.created_specs == [
        (first.name, "kirocrew-arm", "arm-small"),
        (second.name, "kirocrew-gpu", "gpu-medium"),
    ]


@pytest.mark.asyncio
async def test_existing_binding_keeps_original_profile_coordinates(tmp_path: Path) -> None:
    client = _FakeClient()
    manager = _manager(tmp_path, client)
    first = await manager.ensure_ready("dashboard:one", template="kirocrew-arm", preset="arm-small")
    client.workspaces[first.name] = CoderWorkspace(**{**first.__dict__, "status": "stopped"})

    resumed = await manager.ensure_ready(
        "dashboard:one", template="kirocrew-gpu", preset="gpu-medium"
    )

    assert resumed.template == "kirocrew-arm"
    binding = manager.registry.get_by_session("dashboard:one")
    assert binding is not None
    assert (binding.template, binding.preset) == ("kirocrew-arm", "arm-small")


@pytest.mark.asyncio
async def test_stopped_workspace_restarts_with_same_uuid(tmp_path: Path) -> None:
    client = _FakeClient()
    manager = _manager(tmp_path, client)
    first = await manager.ensure_ready("dashboard:one")
    client.workspaces[first.name] = CoderWorkspace(**{**first.__dict__, "status": "stopped"})

    resumed = await manager.ensure_ready("dashboard:one")

    assert resumed.uuid == first.uuid
    assert client.started == [first.name]


@pytest.mark.asyncio
async def test_stop_for_session_stops_its_exact_bound_workspace(tmp_path: Path) -> None:
    client = _FakeClient()
    manager = _manager(tmp_path, client)
    workspace = await manager.ensure_ready("dashboard:one")

    stopped = await manager.stop_for_session("dashboard:one")

    assert stopped == workspace.name
    assert client.stopped == [workspace.name]
    binding = manager.registry.get_by_session("dashboard:one")
    assert binding is not None
    assert binding.state == "stopped"


@pytest.mark.asyncio
async def test_running_capacity_refuses_an_unrelated_start(tmp_path: Path) -> None:
    client = _FakeClient()
    manager = _manager(tmp_path, client)
    manager.policy = ManagedWorkspacePolicy(**{**manager.policy.__dict__, "max_running": 1})
    await manager.ensure_ready("dashboard:one")

    with pytest.raises(CoderCapacityError):
        await manager.ensure_ready("dashboard:two")


@pytest.mark.asyncio
async def test_retention_deletes_only_exact_stopped_registry_owned_workspace(
    tmp_path: Path,
) -> None:
    client = _FakeClient()
    manager = _manager(tmp_path, client)
    workspace = await manager.ensure_ready("dashboard:one")
    binding = manager.registry.get_by_session("dashboard:one")
    assert binding is not None
    client.workspaces[workspace.name] = CoderWorkspace(
        **{**workspace.__dict__, "status": "stopped", "last_used_at": "2026-06-01T00:00:00Z"}
    )
    manager.registry.replace(replace(binding, last_activity_at="2026-06-01T00:00:00Z"))

    deleted = await manager.reconcile_retention(now="2026-08-25T12:00:00Z")

    assert deleted == (workspace.name,)
    assert manager.registry.get_by_session("dashboard:one").state == "deleted"


@pytest.mark.asyncio
async def test_active_managed_scope_renews_coder_deadline_and_activity(tmp_path: Path) -> None:
    client = _FakeClient()
    manager = _manager(tmp_path, client)
    workspace = await manager.ensure_ready("dashboard:one")
    client.active_scopes.add(workspace.name)

    renewed = await manager.reconcile_active_scopes(now="2026-08-25T13:00:00Z")

    assert renewed == (workspace.name,)
    assert client.extended == [(workspace.name, 30)]
    binding = manager.registry.get_by_session("dashboard:one")
    assert binding is not None
    assert binding.last_activity_at == "2026-08-25T13:00:00Z"


@pytest.mark.asyncio
async def test_no_scope_does_not_renew_running_workspace(tmp_path: Path) -> None:
    client = _FakeClient()
    manager = _manager(tmp_path, client)
    await manager.ensure_ready("dashboard:one")

    assert await manager.reconcile_active_scopes() == ()
    assert client.extended == []
