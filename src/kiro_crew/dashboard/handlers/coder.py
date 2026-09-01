"""Owner-only Coder session-hosting settings and connection probe."""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew.acp.session_host import (
    CODER_SESSION_TOKEN_SECRET,
    SessionHostError,
    validate_coder_remote_cwd,
    validate_coder_url,
    validate_coder_workspace,
)
from kiro_crew.coder.client import CoderClient, CoderClientError
from kiro_crew.config.loader import (
    ConfigReadError,
    KiroCrewConfig,
    config_dir,
    config_path,
    update_config_locked,
)
from kiro_crew.constants import (
    CODER_DEFAULT_DELETE_AFTER_DAYS,
    CODER_DEFAULT_MAX_RUNNING,
    CODER_DEFAULT_REMOTE_CWD,
    CODER_DEFAULT_RUNTIME_WARM_MINUTES,
    CODER_DEFAULT_STOP_AFTER_MINUTES,
    CODER_DEFAULT_WORKSPACE_PREFIX,
    CODER_MAX_PROFILES,
    CODER_WORKSPACE_PREFIX_MAX_CHARS,
)
from kiro_crew.dashboard.handlers.agents import _get_config_lock
from kiro_crew.dashboard.handlers.secrets import _owner_only
from kiro_crew.secrets import SecretVault

_CODER_TOKEN_MAX_BYTES = 16 * 1024


def _error(message: str, code: str, *, status: int = 400) -> web.Response:
    if status == 400:
        return web.json_response({"error": message, "code": code}, status=400)
    if status == 500:
        return web.json_response({"error": message, "code": code}, status=500)
    if status == 502:
        return web.json_response({"error": message, "code": code}, status=502)
    raise ValueError(f"unsupported Coder error status: {status}")


async def _stored_token() -> str:
    secret = await asyncio.to_thread(
        lambda: SecretVault(config_dir()).get(CODER_SESSION_TOKEN_SECRET)
    )
    return secret.reveal() if secret is not None else ""


def _string_field(body: dict[str, Any], name: str) -> str:
    value = body.get(name, "")
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value.strip()


def _validate_coordinates(
    *, url: str, template: str, preset: str, remote_cwd: str, workspace_prefix: str
) -> tuple[str, str, str, str, str]:
    safe_prefix = validate_coder_workspace(workspace_prefix)
    if len(safe_prefix) > CODER_WORKSPACE_PREFIX_MAX_CHARS:
        raise ValueError(
            "workspace_prefix must be at most " f"{CODER_WORKSPACE_PREFIX_MAX_CHARS} characters"
        )
    return (
        validate_coder_url(url),
        validate_coder_workspace(template),
        validate_coder_workspace(preset) if preset else "",
        validate_coder_remote_cwd(remote_cwd),
        safe_prefix,
    )


def _int_field(body: dict[str, Any], name: str, *, minimum: int, maximum: int) -> int:
    value = body.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")
    return value


def _profiles_field(body: dict[str, Any]) -> dict[str, dict[str, str]]:
    value = body.get("profiles", {})
    if not isinstance(value, dict) or len(value) > CODER_MAX_PROFILES:
        raise ValueError(f"profiles must be an object with at most {CODER_MAX_PROFILES} entries")
    profiles: dict[str, dict[str, str]] = {}
    for name, raw in value.items():
        if not isinstance(name, str) or not isinstance(raw, dict):
            raise ValueError("each Coder profile must be a named object")
        safe_name = validate_coder_workspace(name)
        template = raw.get("template", "")
        preset = raw.get("preset", "")
        if not isinstance(template, str) or not isinstance(preset, str):
            raise ValueError("Coder profile template and preset must be strings")
        profiles[safe_name] = {
            "template": validate_coder_workspace(template.strip()),
            "preset": validate_coder_workspace(preset.strip()) if preset.strip() else "",
        }
    return profiles


async def probe_coder_managed_config(**kwargs: Any) -> dict[str, str]:
    """Verify auth and template visibility without creating billable compute."""
    env = os.environ
    requested_bin = env.get("KIROCREW_CODER_BIN", "coder")
    coder_bin = shutil.which(requested_bin, path=env.get("PATH"))
    if not coder_bin:
        raise SessionHostError(f"Coder CLI is not executable: {requested_bin}")
    client = CoderClient(
        coder_bin,
        kwargs["url"],
        kwargs["token"],
        Path(kwargs["local_cwd"]),
    )
    try:
        return await client.probe(template=kwargs["template"], preset=kwargs["preset"])
    except CoderClientError as exc:
        raise SessionHostError(str(exc)) from exc


async def api_coder_config(request: web.Request) -> web.Response:
    """GET/PUT Coder defaults; the session token is presence-only on reads."""
    denied = await _owner_only(request, "coder_config")
    if denied is not None:
        return denied

    if request.method == "GET":
        try:
            cfg, token = await asyncio.gather(
                asyncio.to_thread(KiroCrewConfig.load),
                _stored_token(),
            )
        except Exception:
            return _error(
                "Could not read Coder settings",
                "coder_config_unavailable",
                status=500,
            )
        coder = cfg.session.coder
        legacy = coder.enabled is None and bool(os.environ.get("KIROCREW_CODER_WORKSPACE", ""))
        if legacy:
            url = os.environ.get("CODER_URL", "")
            workspace = os.environ.get("KIROCREW_CODER_WORKSPACE", "")
            remote_cwd = os.environ.get("KIROCREW_CODER_REMOTE_CWD", CODER_DEFAULT_REMOTE_CWD)
            managed = {
                "template": "",
                "preset": "",
                "profiles": {},
                "runtime_warm_minutes": CODER_DEFAULT_RUNTIME_WARM_MINUTES,
                "stop_after_minutes": CODER_DEFAULT_STOP_AFTER_MINUTES,
                "delete_after_days": CODER_DEFAULT_DELETE_AFTER_DAYS,
                "max_running": CODER_DEFAULT_MAX_RUNNING,
                "workspace_prefix": CODER_DEFAULT_WORKSPACE_PREFIX,
                "static_workspace": workspace,
            }
        else:
            url = coder.url
            remote_cwd = coder.remote_cwd
            managed = {
                "template": coder.template,
                "preset": coder.preset,
                "profiles": {
                    name: {"template": profile.template, "preset": profile.preset}
                    for name, profile in coder.profiles.items()
                },
                "runtime_warm_minutes": coder.runtime_warm_minutes,
                "stop_after_minutes": coder.stop_after_minutes,
                "delete_after_days": coder.delete_after_days,
                "max_running": coder.max_running,
                "workspace_prefix": coder.workspace_prefix,
            }
        return web.json_response(
            {
                "enabled": legacy or coder.enabled is True,
                "url": url,
                "remote_cwd": remote_cwd,
                "token_configured": bool(token or os.environ.get("CODER_SESSION_TOKEN", "")),
                "legacy_environment": legacy,
                "limits": {
                    "max_profiles": CODER_MAX_PROFILES,
                    "workspace_prefix_max_chars": CODER_WORKSPACE_PREFIX_MAX_CHARS,
                },
                **managed,
            }
        )

    try:
        body = await request.json()
    except ValueError:
        return _error("Invalid JSON body", "invalid_json")
    if not isinstance(body, dict):
        return _error("Request body must be a JSON object", "invalid_body")
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        return _error("enabled must be a boolean", "coder_enabled_invalid")
    try:
        url = _string_field(body, "url")
        template = _string_field(body, "template")
        preset = _string_field(body, "preset")
        remote_cwd = _string_field(body, "remote_cwd")
        workspace_prefix = _string_field(body, "workspace_prefix")
        runtime_warm_minutes = _int_field(body, "runtime_warm_minutes", minimum=0, maximum=1440)
        stop_after_minutes = _int_field(body, "stop_after_minutes", minimum=1, maximum=10080)
        delete_after_days = _int_field(body, "delete_after_days", minimum=1, maximum=3650)
        max_running = _int_field(body, "max_running", minimum=1, maximum=100)
        token = _string_field(body, "token")
    except ValueError as exc:
        return _error(str(exc), "coder_field_invalid")
    try:
        profiles = _profiles_field(body)
    except ValueError as exc:
        return _error(str(exc), "coder_profiles_invalid")
    if len(token.encode("utf-8")) > _CODER_TOKEN_MAX_BYTES:
        return _error("token is too large", "coder_token_invalid")
    if enabled:
        try:
            url, template, preset, remote_cwd, workspace_prefix = _validate_coordinates(
                url=url,
                template=template,
                preset=preset,
                remote_cwd=remote_cwd,
                workspace_prefix=workspace_prefix,
            )
        except ValueError as exc:
            return _error(str(exc), "coder_config_invalid")

    try:
        stored_token = await _stored_token()
    except Exception:
        return _error("Could not read the Coder token", "coder_vault_unavailable", status=500)
    effective_token = token or stored_token or os.environ.get("CODER_SESSION_TOKEN", "")
    if enabled and not effective_token:
        return _error("A Coder session token is required", "coder_token_required")

    # Saving an enabled legacy setup with a blank token field migrates the
    # gateway-only environment bearer into the encrypted vault. It is never
    # serialized into config.json or returned by this endpoint.
    if effective_token and (token or (enabled and not stored_token)):
        try:
            await SecretVault(config_dir()).set(CODER_SESSION_TOKEN_SECRET, effective_token)
        except Exception:
            return _error("Could not store the Coder token", "coder_vault_unavailable", status=500)

    cfg_path = config_path()
    async with _get_config_lock():

        def _mutate(data: dict) -> dict:
            session = data.setdefault("session", {})
            if not isinstance(session, dict):
                raise ValueError("config section 'session' is not an object")
            session["coder"] = {
                "enabled": enabled,
                "url": url,
                "template": template,
                "preset": preset,
                "profiles": profiles,
                "remote_cwd": remote_cwd,
                "runtime_warm_minutes": runtime_warm_minutes,
                "stop_after_minutes": stop_after_minutes,
                "delete_after_days": delete_after_days,
                "max_running": max_running,
                "workspace_prefix": workspace_prefix,
            }
            return data

        try:
            await asyncio.to_thread(update_config_locked, cfg_path, mutate=_mutate)
        except ConfigReadError:
            return _error("Could not read the config file", "coder_config_read_failed", status=500)
        except (OSError, ValueError):
            return _error("Could not save Coder settings", "coder_config_write_failed", status=500)

    state = request.app["state"]
    try:
        await state.sessions.refresh_defaults()
    except Exception:
        return _error(
            "Coder settings were saved but new defaults could not be refreshed",
            "coder_defaults_refresh_failed",
            status=500,
        )
    return web.json_response(
        {
            "ok": True,
            "token_configured": bool(effective_token),
            "active_sessions_unchanged": True,
        }
    )


async def api_coder_test(request: web.Request) -> web.Response:
    """Test candidate Coder coordinates without persisting or echoing a token."""
    denied = await _owner_only(request, "coder_test")
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except ValueError:
        return _error("Invalid JSON body", "invalid_json")
    if not isinstance(body, dict):
        return _error("Request body must be a JSON object", "invalid_body")
    try:
        url = _string_field(body, "url")
        template = _string_field(body, "template")
        preset = _string_field(body, "preset")
        remote_cwd = _string_field(body, "remote_cwd")
        candidate_token = _string_field(body, "token")
        url, template, preset, remote_cwd, _workspace_prefix = _validate_coordinates(
            url=url,
            template=template,
            preset=preset,
            remote_cwd=remote_cwd,
            workspace_prefix=CODER_DEFAULT_WORKSPACE_PREFIX,
        )
    except ValueError as exc:
        return _error(str(exc), "coder_config_invalid")
    if len(candidate_token.encode("utf-8")) > _CODER_TOKEN_MAX_BYTES:
        return _error("token is too large", "coder_token_invalid")
    try:
        token = (
            candidate_token or await _stored_token() or os.environ.get("CODER_SESSION_TOKEN", "")
        )
    except Exception:
        return _error("Could not read the Coder token", "coder_vault_unavailable", status=500)
    if not token:
        return _error("A Coder session token is required", "coder_token_required")
    try:
        result = await probe_coder_managed_config(
            url=url,
            token=token,
            template=template,
            preset=preset,
            remote_cwd=remote_cwd,
            local_cwd=Path(config_dir()),
        )
    except (OSError, SessionHostError):
        return _error(
            "The gateway could not connect to the Coder workspace",
            "coder_connection_failed",
            status=502,
        )
    return web.json_response({"ok": True, **result})


def setup_coder_routes(app: web.Application) -> None:
    app.router.add_get("/api/coder/config", api_coder_config)
    app.router.add_put("/api/coder/config", api_coder_config)
    app.router.add_post("/api/coder/test", api_coder_test)
