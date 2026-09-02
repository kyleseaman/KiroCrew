from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


def test_public_binding_serializes_provider_neutral_identity() -> None:
    from kiro_crew.session_environment import SessionEnvironmentBinding

    binding = SessionEnvironmentBinding(
        provider="coder",
        configuration="gpu",
        resource_name="crew-session-kyle-opaque",
    )

    assert binding.to_dict() == {
        "provider": "coder",
        "configuration": "gpu",
        "resource_name": "crew-session-kyle-opaque",
    }


def test_binding_rejects_invalid_provider_identifier() -> None:
    from kiro_crew.session_environment import SessionEnvironmentBinding

    with pytest.raises(ValueError, match="provider"):
        SessionEnvironmentBinding(provider="Coder URL", configuration="default")


def test_binding_parses_only_complete_public_shape() -> None:
    from kiro_crew.session_environment import SessionEnvironmentBinding

    assert SessionEnvironmentBinding.from_dict(
        {"provider": "coder", "configuration": "gpu", "resource_name": "crew-one"}
    ) == SessionEnvironmentBinding("coder", "gpu", "crew-one")
    assert SessionEnvironmentBinding.from_dict({"provider": "coder"}) is None
    assert SessionEnvironmentBinding.from_dict({"provider": "Coder URL"}) is None


def test_registry_fails_closed_for_unknown_provider() -> None:
    from kiro_crew.session_environment import (
        SessionEnvironmentRegistry,
        SessionEnvironmentUnavailable,
    )

    registry = SessionEnvironmentRegistry([])

    with pytest.raises(SessionEnvironmentUnavailable, match="unavailable"):
        registry.require("coder")


def test_registry_catalog_contains_only_public_provider_metadata() -> None:
    from kiro_crew.session_environment import (
        SessionEnvironmentBinding,
        SessionEnvironmentConfiguration,
        SessionEnvironmentRegistry,
    )

    @dataclass
    class FakeProvider:
        provider_id: str = "fake"
        display_name: str = "Fake Compute"
        icon: str = "server"
        secret: str = "must-not-leak"

        def configurations(self) -> tuple[SessionEnvironmentConfiguration, ...]:
            return (SessionEnvironmentConfiguration(id="small", name="Small"),)

        def validate_configuration(self, configuration: str) -> str:
            return configuration

        def binding_for_session(self, session_key: str) -> SessionEnvironmentBinding | None:
            return None

        def create_session_host(self, session_key: str, configuration: str) -> object:
            return object()

        async def stop_for_session(self, session_key: str) -> str | None:
            return None

    registry = SessionEnvironmentRegistry([FakeProvider()])

    assert registry.catalog() == [
        {
            "id": "fake",
            "name": "Fake Compute",
            "icon": "server",
            "configurations": [{"id": "small", "name": "Small"}],
        }
    ]


def test_coder_adapter_creates_managed_host_for_selected_configuration() -> None:
    from kiro_crew.acp.session_host import ManagedCoderWorkspaceSessionHost
    from kiro_crew.session_environment import CoderSessionEnvironmentProvider

    manager = MagicMock()
    manager.registry.get_by_session.return_value = None
    provider = CoderSessionEnvironmentProvider(
        manager=manager,
        configurations={"gpu": ("kirocrew-gpu", "gpu-medium")},
        default_template="kirocrew-arm",
        default_preset="",
        remote_cwd="/home/coder/workspace",
        coder_bin="/opt/coder",
        coder_url="https://coder.example",
        session_token="secret",
        runtime_warm_minutes=5,
    )

    host = provider.create_session_host("dashboard:chat-1", "gpu")

    assert isinstance(host, ManagedCoderWorkspaceSessionHost)
    assert (host._template, host._preset) == ("kirocrew-gpu", "gpu-medium")
    assert host._allow_start is True
    assert provider.runtime_warm_seconds == 300


def test_coder_adapter_prefetch_host_cannot_start_compute() -> None:
    from kiro_crew.acp.session_host import ManagedCoderWorkspaceSessionHost
    from kiro_crew.session_environment import CoderSessionEnvironmentProvider

    manager = MagicMock()
    manager.registry.get_by_session.return_value = SimpleNamespace(
        workspace_name="crew-session-kyle-opaque",
        workspace_uuid="workspace-uuid",
        state="running",
    )
    provider = CoderSessionEnvironmentProvider(
        manager=manager,
        configurations={},
        default_template="kirocrew-arm",
        default_preset="",
        remote_cwd="/home/coder/workspace",
        coder_bin="/opt/coder",
        coder_url="https://coder.example",
        session_token="secret",
        runtime_warm_minutes=5,
    )

    host = provider.create_prefetch_session_host("dashboard:chat-1", "")

    assert isinstance(host, ManagedCoderWorkspaceSessionHost)
    assert host._allow_start is False


def test_registry_prefetch_support_is_positive_opt_in() -> None:
    from kiro_crew.session_environment import (
        SessionEnvironmentPrefetchProvider,
        SessionEnvironmentRegistry,
        SessionEnvironmentSelection,
    )

    class GenericProvider:
        provider_id = "generic"

    class PrefetchProvider(SessionEnvironmentPrefetchProvider):
        provider_id = "prefetch"

        def can_prefetch_session(self, session_key: str) -> bool:
            return session_key == "dashboard:ready"

    registry = SessionEnvironmentRegistry(
        [GenericProvider(), PrefetchProvider()],  # type: ignore[list-item]
        default_provider_id="prefetch",
    )

    assert registry.supports_prefetch("dashboard:ready", None) is True
    assert (
        registry.supports_prefetch("dashboard:ready", SessionEnvironmentSelection("prefetch"))
        is True
    )
    assert registry.supports_prefetch("dashboard:cold", None) is False
    assert (
        registry.supports_prefetch("dashboard:ready", SessionEnvironmentSelection("generic"))
        is False
    )


def test_coder_adapter_projects_only_safe_binding_identity() -> None:
    from kiro_crew.session_environment import CoderSessionEnvironmentProvider

    manager = MagicMock()
    manager.registry.get_by_session.return_value = SimpleNamespace(
        workspace_name="crew-session-kyle-opaque",
        workspace_uuid="secret-uuid",
        owner_id="secret-owner",
    )
    provider = CoderSessionEnvironmentProvider(
        manager=manager,
        configurations={},
        default_template="kirocrew-arm",
        default_preset="",
        remote_cwd="/home/coder/workspace",
        coder_bin="/opt/coder",
        coder_url="https://coder.example",
        session_token="secret",
        runtime_warm_minutes=5,
    )

    binding = provider.binding_for_session("dashboard:chat-1")

    assert binding is not None
    assert binding.to_dict() == {
        "provider": "coder",
        "configuration": "",
        "resource_name": "crew-session-kyle-opaque",
    }
    assert "uuid" not in binding.to_dict()
    assert "owner" not in binding.to_dict()


@pytest.mark.asyncio
async def test_coder_adapter_stops_only_by_session_key() -> None:
    from kiro_crew.session_environment import CoderSessionEnvironmentProvider

    manager = MagicMock()
    manager.stop_for_session = AsyncMock(return_value="crew-session-kyle-opaque")
    provider = CoderSessionEnvironmentProvider(
        manager=manager,
        configurations={},
        default_template="kirocrew-arm",
        default_preset="",
        remote_cwd="/home/coder/workspace",
        coder_bin="/opt/coder",
        coder_url="https://coder.example",
        session_token="secret",
        runtime_warm_minutes=5,
    )

    stopped = await provider.stop_for_session("dashboard:chat-1")

    assert stopped == "crew-session-kyle-opaque"
    manager.stop_for_session.assert_awaited_once_with("dashboard:chat-1")


@pytest.mark.asyncio
async def test_coder_lifecycle_rejects_catalog_shaped_manager_authority() -> None:
    from kiro_crew.session_environment import CoderSessionEnvironmentProvider

    manager = MagicMock()
    manager.reconcile_active_scopes = AsyncMock()
    manager.reconcile_retention = AsyncMock()
    provider = CoderSessionEnvironmentProvider(
        manager=manager,
        configurations={},
        default_template="kirocrew-arm",
        default_preset="",
        remote_cwd="/home/coder/workspace",
        coder_bin="/opt/coder",
        coder_url="https://coder.example",
        session_token="secret",
        runtime_warm_minutes=5,
    )

    assert provider.lifecycle_interval_seconds is None
    await provider.reconcile_lifecycle()

    manager.reconcile_active_scopes.assert_not_awaited()
    manager.reconcile_retention.assert_not_awaited()


@pytest.mark.asyncio
async def test_coder_adapter_delegates_one_atomic_lifecycle_pass(monkeypatch) -> None:
    from kiro_crew.coder.manager import CoderWorkspaceManager
    from kiro_crew.session_environment import CoderSessionEnvironmentProvider

    manager = object.__new__(CoderWorkspaceManager)
    reconcile = AsyncMock()
    monkeypatch.setattr(manager, "reconcile_lifecycle", reconcile)
    provider = CoderSessionEnvironmentProvider(
        manager=manager,
        configurations={},
        default_template="kirocrew-arm",
        default_preset="",
        remote_cwd="/home/coder/workspace",
        coder_bin="/opt/coder",
        coder_url="https://coder.example",
        session_token="secret",
        runtime_warm_minutes=5,
    )

    await provider.reconcile_lifecycle()

    reconcile.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_coder_adapter_drains_pending_stops_during_shutdown(monkeypatch) -> None:
    from kiro_crew.coder.manager import CoderWorkspaceManager
    from kiro_crew.session_environment import CoderSessionEnvironmentProvider

    manager = object.__new__(CoderWorkspaceManager)
    drain = AsyncMock()
    monkeypatch.setattr(manager, "wait_for_pending_stops", drain)
    provider = CoderSessionEnvironmentProvider(
        manager=manager,
        configurations={},
        default_template="kirocrew-arm",
        default_preset="",
        remote_cwd="/home/coder/workspace",
        coder_bin="/opt/coder",
        coder_url="https://coder.example",
        session_token="secret",
        runtime_warm_minutes=5,
    )

    await provider.shutdown_lifecycle()

    drain.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_gateway_cleanup_drains_provider_lifecycle_work() -> None:
    from aiohttp import web

    from kiro_crew.dashboard.server import _register_session_environment_lifecycle
    from kiro_crew.session_environment import SessionEnvironmentLifecycleProvider

    class ProbeProvider(SessionEnvironmentLifecycleProvider):
        def __init__(self) -> None:
            self.shutdown = AsyncMock()

        async def shutdown_lifecycle(self) -> None:
            await self.shutdown()

    provider = ProbeProvider()
    registry = SimpleNamespace(providers=lambda: (provider,))
    state = SimpleNamespace(
        sessions=SimpleNamespace(environment_registry=lambda: registry),
        _coder_lifecycle_task=asyncio.create_task(asyncio.sleep(60)),
    )
    app = web.Application()
    _register_session_environment_lifecycle(app, state)

    await app.on_cleanup[-1](app)

    provider.shutdown.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_coder_adapter_projects_provider_neutral_memory_health() -> None:
    from kiro_crew.coder.client import CoderWorkspace, CoderWorkspaceMemory
    from kiro_crew.session_environment import CoderSessionEnvironmentProvider

    manager = MagicMock()
    manager.registry.get_by_session.return_value = SimpleNamespace(
        workspace_name="crew-session-kyle-opaque",
        workspace_uuid="workspace-uuid",
        state="running",
    )
    manager.inspect_session_health = AsyncMock(
        return_value=(
            CoderWorkspace(
                uuid="workspace-uuid",
                name="crew-session-kyle-opaque",
                owner="owner-uuid",
                template="kirocrew-arm",
                status="running",
                last_used_at="2026-09-01T12:00:00Z",
            ),
            CoderWorkspaceMemory(0.4, 4.0, 90.0, "critical"),
        )
    )
    provider = CoderSessionEnvironmentProvider(
        manager=manager,
        configurations={},
        default_template="kirocrew-arm",
        default_preset="",
        remote_cwd="/home/coder/workspace",
        coder_bin="/opt/coder",
        coder_url="https://coder.example",
        session_token="secret",
        runtime_warm_minutes=5,
    )

    health = await provider.health_for_session("dashboard:chat-1")

    assert health.to_dict() == {
        "provider": "coder",
        "resource_name": "crew-session-kyle-opaque",
        "state": "running",
        "memory": {
            "available_gb": 0.4,
            "total_gb": 4.0,
            "used_percent": 90.0,
            "pressure": "critical",
        },
    }
    manager.inspect_session_health.assert_awaited_once_with("dashboard:chat-1")


@pytest.mark.asyncio
async def test_coder_adapter_projects_failed_bootstrap_as_unavailable() -> None:
    from kiro_crew.coder.client import CoderWorkspace, CoderWorkspaceMemory
    from kiro_crew.session_environment import CoderSessionEnvironmentProvider

    manager = MagicMock()
    manager.registry.get_by_session.return_value = SimpleNamespace(
        workspace_name="crew-session-kyle-opaque",
        workspace_uuid="workspace-uuid",
        state="running",
    )
    manager.inspect_session_health = AsyncMock(
        return_value=(
            CoderWorkspace(
                uuid="workspace-uuid",
                name="crew-session-kyle-opaque",
                owner="owner-uuid",
                template="kirocrew-arm",
                status="running",
                last_used_at="2026-09-01T12:00:00Z",
            ),
            CoderWorkspaceMemory(2.0, 8.0, 75.0, "normal", "failed:kiro-cli"),
        )
    )
    provider = CoderSessionEnvironmentProvider(
        manager=manager,
        configurations={},
        default_template="kirocrew-arm",
        default_preset="",
        remote_cwd="/home/coder/workspace",
        coder_bin="/opt/coder",
        coder_url="https://coder.example",
        session_token="secret",
        runtime_warm_minutes=5,
    )

    health = await provider.health_for_session("dashboard:chat-1")

    assert health.state == "unavailable"


def test_health_capability_is_positive_opt_in() -> None:
    from kiro_crew.session_environment import SessionEnvironmentHealthProvider

    assert not isinstance(
        SimpleNamespace(health_for_session=AsyncMock()), SessionEnvironmentHealthProvider
    )


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_memory_health_rejects_non_finite_metrics(value: float) -> None:
    from kiro_crew.session_environment import SessionEnvironmentMemoryHealth

    with pytest.raises(ValueError, match="invalid"):
        SessionEnvironmentMemoryHealth(
            available_gb=value,
            total_gb=4.0,
            used_percent=50.0,
            pressure="normal",
        )
