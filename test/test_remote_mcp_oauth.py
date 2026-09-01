"""Encrypted OAuth state for gateway-owned remote MCP transports."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from urllib.parse import parse_qs, urlsplit

import pytest
from aiohttp import web
from mcp.client.auth import OAuthFlowError
from mcp.shared.auth import AuthorizationCodeResult, OAuthClientInformationFull, OAuthToken

from kiro_crew.mcp_gateway.oauth import (
    OAuthCredentialIdentity,
    RemoteMcpOAuthBroker,
    RemoteMcpOAuthManager,
    VaultOAuthTokenStorage,
    _validated_revocation_endpoint,
)
from kiro_crew.mcp_gateway.remote_http import GatewayMcpHttpAdapter
from kiro_crew.mcp_gateway.remote_proxy import RemoteHttpMcpTarget, RemoteMcpProxy
from kiro_crew.secrets.vault import SecretVault


def _target(
    *,
    url: str = "https://mcp.example.test/mcp",
    client_id: str = "",
) -> RemoteHttpMcpTarget:
    return RemoteHttpMcpTarget(
        server_name="example",
        url=url,
        headers={},
        client_id=client_id,
    )


@pytest.mark.asyncio
async def test_vault_oauth_storage_round_trips_sdk_models_without_named_secrets(
    tmp_path,
) -> None:
    identity = OAuthCredentialIdentity.for_target(_target())
    storage = VaultOAuthTokenStorage(SecretVault(tmp_path), identity)
    tokens = OAuthToken(
        access_token="access-secret-canary",
        refresh_token="refresh-secret-canary",
        expires_in=3600,
        scope="read write",
    )
    client = OAuthClientInformationFull(
        client_id="dynamic-client",
        client_secret="client-secret-canary",
        redirect_uris=["https://crew.example/api/mcp/oauth/callback"],
        token_endpoint_auth_method="client_secret_post",
    )

    await storage.set_tokens(tokens)
    await storage.set_client_info(client)

    assert await storage.get_tokens() == tokens
    assert await storage.get_client_info() == client
    names = storage.vault_names
    serialized_names = " ".join(names)
    assert "mcp.example.test" not in serialized_names
    assert "dynamic-client" not in serialized_names
    assert "access-secret-canary" not in repr(storage)
    assert "client-secret-canary" not in repr(storage)

    await storage.delete()

    assert await storage.get_tokens() is None
    assert await storage.get_client_info() is None


def test_oauth_identity_separates_resource_and_client_without_leaky_repr() -> None:
    default = OAuthCredentialIdentity.for_target(_target())
    other_resource = OAuthCredentialIdentity.for_target(
        _target(url="https://other.example.test/mcp")
    )
    explicit_client = OAuthCredentialIdentity.for_target(_target(client_id="client-two"))

    assert len({default.digest, other_resource.digest, explicit_client.digest}) == 3
    assert "mcp.example.test" not in repr(default)
    assert "client-two" not in repr(explicit_client)


@pytest.mark.parametrize(
    ("endpoint", "issuer", "token_endpoint", "expected"),
    [
        (
            "https://auth.example/revoke",
            "https://auth.example",
            "https://auth.example/token",
            "https://auth.example/revoke",
        ),
        (
            "http://127.0.0.1:8123/revoke",
            "http://127.0.0.1:8123",
            "http://127.0.0.1:8123/token",
            "http://127.0.0.1:8123/revoke",
        ),
        (
            "https://attacker.example/collect",
            "https://auth.example",
            "https://auth.example/token",
            "",
        ),
        (
            "http://169.254.169.254/latest",
            "http://169.254.169.254",
            "http://169.254.169.254/token",
            "",
        ),
        (
            "https://user:password@auth.example/revoke",
            "https://auth.example",
            "https://auth.example/token",
            "",
        ),
    ],
)
def test_revocation_endpoint_must_match_the_oauth_server_origin(
    endpoint: str,
    issuer: str,
    token_endpoint: str,
    expected: str,
) -> None:
    assert _validated_revocation_endpoint(endpoint, issuer, token_endpoint) == expected


@pytest.mark.asyncio
async def test_oauth_attempt_is_one_shot_and_returns_sdk_callback_result() -> None:
    broker = RemoteMcpOAuthBroker()
    attempt = broker.begin(
        "dashboard:one",
        "linear",
        "https://auth.example/authorize?state=state-one&code_challenge=challenge",
    )

    assert broker.complete(
        {"code": "code-one", "state": "state-one", "iss": "https://auth.example"}
    )
    assert await broker.wait(attempt) == AuthorizationCodeResult(
        code="code-one",
        state="state-one",
        iss="https://auth.example",
    )
    assert not broker.complete({"code": "replay", "state": "state-one"})
    assert "code-one" not in repr(attempt)


@pytest.mark.asyncio
async def test_oauth_attempt_rejects_bad_state_and_bounds_provider_denial() -> None:
    broker = RemoteMcpOAuthBroker()
    denial = broker.begin(
        "dashboard:one",
        "linear",
        "https://auth.example/authorize?state=state-denied",
    )

    assert not broker.complete({"code": "code", "state": "unknown"})
    assert not broker.complete({"code": "code", "state": ["one", "two"]})
    assert not broker.complete({"code": "x" * 9_000, "state": "state-denied"})
    assert not broker.complete({"code": "bad\ncode", "state": "state-denied"})
    assert broker.complete(
        {
            "error": "access_denied",
            "error_description": "provider-secret-canary",
            "state": "state-denied",
        }
    )

    with pytest.raises(OAuthFlowError) as caught:
        await broker.wait(denial)

    assert str(caught.value) == "remote_mcp_oauth_failed"
    assert "provider-secret-canary" not in repr(caught.value)


@pytest.mark.asyncio
async def test_oauth_callback_with_matching_state_but_no_code_fails_attempt() -> None:
    broker = RemoteMcpOAuthBroker()
    attempt = broker.begin(
        "dashboard:one",
        "linear",
        "https://auth.example/authorize?state=state-without-code",
    )

    assert not broker.complete({"state": "state-without-code"})
    assert attempt._future.done()
    with pytest.raises(OAuthFlowError):
        await broker.wait(attempt)


@pytest.mark.asyncio
async def test_oauth_broker_close_cancels_live_attempts() -> None:
    broker = RemoteMcpOAuthBroker()
    attempt = broker.begin(
        "dashboard:one",
        "linear",
        "https://auth.example/authorize?state=state-close",
    )

    await broker.close()

    with pytest.raises(asyncio.CancelledError):
        await broker.wait(attempt)
    assert not broker.complete({"code": "late", "state": "state-close"})


@pytest.mark.asyncio
async def test_cancelled_oauth_wait_retires_attempt_immediately() -> None:
    broker = RemoteMcpOAuthBroker()
    attempt = broker.begin(
        "dashboard:one",
        "linear",
        "https://auth.example/authorize?state=state-cancelled-wait",
    )
    waiter = asyncio.create_task(broker.wait(attempt))
    await asyncio.sleep(0)

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert not broker.complete({"code": "late", "state": "state-cancelled-wait"})
    assert attempt._future.cancelled()


@pytest.mark.asyncio
async def test_official_sdk_oauth_flow_stays_gateway_owned_and_reuses_grant(
    tmp_path,
) -> None:
    access_token = "oauth-access-secret-canary"
    authorization_urls: list[str] = []
    oauth_outcomes: list[str] = []
    token_forms: list[dict[str, str]] = []
    revocation_forms: list[dict[str, str]] = []
    registrations = 0
    app = web.Application()
    origin = ""

    async def handler(request: web.Request) -> web.Response:
        nonlocal registrations
        if request.path == "/mcp":
            if request.headers.get("Authorization") != f"Bearer {access_token}":
                return web.Response(
                    status=401,
                    headers={
                        "WWW-Authenticate": (
                            f'Bearer resource_metadata="{origin}/.well-known/'
                            'oauth-protected-resource", scope="read"'
                        )
                    },
                )
            payload = await request.json()
            return web.json_response(
                {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "serverInfo": {"name": "oauth-test", "version": "1"},
                    },
                }
            )
        if request.path == "/.well-known/oauth-protected-resource":
            return web.json_response(
                {
                    "resource": f"{origin}/mcp",
                    "authorization_servers": [origin],
                    "scopes_supported": ["read"],
                }
            )
        if request.path.startswith("/.well-known/oauth-authorization-server"):
            return web.json_response(
                {
                    "issuer": origin,
                    "authorization_endpoint": f"{origin}/authorize",
                    "token_endpoint": f"{origin}/token",
                    "registration_endpoint": f"{origin}/register",
                    "revocation_endpoint": f"{origin}/revoke",
                    "response_types_supported": ["code"],
                    "grant_types_supported": ["authorization_code", "refresh_token"],
                    "token_endpoint_auth_methods_supported": ["none"],
                    "code_challenge_methods_supported": ["S256"],
                    "scopes_supported": ["read"],
                }
            )
        if request.path == "/register":
            registrations += 1
            payload = await request.json()
            return web.json_response(
                {
                    **payload,
                    "client_id": "dynamic-client",
                    "token_endpoint_auth_method": "none",
                },
                status=201,
            )
        if request.path == "/token":
            form = dict(await request.post())
            token_forms.append(form)
            expires_in = 0 if form.get("grant_type") == "authorization_code" else 3600
            return web.json_response(
                {
                    "access_token": access_token,
                    "token_type": "Bearer",
                    "refresh_token": "oauth-refresh-secret-canary",
                    "expires_in": expires_in,
                    "scope": "read",
                }
            )
        if request.path == "/revoke":
            revocation_forms.append(dict(await request.post()))
            return web.Response(status=500)
        return web.Response(status=404)

    app.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    sockets = site._server.sockets
    assert sockets
    origin = f"http://127.0.0.1:{sockets[0].getsockname()[1]}"
    manager: RemoteMcpOAuthManager

    async def authorize(event) -> None:
        oauth_outcomes.append(event.outcome)
        if event.outcome != "required":
            return
        authorization_urls.append(event.authorization_url)
        state = parse_qs(urlsplit(event.authorization_url).query)["state"][0]
        assert manager.broker.complete({"code": "authorization-code", "state": state})

    manager = RemoteMcpOAuthManager(
        SecretVault(tmp_path),
        "https://crew.example",
        event_sink=authorize,
    )
    adapter = GatewayMcpHttpAdapter(manager)
    target = RemoteHttpMcpTarget(
        server_name="oauth-test",
        url=f"{origin}/mcp",
        headers={},
        scopes=("read",),
    )
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    }
    try:
        for session_key in ("dashboard:one", "dashboard:two"):
            proxy = RemoteMcpProxy(http_adapter=adapter)
            await proxy.start()
            grant = proxy.mint(session_key, target)
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy.local_port)
            writer.write(json.dumps({"version": 1, "token": grant.token}).encode() + b"\n")
            writer.write(json.dumps(request).encode() + b"\n")
            await writer.drain()
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=5)
                assert line
                response = json.loads(line)
                assert response["result"]["serverInfo"]["name"] == "oauth-test"
            finally:
                writer.close()
                await writer.wait_closed()
                await proxy.close()
        await manager.disconnect(target)
    finally:
        await manager.close()
        await runner.cleanup()

    assert len(authorization_urls) == 1
    assert oauth_outcomes == ["required", "completed"]
    assert registrations == 1
    assert [form["grant_type"] for form in token_forms] == [
        "authorization_code",
        "refresh_token",
    ]
    assert {form["token_type_hint"] for form in revocation_forms} == {
        "access_token",
        "refresh_token",
    }
    storage = VaultOAuthTokenStorage(
        SecretVault(tmp_path),
        OAuthCredentialIdentity.for_target(target),
    )
    assert await storage.get_tokens() is None
    assert await storage.get_client_info() is None
    verifier = token_forms[0]["code_verifier"]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode()
    challenge = challenge.rstrip("=")
    assert parse_qs(urlsplit(authorization_urls[0]).query)["code_challenge"] == [challenge]


@pytest.mark.asyncio
async def test_oauth_manager_shares_one_provider_during_concurrent_startup(tmp_path) -> None:
    manager = RemoteMcpOAuthManager(SecretVault(tmp_path), "https://crew.example")
    target = _target(client_id="public-client")
    try:
        first, second = await asyncio.gather(
            manager.provider_for(target),
            manager.provider_for(target),
        )
    finally:
        await manager.close()

    assert first is second
