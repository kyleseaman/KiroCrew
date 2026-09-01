"""Trusted dashboard callback and banner routing for gateway MCP OAuth."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers.remote_mcp_oauth import (
    REMOTE_MCP_OAUTH_CALLBACK_PATH,
    api_remote_mcp_oauth_callback,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.dashboard.token_auth import _BYPASS_EXACT_METHODS
from kiro_crew.mcp_gateway.oauth import RemoteMcpOAuthBroker, RemoteMcpOAuthEvent


@pytest.mark.asyncio
async def test_remote_mcp_oauth_callback_completes_once_without_echoing_state() -> None:
    broker = RemoteMcpOAuthBroker()
    attempt = broker.begin(
        "dashboard:one",
        "linear",
        "https://auth.example/authorize?state=one-shot-state",
    )
    app = web.Application()
    app["remote_mcp_oauth_broker"] = broker
    app.router.add_get(REMOTE_MCP_OAUTH_CALLBACK_PATH, api_remote_mcp_oauth_callback)

    async with TestClient(TestServer(app)) as client:
        response = await client.get(
            REMOTE_MCP_OAUTH_CALLBACK_PATH,
            params={"code": "code-secret-canary", "state": "one-shot-state"},
        )
        replay = await client.get(
            REMOTE_MCP_OAUTH_CALLBACK_PATH,
            params={"code": "replay", "state": "one-shot-state"},
        )
        body = await replay.json()
        response_text = await response.text()

    assert response.status == 200
    assert response.content_type == "text/html"
    assert "Authorization complete" in response_text
    assert (
        response.headers["Content-Security-Policy"]
        == "default-src 'none'; base-uri 'none'; form-action 'none'"
    )
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert replay.status == 400
    assert body == {"code": "remote_mcp_oauth_failed"}
    assert "one-shot-state" not in str(body)
    assert (await broker.wait(attempt)).code == "code-secret-canary"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        {},
        {"state": "unknown", "code": "code"},
        {"state": ["one", "two"], "code": "code"},
        {"state": "bad\nstate", "code": "code"},
    ],
)
async def test_remote_mcp_oauth_callback_rejects_malformed_queries(query) -> None:
    app = web.Application()
    app["remote_mcp_oauth_broker"] = RemoteMcpOAuthBroker()
    app.router.add_get(REMOTE_MCP_OAUTH_CALLBACK_PATH, api_remote_mcp_oauth_callback)

    async with TestClient(TestServer(app)) as client:
        response = await client.get(REMOTE_MCP_OAUTH_CALLBACK_PATH, params=query)
        body = await response.json()

    assert response.status == 400
    assert body == {"code": "remote_mcp_oauth_failed"}


def test_remote_mcp_oauth_callback_bypasses_cookie_auth_for_get_only() -> None:
    assert _BYPASS_EXACT_METHODS[REMOTE_MCP_OAUTH_CALLBACK_PATH] == frozenset({"GET"})


@pytest.mark.asyncio
async def test_remote_mcp_oauth_prompt_from_subagent_routes_to_parent_channel_slot(
    monkeypatch,
) -> None:
    linked_slot = object()
    emitted: list[tuple[object, str, str]] = []
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_runner._emit_mcp_oauth_request",
        lambda _state, slot, name, url: emitted.append((slot, name, url)),
    )
    state = SimpleNamespace(
        subagents=SimpleNamespace(
            get=lambda child_id: (
                SimpleNamespace(parent_session_key="slack:thread-one")
                if child_id == "child-one"
                else None
            )
        ),
        get_linked_slot=lambda key: linked_slot if key == "slack:thread-one" else None,
        get_slot=lambda _key: None,
        push_slots_update=lambda: None,
    )
    event = RemoteMcpOAuthEvent(
        session_key="subagent:child-one",
        server_name="linear",
        state="opaque-state",
        authorization_url="https://auth.example/authorize?state=opaque-state",
    )

    await DashboardState.publish_remote_mcp_oauth_event(state, event)

    assert emitted == [(linked_slot, "linear", event.authorization_url)]


@pytest.mark.asyncio
async def test_remote_mcp_oauth_completion_updates_direct_dashboard_slot(monkeypatch) -> None:
    dashboard_slot = object()
    completed: list[tuple[object, str, bool, str]] = []
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_runner._mark_mcp_oauth_completed",
        lambda _state, slot, name, success, error: completed.append((slot, name, success, error)),
    )
    state = SimpleNamespace(
        subagents=None,
        get_linked_slot=lambda _key: None,
        get_slot=lambda key: dashboard_slot if key == "chat-one" else None,
        push_slots_update=lambda: None,
    )
    event = RemoteMcpOAuthEvent(
        session_key="dashboard:chat-one",
        server_name="linear",
        state="opaque-state",
        outcome="completed",
        code="",
    )

    await DashboardState.publish_remote_mcp_oauth_event(state, event)

    assert completed == [(dashboard_slot, "linear", True, "")]
