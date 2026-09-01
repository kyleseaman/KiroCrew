"""Gateway-owned OAuth credentials and browser callback rendezvous for MCP."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

import httpx2
from mcp.client.auth import OAuthClientProvider, OAuthFlowError
from mcp.shared.auth import (
    AuthorizationCodeResult,
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthToken,
)
from mcp.shared.auth_utils import resource_url_from_server_url
from pydantic import AnyUrl

from kiro_crew.mcp_gateway.remote_proxy import RemoteHttpMcpTarget
from kiro_crew.secrets.vault import SecretVault

_VAULT_PREFIX = "remote-mcp-oauth-v1"
_AUTHORIZATION_URL_MAX_BYTES = 8 * 1024
_CALLBACK_QUERY_MAX_BYTES = 8 * 1024
_ATTEMPT_TTL_SECONDS = 5 * 60.0
_MAX_LIVE_ATTEMPTS = 64
_CALLBACK_KEYS = frozenset(("code", "state", "iss", "error", "error_description"))
_OAUTH_FAILURE_CODE = "remote_mcp_oauth_failed"
_REVOCATION_TIMEOUT_SECONDS = 10.0
_LOOPBACK_HOSTS = frozenset(("localhost", "127.0.0.1", "::1"))

logger = logging.getLogger(__name__)


def _contains_control(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _url_origin(value: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if (
        scheme not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or _contains_control(value)
    ):
        return None
    if scheme == "http" and host not in _LOOPBACK_HOSTS:
        return None
    return scheme, host, port or (443 if scheme == "https" else 80)


def _validated_revocation_endpoint(
    endpoint: str,
    issuer: str,
    token_endpoint: str,
) -> str:
    """Accept revocation only on an already-authorized OAuth server origin."""
    endpoint_origin = _url_origin(endpoint)
    if endpoint_origin is None:
        return ""
    allowed_origins = {
        origin
        for candidate in (issuer, token_endpoint)
        if candidate and (origin := _url_origin(candidate)) is not None
    }
    return endpoint if endpoint_origin in allowed_origins else ""


@dataclass(frozen=True, repr=False)
class OAuthCredentialIdentity:
    """Opaque stable identity for one resource and OAuth client selection."""

    digest: str
    canonical_resource_url: str = field(repr=False, compare=False)
    client_identity: str = field(repr=False, compare=False)

    @classmethod
    def for_target(cls, target: RemoteHttpMcpTarget) -> OAuthCredentialIdentity:
        resource = resource_url_from_server_url(target.url)
        client_identity = target.client_id or "dynamic-registration"
        digest = hashlib.sha256((resource + "\0" + client_identity).encode("utf-8")).hexdigest()
        return cls(
            digest=digest,
            canonical_resource_url=resource,
            client_identity=client_identity,
        )

    def __repr__(self) -> str:
        return f"OAuthCredentialIdentity(digest={self.digest[:12]!r})"


class VaultOAuthTokenStorage:
    """MCP SDK token storage backed by the agent-inaccessible encrypted vault."""

    def __init__(self, vault: SecretVault, identity: OAuthCredentialIdentity) -> None:
        self._vault = vault
        self._identity = identity
        root = f"{_VAULT_PREFIX}-{identity.digest}"
        self._tokens_name = f"{root}-tokens"
        self._client_name = f"{root}-client"

    @property
    def vault_names(self) -> tuple[str, str]:
        return (self._tokens_name, self._client_name)

    async def get_tokens(self) -> OAuthToken | None:
        raw = await asyncio.to_thread(self._vault.get, self._tokens_name)
        if raw is None:
            return None
        try:
            return OAuthToken.model_validate_json(raw.reveal())
        except Exception:
            return None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        await self._vault.set(
            self._tokens_name,
            tokens.model_dump_json(exclude_none=True),
        )

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        raw = await asyncio.to_thread(self._vault.get, self._client_name)
        if raw is None:
            return None
        try:
            return OAuthClientInformationFull.model_validate_json(raw.reveal())
        except Exception:
            return None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        await self._vault.set(
            self._client_name,
            client_info.model_dump_json(exclude_none=True),
        )

    async def delete(self) -> None:
        await self._vault.delete(self._tokens_name)
        await self._vault.delete(self._client_name)

    def __repr__(self) -> str:
        return f"VaultOAuthTokenStorage(identity={self._identity!r})"


@dataclass(frozen=True)
class RemoteMcpOAuthEvent:
    session_key: str
    server_name: str
    state: str
    authorization_url: str = field(default="", repr=False)
    outcome: str = "required"
    code: str = "remote_mcp_oauth_required"


@dataclass(frozen=True, repr=False)
class OAuthAttempt:
    session_key: str
    server_name: str
    state: str
    authorization_url: str = field(repr=False)
    expires_at: float = field(repr=False)
    _future: asyncio.Future[AuthorizationCodeResult] = field(repr=False, compare=False)

    def __repr__(self) -> str:
        return (
            "OAuthAttempt(" f"session_key={self.session_key!r}, server_name={self.server_name!r})"
        )


class RemoteMcpOAuthBroker:
    """One-shot, bounded rendezvous between SDK OAuth and dashboard callbacks."""

    def __init__(
        self,
        *,
        clock=time.monotonic,
        event_sink: Callable[[RemoteMcpOAuthEvent], Awaitable[None]] | None = None,
    ) -> None:
        self._clock = clock
        self._event_sink = event_sink
        self._attempts: dict[str, OAuthAttempt] = {}
        self._closed = False

    def begin(
        self,
        session_key: str,
        server_name: str,
        authorization_url: str,
    ) -> OAuthAttempt:
        if self._closed:
            raise OAuthFlowError(_OAUTH_FAILURE_CODE)
        if (
            not authorization_url
            or len(authorization_url.encode("utf-8")) > _AUTHORIZATION_URL_MAX_BYTES
            or _contains_control(authorization_url)
        ):
            raise OAuthFlowError(_OAUTH_FAILURE_CODE)
        try:
            query = parse_qs(
                urlsplit(authorization_url).query,
                keep_blank_values=True,
                strict_parsing=False,
            )
        except ValueError:
            raise OAuthFlowError(_OAUTH_FAILURE_CODE) from None
        states = query.get("state", [])
        if len(states) != 1 or not states[0] or _contains_control(states[0]):
            raise OAuthFlowError(_OAUTH_FAILURE_CODE)
        state = states[0]
        self._evict_expired()
        if state in self._attempts or len(self._attempts) >= _MAX_LIVE_ATTEMPTS:
            raise OAuthFlowError(_OAUTH_FAILURE_CODE)
        future: asyncio.Future[AuthorizationCodeResult] = asyncio.get_running_loop().create_future()
        attempt = OAuthAttempt(
            session_key=session_key,
            server_name=server_name,
            state=state,
            authorization_url=authorization_url,
            expires_at=self._clock() + _ATTEMPT_TTL_SECONDS,
            _future=future,
        )
        self._attempts[state] = attempt
        return attempt

    async def announce(self, attempt: OAuthAttempt) -> None:
        if self._event_sink is None:
            return
        await self._event_sink(
            RemoteMcpOAuthEvent(
                session_key=attempt.session_key,
                server_name=attempt.server_name,
                state=attempt.state,
                authorization_url=attempt.authorization_url,
            )
        )

    async def announce_terminal(self, attempt: OAuthAttempt, *, completed: bool) -> None:
        if self._event_sink is None:
            return
        event = RemoteMcpOAuthEvent(
            session_key=attempt.session_key,
            server_name=attempt.server_name,
            state=attempt.state,
            outcome="completed" if completed else "failed",
            code="" if completed else _OAUTH_FAILURE_CODE,
        )
        try:
            await self._event_sink(event)
        except Exception:
            logger.warning("Cannot publish remote MCP OAuth terminal event", exc_info=True)

    async def wait(self, attempt: OAuthAttempt) -> AuthorizationCodeResult:
        remaining = max(0.0, attempt.expires_at - self._clock())
        if remaining == 0.0 and not attempt._future.done():
            self._attempts.pop(attempt.state, None)
            attempt._future.cancel()
            raise OAuthFlowError(_OAUTH_FAILURE_CODE)
        try:
            return await asyncio.wait_for(asyncio.shield(attempt._future), timeout=remaining)
        except asyncio.TimeoutError:
            self._attempts.pop(attempt.state, None)
            attempt._future.cancel()
            raise OAuthFlowError(_OAUTH_FAILURE_CODE) from None
        except asyncio.CancelledError:
            current = self._attempts.pop(attempt.state, None)
            if current is attempt and not attempt._future.done():
                attempt._future.cancel()
            raise

    def complete(self, query: Mapping[str, object]) -> bool:
        values = self._validated_callback_values(query)
        if values is None:
            return False
        state = values.get("state", "")
        attempt = self._attempts.pop(state, None)
        if attempt is None or attempt._future.done():
            return False
        if attempt.expires_at <= self._clock():
            attempt._future.set_exception(OAuthFlowError(_OAUTH_FAILURE_CODE))
            return False
        if values.get("error"):
            attempt._future.set_exception(OAuthFlowError(_OAUTH_FAILURE_CODE))
            return True
        code = values.get("code", "")
        if not code:
            attempt._future.set_exception(OAuthFlowError(_OAUTH_FAILURE_CODE))
            return False
        attempt._future.set_result(
            AuthorizationCodeResult(
                code=code,
                state=state,
                iss=values.get("iss") or None,
            )
        )
        return True

    def fail(self, attempt: OAuthAttempt, code: str = _OAUTH_FAILURE_CODE) -> None:
        current = self._attempts.pop(attempt.state, None)
        if current is attempt and not attempt._future.done():
            attempt._future.set_exception(OAuthFlowError(code))

    async def close(self) -> None:
        self._closed = True
        attempts = tuple(self._attempts.values())
        self._attempts.clear()
        for attempt in attempts:
            if not attempt._future.done():
                attempt._future.cancel()

    def _evict_expired(self) -> None:
        now = self._clock()
        expired = [state for state, attempt in self._attempts.items() if attempt.expires_at <= now]
        for state in expired:
            attempt = self._attempts.pop(state)
            if not attempt._future.done():
                attempt._future.set_exception(OAuthFlowError(_OAUTH_FAILURE_CODE))

    @staticmethod
    def _validated_callback_values(
        query: Mapping[str, object],
    ) -> dict[str, str] | None:
        if not set(query).issubset(_CALLBACK_KEYS):
            return None
        values: dict[str, str] = {}
        total_bytes = 0
        for key, raw in query.items():
            if not isinstance(raw, str) or _contains_control(raw):
                return None
            total_bytes += len(key.encode("utf-8")) + len(raw.encode("utf-8"))
            if total_bytes > _CALLBACK_QUERY_MAX_BYTES:
                return None
            values[key] = raw
        if not values.get("state"):
            return None
        return values


@dataclass
class _OAuthInteraction:
    session_key: str
    server_name: str
    attempt: OAuthAttempt | None = None


class RemoteMcpOAuthManager:
    """Share SDK OAuth providers and encrypted grants across remote sessions."""

    callback_path = "/api/mcp/oauth/callback"

    def __init__(
        self,
        vault: SecretVault,
        callback_origin: str,
        *,
        event_sink: Callable[[RemoteMcpOAuthEvent], Awaitable[None]] | None = None,
    ) -> None:
        self._vault = vault
        self._callback_origin = callback_origin.rstrip("/")
        self.broker = RemoteMcpOAuthBroker(event_sink=event_sink)
        self._providers: dict[str, OAuthClientProvider] = {}
        self._storages: dict[str, VaultOAuthTokenStorage] = {}
        self._provider_lock = asyncio.Lock()
        self._interaction: ContextVar[_OAuthInteraction | None] = ContextVar(
            "remote_mcp_oauth_interaction",
            default=None,
        )

    @property
    def callback_uri(self) -> str:
        return self._callback_origin + self.callback_path

    async def _redirect_handler(self, authorization_url: str) -> None:
        interaction = self._interaction.get()
        if interaction is None:
            raise OAuthFlowError(_OAUTH_FAILURE_CODE)
        attempt = self.broker.begin(
            interaction.session_key,
            interaction.server_name,
            authorization_url,
        )
        interaction.attempt = attempt
        await self.broker.announce(attempt)

    async def _callback_handler(self) -> AuthorizationCodeResult:
        interaction = self._interaction.get()
        if interaction is None or interaction.attempt is None:
            raise OAuthFlowError(_OAUTH_FAILURE_CODE)
        return await self.broker.wait(interaction.attempt)

    async def provider_for(self, target: RemoteHttpMcpTarget) -> OAuthClientProvider | None:
        if any(name.lower() == "authorization" for name in target.headers):
            return None
        identity = OAuthCredentialIdentity.for_target(target)
        provider = self._providers.get(identity.digest)
        if provider is not None:
            return provider
        async with self._provider_lock:
            provider = self._providers.get(identity.digest)
            if provider is not None:
                return provider
            storage = VaultOAuthTokenStorage(self._vault, identity)
            if target.client_id and await storage.get_client_info() is None:
                await storage.set_client_info(
                    OAuthClientInformationFull(
                        client_id=target.client_id,
                        redirect_uris=[AnyUrl(self.callback_uri)],
                        token_endpoint_auth_method="none",
                        scope=" ".join(target.scopes) or None,
                    )
                )
            metadata = OAuthClientMetadata(
                redirect_uris=[AnyUrl(self.callback_uri)],
                scope=" ".join(target.scopes) or None,
                client_name="Kiro Crew",
                application_type="web",
            )
            provider = OAuthClientProvider(
                target.url,
                metadata,
                storage,
                redirect_handler=self._redirect_handler,
                callback_handler=self._callback_handler,
            )
            self._storages[identity.digest] = storage
            self._providers[identity.digest] = provider
            return provider

    @asynccontextmanager
    async def interaction(self, target: RemoteHttpMcpTarget, session_key: str):
        provider = await self.provider_for(target)
        interaction = _OAuthInteraction(
            session_key=session_key,
            server_name=target.server_name,
        )
        token = self._interaction.set(interaction)
        try:
            yield provider
        finally:
            self._interaction.reset(token)

    async def transport_succeeded(self) -> None:
        interaction = self._interaction.get()
        if interaction is None or interaction.attempt is None:
            return
        attempt = interaction.attempt
        interaction.attempt = None
        await self.broker.announce_terminal(attempt, completed=True)

    async def transport_failed(self) -> None:
        interaction = self._interaction.get()
        if interaction is None or interaction.attempt is None:
            return
        attempt = interaction.attempt
        interaction.attempt = None
        self.broker.fail(attempt)
        await self.broker.announce_terminal(attempt, completed=False)

    async def disconnect(self, target: RemoteHttpMcpTarget) -> None:
        identity = OAuthCredentialIdentity.for_target(target)
        async with self._provider_lock:
            provider = self._providers.pop(identity.digest, None)
            storage = self._storages.pop(identity.digest, None)
        if storage is None:
            storage = VaultOAuthTokenStorage(self._vault, identity)
        revocations: list[tuple[str, dict[str, str], dict[str, str]]] = []
        if provider is not None:
            async with provider.context.lock:
                tokens = provider.context.current_tokens or await storage.get_tokens()
                client_info = provider.context.client_info or await storage.get_client_info()
                provider.context.client_info = client_info
                metadata = provider.context.oauth_metadata
                endpoint = ""
                if metadata is not None and metadata.revocation_endpoint is not None:
                    endpoint = _validated_revocation_endpoint(
                        str(metadata.revocation_endpoint),
                        str(metadata.issuer) if metadata.issuer is not None else "",
                        (
                            str(metadata.token_endpoint)
                            if metadata.token_endpoint is not None
                            else ""
                        ),
                    )
                try:
                    if endpoint and tokens is not None:
                        for token_type, token_value in (
                            ("access_token", tokens.access_token),
                            ("refresh_token", tokens.refresh_token or ""),
                        ):
                            if not token_value:
                                continue
                            data, headers = provider.context.prepare_token_auth(
                                {
                                    "token": token_value,
                                    "token_type_hint": token_type,
                                }
                            )
                            revocations.append((endpoint, data, headers))
                finally:
                    await storage.delete()
                    provider.context.clear_tokens()
                    provider.context.client_info = None
        else:
            await storage.delete()

        timeout = httpx2.Timeout(_REVOCATION_TIMEOUT_SECONDS)
        async with httpx2.AsyncClient(
            timeout=timeout,
            verify=True,
            follow_redirects=False,
        ) as client:
            for endpoint, data, headers in revocations:
                try:
                    async with client.stream(
                        "POST",
                        endpoint,
                        data=data,
                        headers=headers,
                    ):
                        pass
                except Exception:
                    logger.warning("Remote MCP OAuth revocation failed", exc_info=True)

    async def close(self) -> None:
        await self.broker.close()
        async with self._provider_lock:
            self._providers.clear()
            self._storages.clear()


_RUNTIME_MANAGER: RemoteMcpOAuthManager | None = None


def configure_runtime_oauth_manager(manager: RemoteMcpOAuthManager | None) -> None:
    """Publish the dashboard-owned manager to session-host HTTP adapters."""
    global _RUNTIME_MANAGER
    _RUNTIME_MANAGER = manager


def runtime_oauth_manager() -> RemoteMcpOAuthManager | None:
    return _RUNTIME_MANAGER
