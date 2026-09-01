"""Dashboard contract for gateway-owned Coder session hosting settings."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import kiro_crew.config.loader as loader_mod
from kiro_crew.acp.session_host import CODER_SESSION_TOKEN_SECRET
from kiro_crew.constants import (
    CODER_DEFAULT_RUNTIME_WARM_MINUTES,
    CODER_MAX_PROFILES,
    CODER_WORKSPACE_PREFIX_MAX_CHARS,
)
from kiro_crew.dashboard.handlers import coder as coder_handlers
from kiro_crew.secrets import SecretVault


class _Sessions:
    def __init__(self) -> None:
        self.refresh_defaults = AsyncMock()


class _State:
    owner_id = ""

    def __init__(self) -> None:
        self.sessions = _Sessions()


def test_default_runtime_warm_window_avoids_per_message_cold_starts() -> None:
    assert CODER_DEFAULT_RUNTIME_WARM_MINUTES == 15


def _app() -> web.Application:
    @web.middleware
    async def _identity(request, handler):
        request["user"] = request.headers.get("X-Test-User", "local-app")
        request["app"] = request.headers.get("X-Test-App", "")
        return await handler(request)

    app = web.Application(middlewares=[_identity])
    app["state"] = _State()
    coder_handlers.setup_coder_routes(app)
    return app


@pytest.fixture()
def coder_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Route every config/vault access into the test-owned home."""
    cfg_path = tmp_path / "config.json"
    monkeypatch.setattr(coder_handlers, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(coder_handlers, "config_path", lambda: cfg_path)
    monkeypatch.setattr(loader_mod, "config_path", lambda: cfg_path)
    return cfg_path


def _write_enabled_config(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "session": {
                    "coder": {
                        "enabled": True,
                        "url": "https://coder.example",
                        "template": "kirocrew-arm",
                        "preset": "arm-small",
                        "profiles": {
                            "gpu": {
                                "template": "kirocrew-gpu",
                                "preset": "gpu-medium",
                            }
                        },
                        "remote_cwd": "/home/coder/workspace",
                        "runtime_warm_minutes": 5,
                        "stop_after_minutes": 30,
                        "delete_after_days": 30,
                        "max_running": 3,
                        "workspace_prefix": "crew",
                    }
                }
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_get_returns_only_masked_token_status(coder_paths: Path, tmp_path: Path) -> None:
    """A dashboard read can learn presence, never recover the Coder bearer."""
    _write_enabled_config(coder_paths)
    SecretVault(tmp_path)._set_sync(CODER_SESSION_TOKEN_SECRET, "coder-secret-must-not-return")
    app = _app()

    async with TestClient(TestServer(app)) as client:
        response = await client.get("/api/coder/config")
        payload = await response.json()

    assert response.status == 200
    assert payload == {
        "enabled": True,
        "url": "https://coder.example",
        "template": "kirocrew-arm",
        "preset": "arm-small",
        "profiles": {"gpu": {"template": "kirocrew-gpu", "preset": "gpu-medium"}},
        "remote_cwd": "/home/coder/workspace",
        "runtime_warm_minutes": 5,
        "stop_after_minutes": 30,
        "delete_after_days": 30,
        "max_running": 3,
        "workspace_prefix": "crew",
        "token_configured": True,
        "legacy_environment": False,
        "limits": {
            "max_profiles": CODER_MAX_PROFILES,
            "workspace_prefix_max_chars": CODER_WORKSPACE_PREFIX_MAX_CHARS,
        },
    }
    assert "coder-secret-must-not-return" not in json.dumps(payload)
    assert "token" not in payload


@pytest.mark.asyncio
async def test_put_persists_non_secret_config_and_vault_token(
    coder_paths: Path, tmp_path: Path
) -> None:
    """Saving catches split storage regressions and refreshes only new defaults."""
    app = _app()

    async with TestClient(TestServer(app)) as client:
        response = await client.put(
            "/api/coder/config",
            json={
                "enabled": True,
                "url": "https://coder.example",
                "template": "kirocrew-arm",
                "preset": "arm-small",
                "profiles": {"gpu": {"template": "kirocrew-gpu", "preset": "gpu-medium"}},
                "remote_cwd": "/home/coder/project",
                "runtime_warm_minutes": 7,
                "stop_after_minutes": 45,
                "delete_after_days": 21,
                "max_running": 2,
                "workspace_prefix": "dogfood",
                "token": "new-coder-secret",
            },
        )
        payload = await response.json()

    assert response.status == 200
    assert payload == {
        "ok": True,
        "token_configured": True,
        "active_sessions_unchanged": True,
    }
    assert "new-coder-secret" not in json.dumps(payload)
    stored = json.loads(coder_paths.read_text(encoding="utf-8"))
    assert stored["session"]["coder"] == {
        "enabled": True,
        "url": "https://coder.example",
        "template": "kirocrew-arm",
        "preset": "arm-small",
        "profiles": {"gpu": {"template": "kirocrew-gpu", "preset": "gpu-medium"}},
        "remote_cwd": "/home/coder/project",
        "runtime_warm_minutes": 7,
        "stop_after_minutes": 45,
        "delete_after_days": 21,
        "max_running": 2,
        "workspace_prefix": "dogfood",
    }
    assert "token" not in json.dumps(stored)
    assert SecretVault(tmp_path).get(CODER_SESSION_TOKEN_SECRET).reveal() == "new-coder-secret"
    app["state"].sessions.refresh_defaults.assert_awaited_once()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_put_rejects_enabled_config_without_a_token(
    coder_paths: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partial remote default cannot be saved into a silent local fallback."""
    monkeypatch.delenv("CODER_SESSION_TOKEN", raising=False)
    app = _app()

    async with TestClient(TestServer(app)) as client:
        response = await client.put(
            "/api/coder/config",
            json={
                "enabled": True,
                "url": "https://coder.example",
                "template": "kirocrew-arm",
                "preset": "",
                "profiles": {},
                "remote_cwd": "/home/coder/workspace",
                "runtime_warm_minutes": 5,
                "stop_after_minutes": 30,
                "delete_after_days": 30,
                "max_running": 3,
                "workspace_prefix": "crew",
                "token": "",
            },
        )
        payload = await response.json()

    assert response.status == 400
    assert payload["code"] == "coder_token_required"
    assert not coder_paths.exists()


@pytest.mark.asyncio
async def test_put_rejects_prefix_too_long_for_generated_workspace_names(
    coder_paths: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODER_SESSION_TOKEN", "existing-token")
    app = _app()

    async with TestClient(TestServer(app)) as client:
        response = await client.put(
            "/api/coder/config",
            json={
                "enabled": True,
                "url": "https://coder.example",
                "template": "kirocrew-arm",
                "preset": "",
                "profiles": {},
                "remote_cwd": "/home/coder/workspace",
                "runtime_warm_minutes": 5,
                "stop_after_minutes": 30,
                "delete_after_days": 30,
                "max_running": 3,
                "workspace_prefix": "x" * (CODER_WORKSPACE_PREFIX_MAX_CHARS + 1),
                "token": "",
            },
        )
        payload = await response.json()

    assert response.status == 400
    assert payload["code"] == "coder_config_invalid"


@pytest.mark.asyncio
async def test_connection_probe_uses_candidate_values_without_echoing_token(
    coder_paths: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test Connection may use an unsaved token but must never return it."""
    probe = AsyncMock(return_value={"owner": "kyleseaman", "template": "kirocrew-arm"})
    monkeypatch.setattr(coder_handlers, "probe_coder_managed_config", probe, raising=False)
    app = _app()

    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/api/coder/test",
            json={
                "url": "https://candidate-coder.example",
                "template": "kirocrew-arm",
                "preset": "arm-small",
                "remote_cwd": "/home/coder/workspace",
                "token": "candidate-secret",
            },
        )
        payload = await response.json()

    assert response.status == 200
    assert payload == {"ok": True, "owner": "kyleseaman", "template": "kirocrew-arm"}
    assert "candidate-secret" not in json.dumps(payload)
    probe.assert_awaited_once()
    assert probe.await_args.kwargs["token"] == "candidate-secret"
    assert probe.await_args.kwargs["url"] == "https://candidate-coder.example"
    assert probe.await_args.kwargs["template"] == "kirocrew-arm"
    assert probe.await_args.kwargs["preset"] == "arm-small"


def test_loader_resolves_named_profiles_without_changing_the_default(
    coder_paths: Path,
) -> None:
    _write_enabled_config(coder_paths)

    coder = loader_mod.KiroCrewConfig.load().session.coder

    assert coder.resolve_profile("") == ("kirocrew-arm", "arm-small")
    assert coder.resolve_profile("gpu") == ("kirocrew-gpu", "gpu-medium")
    with pytest.raises(ValueError, match="Unknown Coder profile"):
        coder.resolve_profile("missing")


@pytest.mark.asyncio
async def test_put_rejects_unsafe_profile_names_before_writing(coder_paths: Path) -> None:
    app = _app()

    async with TestClient(TestServer(app)) as client:
        response = await client.put(
            "/api/coder/config",
            json={
                "enabled": True,
                "url": "https://coder.example",
                "template": "kirocrew-arm",
                "preset": "",
                "profiles": {"gpu;bad": {"template": "kirocrew-gpu", "preset": ""}},
                "remote_cwd": "/home/coder/workspace",
                "runtime_warm_minutes": 5,
                "stop_after_minutes": 30,
                "delete_after_days": 30,
                "max_running": 3,
                "workspace_prefix": "crew",
                "token": "candidate-secret",
            },
        )
        payload = await response.json()

    assert response.status == 400
    assert payload["code"] == "coder_profiles_invalid"
    assert not coder_paths.exists()


def test_loader_exposes_managed_workspace_policy_defaults(
    coder_paths: Path,
) -> None:
    coder_paths.write_text(
        json.dumps({"session": {"coder": {"enabled": True}}}),
        encoding="utf-8",
    )

    coder = loader_mod.KiroCrewConfig.load().session.coder

    assert coder.template == ""
    assert coder.preset == ""
    assert coder.remote_cwd == "/home/coder/workspace"
    assert coder.runtime_warm_minutes == 15
    assert coder.stop_after_minutes == 30
    assert coder.delete_after_days == 30
    assert coder.max_running == 3
    assert coder.workspace_prefix == "crew-session"
    assert not hasattr(coder, "workspace")


@pytest.mark.asyncio
async def test_coder_settings_are_owner_only(coder_paths: Path) -> None:
    """An app-scoped token cannot enumerate or replace gateway credentials."""
    app = _app()

    async with TestClient(TestServer(app)) as client:
        response = await client.get(
            "/api/coder/config",
            headers={"X-Test-App": "some-app", "X-Test-User": "some-app-subject"},
        )
        payload = await response.json()

    assert response.status == 403
    assert payload["code"] == "owner_only"
