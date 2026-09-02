"""Per-parent Coder allocation and lifecycle coordination."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import replace
from pathlib import Path

import pytest

from kiro_crew.coder.client import CoderClientError, CoderWorkspace, CoderWorkspaceMemory
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
        self.current_user_calls = 0
        self.memory_probes: list[str] = []
        self.get_calls = 0
        self.list_calls = 0
        self.lifecycle_events: list[str] = []

    async def current_user(self) -> tuple[str, str]:
        self.current_user_calls += 1
        return "kyleseaman", "owner-kyleseaman"

    async def get_workspace(self, name: str) -> CoderWorkspace | None:
        self.get_calls += 1
        return self.workspaces.get(name)

    async def list_workspaces(self) -> tuple[CoderWorkspace, ...]:
        self.list_calls += 1
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
        self.lifecycle_events.append(f"stop:{name}")
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
        self.lifecycle_events.append(f"renew:{name}")
        self.extended.append((name, minutes))

    async def workspace_memory(self, name: str) -> CoderWorkspaceMemory:
        self.memory_probes.append(name)
        return CoderWorkspaceMemory(2.0, 8.0, 75.0, "normal")


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
async def test_unrelated_workspace_starts_can_provision_in_parallel(tmp_path: Path) -> None:
    client = _FakeClient()
    manager = _manager(tmp_path, client)
    entered: set[str] = set()
    both_entered = asyncio.Event()
    release = asyncio.Event()
    create = client.create_workspace

    async def blocked_create(**kwargs: object) -> CoderWorkspace:
        entered.add(str(kwargs["name"]))
        if len(entered) == 2:
            both_entered.set()
        await release.wait()
        return await create(**kwargs)  # type: ignore[arg-type]

    client.create_workspace = blocked_create  # type: ignore[method-assign]
    first = asyncio.create_task(manager.ensure_ready("dashboard:one"))
    second = asyncio.create_task(manager.ensure_ready("dashboard:two"))
    try:
        await asyncio.wait_for(both_entered.wait(), timeout=0.5)
    finally:
        release.set()
    await asyncio.gather(first, second)

    assert len(entered) == 2


@pytest.mark.asyncio
async def test_workspace_start_reports_provider_neutral_progress(tmp_path: Path) -> None:
    client = _FakeClient()
    manager = _manager(tmp_path, client)
    progress: list[tuple[str, str]] = []

    workspace = await manager.ensure_ready(
        "dashboard:one",
        on_progress=lambda phase, name: progress.append((phase, name)),
    )

    assert progress == [
        ("allocating", ""),
        ("provisioning", workspace.name),
        ("connecting", workspace.name),
    ]


@pytest.mark.asyncio
async def test_running_workspace_reports_only_connection_progress(tmp_path: Path) -> None:
    """Reconstructing a runtime must not claim that warm compute is provisioning."""
    client = _FakeClient()
    manager = _manager(tmp_path, client)
    workspace = await manager.ensure_ready("dashboard:one")
    progress: list[tuple[str, str]] = []

    reused = await manager.ensure_ready(
        "dashboard:one",
        on_progress=lambda phase, name: progress.append((phase, name)),
    )

    assert reused == workspace
    assert progress == [("connecting", workspace.name)]
    assert client.created == [workspace.name]
    assert client.started == []


@pytest.mark.asyncio
async def test_running_binding_reconnect_skips_redundant_identity_lookup(tmp_path: Path) -> None:
    """The immutable binding already owns the allocation identity on reconnect."""
    client = _FakeClient()
    manager = _manager(tmp_path, client)
    workspace = await manager.ensure_ready("dashboard:one")
    assert client.current_user_calls == 1

    reused = await manager.ensure_ready("dashboard:one")

    assert reused == workspace
    assert client.current_user_calls == 1


@pytest.mark.asyncio
async def test_prefetch_reuses_running_workspace_without_starting_compute(tmp_path: Path) -> None:
    client = _FakeClient()
    manager = _manager(tmp_path, client)
    workspace = await manager.ensure_ready("dashboard:one")

    reused = await manager.ensure_ready("dashboard:one", allow_start=False)

    assert reused == workspace
    assert client.created == [workspace.name]
    assert client.started == []


@pytest.mark.asyncio
async def test_prefetch_refuses_stopped_workspace_without_starting_compute(tmp_path: Path) -> None:
    from kiro_crew.session_environment import SessionEnvironmentPrefetchUnavailable

    client = _FakeClient()
    manager = _manager(tmp_path, client)
    workspace = await manager.ensure_ready("dashboard:one")
    client.workspaces[workspace.name] = CoderWorkspace(
        **{**workspace.__dict__, "status": "stopped"}
    )

    with pytest.raises(SessionEnvironmentPrefetchUnavailable):
        await manager.ensure_ready("dashboard:one", allow_start=False)

    assert client.started == []


@pytest.mark.asyncio
async def test_prefetch_refuses_unallocated_session_without_creating_compute(
    tmp_path: Path,
) -> None:
    from kiro_crew.session_environment import SessionEnvironmentPrefetchUnavailable

    client = _FakeClient()
    manager = _manager(tmp_path, client)

    with pytest.raises(SessionEnvironmentPrefetchUnavailable):
        await manager.ensure_ready("dashboard:absent", allow_start=False)

    assert client.current_user_calls == 0
    assert client.created == []


@pytest.mark.asyncio
async def test_stopped_workspace_reports_compute_start_before_connection(tmp_path: Path) -> None:
    """A real wake earns the compute-start phase that a warm reuse skips."""
    client = _FakeClient()
    manager = _manager(tmp_path, client)
    workspace = await manager.ensure_ready("dashboard:one")
    await manager.stop_for_session("dashboard:one")
    progress: list[tuple[str, str]] = []

    resumed = await manager.ensure_ready(
        "dashboard:one",
        on_progress=lambda phase, name: progress.append((phase, name)),
    )

    assert resumed.uuid == workspace.uuid
    assert progress == [
        ("provisioning", workspace.name),
        ("connecting", workspace.name),
    ]


@pytest.mark.asyncio
async def test_legacy_unprovisioned_name_is_repaired_before_create(tmp_path: Path) -> None:
    client = _FakeClient()
    manager = _manager(tmp_path, client)
    binding = manager.registry.allocate(
        "dashboard:one",
        template="kirocrew-arm",
        preset="arm-small",
        prefix="crew-session",
        owner_name="kyleseaman",
    )
    payload = json.loads(manager.registry.path.read_text(encoding="utf-8"))
    payload["bindings"][binding.binding_id]["workspace_name"] = "crew-session-kyleseaman-yjb-GQN_"
    manager.registry.path.write_text(json.dumps(payload), encoding="utf-8")

    workspace = await manager.ensure_ready("dashboard:one")

    assert workspace.name != "crew-session-kyleseaman-yjb-GQN_"
    assert re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", workspace.name)
    repaired = manager.registry.get_by_session("dashboard:one")
    assert repaired is not None
    assert repaired.workspace_name == workspace.name
    assert repaired.generation == binding.generation + 1


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
async def test_stop_request_is_durable_before_control_plane_stop(tmp_path: Path) -> None:
    client = _FakeClient()
    manager = _manager(tmp_path, client)
    workspace = await manager.ensure_ready("dashboard:one")
    release = asyncio.Event()

    async def blocked_stop(name: str) -> CoderWorkspace:
        await release.wait()
        return await _FakeClient.stop_workspace(client, name)

    client.stop_workspace = blocked_stop  # type: ignore[method-assign]

    requested = await manager.request_stop_for_session("dashboard:one")

    assert requested == workspace.name
    binding = manager.registry.get_by_session("dashboard:one")
    assert binding is not None
    assert binding.state == "stop_pending"
    release.set()
    await manager.wait_for_pending_stops()
    assert manager.registry.get_by_session("dashboard:one").state == "stopped"


@pytest.mark.asyncio
async def test_lifecycle_retries_a_durable_stop_request(tmp_path: Path) -> None:
    client = _FakeClient()
    manager = _manager(tmp_path, client)
    workspace = await manager.ensure_ready("dashboard:one")
    binding = manager.registry.get_by_session("dashboard:one")
    assert binding is not None
    manager.registry.replace(replace(binding, state="stop_pending"))

    stopped = await manager.reconcile_stop_requests()

    assert stopped == (workspace.name,)
    assert manager.registry.get_by_session("dashboard:one").state == "stopped"


@pytest.mark.asyncio
async def test_lifecycle_continues_after_one_stop_request_fails(tmp_path: Path) -> None:
    client = _FakeClient()
    manager = _manager(tmp_path, client)
    first = await manager.ensure_ready("dashboard:one")
    second = await manager.ensure_ready("dashboard:two")
    for session_key in ("dashboard:one", "dashboard:two"):
        binding = manager.registry.get_by_session(session_key)
        assert binding is not None
        manager.registry.replace(replace(binding, state="stop_pending"))
    stop = client.stop_workspace

    async def fail_first_stop(name: str) -> CoderWorkspace:
        if name == first.name:
            raise RuntimeError("unexpected provider failure")
        return await stop(name)

    client.stop_workspace = fail_first_stop  # type: ignore[method-assign]

    stopped = await manager.reconcile_stop_requests()

    assert stopped == (second.name,)
    assert manager.registry.get_by_session("dashboard:one").state == "stop_pending"
    assert manager.registry.get_by_session("dashboard:two").state == "stopped"


@pytest.mark.asyncio
async def test_lifecycle_pass_renews_before_stops_with_one_workspace_inventory(
    tmp_path: Path,
) -> None:
    client = _FakeClient()
    manager = _manager(tmp_path, client)
    active = await manager.ensure_ready("dashboard:active")
    pending = await manager.ensure_ready("dashboard:pending")
    client.active_scopes.add(active.name)
    binding = manager.registry.get_by_session("dashboard:pending")
    assert binding is not None
    manager.registry.replace(replace(binding, state="stop_pending"))
    client.list_calls = 0
    client.get_calls = 0
    client.lifecycle_events.clear()

    await manager.reconcile_lifecycle(now="2026-08-25T13:00:00Z")

    assert client.list_calls == 1
    assert client.get_calls == 0
    assert client.lifecycle_events == [f"renew:{active.name}", f"stop:{pending.name}"]


@pytest.mark.asyncio
async def test_stale_lifecycle_inventory_cannot_settle_a_new_stop_request(tmp_path: Path) -> None:
    client = _FakeClient()
    manager = _manager(tmp_path, client)
    workspace = await manager.ensure_ready("dashboard:one")
    binding = manager.registry.get_by_session("dashboard:one")
    assert binding is not None
    manager.registry.replace(replace(binding, state="stop_pending"))
    pending = manager.registry.get_by_session("dashboard:one")
    assert pending is not None

    stopped = await manager.reconcile_stop_requests(
        workspace_inventory={},
        bindings=(pending,),
    )

    assert stopped == ()
    assert client.stopped == []
    assert manager.registry.get_by_session("dashboard:one").state == "stop_pending"
    assert workspace.name in client.workspaces


@pytest.mark.asyncio
async def test_lifecycle_checks_independent_workspaces_concurrently(tmp_path: Path) -> None:
    client = _FakeClient()
    manager = _manager(tmp_path, client)
    first = await manager.ensure_ready("dashboard:first")
    second = await manager.ensure_ready("dashboard:second")
    entered: set[str] = set()
    both_entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_scope_check(name: str) -> bool:
        entered.add(name)
        if len(entered) == 2:
            both_entered.set()
        await release.wait()
        return False

    client.has_active_workload_scope = blocked_scope_check  # type: ignore[method-assign]
    task = asyncio.create_task(manager.reconcile_lifecycle())
    try:
        await asyncio.wait_for(both_entered.wait(), timeout=0.5)
    finally:
        release.set()
    await task

    assert entered == {first.name, second.name}


@pytest.mark.asyncio
async def test_resume_settles_pending_stop_before_restarting_workspace(tmp_path: Path) -> None:
    client = _FakeClient()
    manager = _manager(tmp_path, client)
    workspace = await manager.ensure_ready("dashboard:one")
    binding = manager.registry.get_by_session("dashboard:one")
    assert binding is not None
    manager.registry.replace(replace(binding, state="stop_pending"))

    resumed = await manager.ensure_ready("dashboard:one")

    assert resumed.uuid == workspace.uuid
    assert client.stopped == [workspace.name]
    assert client.started == [workspace.name]
    assert manager.registry.get_by_session("dashboard:one").state == "running"


@pytest.mark.asyncio
async def test_running_capacity_refuses_an_unrelated_start(tmp_path: Path) -> None:
    client = _FakeClient()
    manager = _manager(tmp_path, client)
    manager.policy = ManagedWorkspacePolicy(**{**manager.policy.__dict__, "max_running": 1})
    await manager.ensure_ready("dashboard:one")

    with pytest.raises(CoderCapacityError):
        await manager.ensure_ready("dashboard:two")


@pytest.mark.asyncio
async def test_parallel_capacity_counts_inflight_workspace_start(tmp_path: Path) -> None:
    client = _FakeClient()
    manager = _manager(tmp_path, client)
    manager.policy = ManagedWorkspacePolicy(**{**manager.policy.__dict__, "max_running": 1})
    entered = asyncio.Event()
    release = asyncio.Event()
    create = client.create_workspace

    async def blocked_create(**kwargs: object) -> CoderWorkspace:
        entered.set()
        await release.wait()
        return await create(**kwargs)  # type: ignore[arg-type]

    client.create_workspace = blocked_create  # type: ignore[method-assign]
    first = asyncio.create_task(manager.ensure_ready("dashboard:one"))
    await entered.wait()
    try:
        with pytest.raises(CoderCapacityError):
            await asyncio.wait_for(manager.ensure_ready("dashboard:two"), timeout=0.5)
    finally:
        release.set()
    await first


@pytest.mark.asyncio
async def test_parallel_capacity_does_not_double_count_visible_reservation(
    tmp_path: Path,
) -> None:
    client = _FakeClient()
    manager = _manager(tmp_path, client)
    manager.policy = ManagedWorkspacePolicy(**{**manager.policy.__dict__, "max_running": 2})
    entered = asyncio.Event()
    release = asyncio.Event()
    create = client.create_workspace
    calls = 0

    async def publish_first_start(**kwargs: object) -> CoderWorkspace:
        nonlocal calls
        calls += 1
        if calls == 1:
            name = str(kwargs["name"])
            client.workspaces[name] = CoderWorkspace(
                uuid=f"uuid-{name}",
                name=name,
                owner="kyleseaman",
                template=str(kwargs["template"]),
                status="starting",
                last_used_at="2026-08-25T12:00:00Z",
            )
            entered.set()
            await release.wait()
        return await create(**kwargs)  # type: ignore[arg-type]

    client.create_workspace = publish_first_start  # type: ignore[method-assign]
    first = asyncio.create_task(manager.ensure_ready("dashboard:one"))
    await entered.wait()
    try:
        second = await manager.ensure_ready("dashboard:two")
    finally:
        release.set()
    await first

    assert second.name in client.created
    assert len(client.created) == 2


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


@pytest.mark.asyncio
async def test_inspect_session_health_probes_only_exact_running_binding(tmp_path: Path) -> None:
    client = _FakeClient()
    manager = _manager(tmp_path, client)
    workspace = await manager.ensure_ready("dashboard:one")

    inspected, memory = await manager.inspect_session_health("dashboard:one")

    assert inspected == workspace
    assert memory == CoderWorkspaceMemory(2.0, 8.0, 75.0, "normal")
    assert client.memory_probes == [workspace.name]


@pytest.mark.asyncio
async def test_inspect_session_health_does_not_probe_stopped_compute(tmp_path: Path) -> None:
    client = _FakeClient()
    manager = _manager(tmp_path, client)
    await manager.ensure_ready("dashboard:one")
    await manager.stop_for_session("dashboard:one")

    inspected, memory = await manager.inspect_session_health("dashboard:one")

    assert inspected.status == "stopped"
    assert memory is None
    assert client.memory_probes == []


@pytest.mark.asyncio
async def test_inspect_session_health_keeps_running_state_when_memory_probe_fails(
    tmp_path: Path,
) -> None:
    client = _FakeClient()
    manager = _manager(tmp_path, client)
    workspace = await manager.ensure_ready("dashboard:one")

    async def unavailable_memory(_name: str) -> CoderWorkspaceMemory:
        raise CoderClientError("memory probe unavailable")

    client.workspace_memory = unavailable_memory  # type: ignore[method-assign]

    inspected, memory = await manager.inspect_session_health("dashboard:one")

    assert inspected == workspace
    assert memory is None
