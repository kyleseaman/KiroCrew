"""Provider-neutral contracts for managed session execution environments."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence

_PROVIDER_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_CONFIGURATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_RESOURCE_NAME_MAX_CHARS = 255
_ENVIRONMENT_STATES = frozenset({"starting", "running", "stopped", "unavailable"})
_MEMORY_PRESSURES = frozenset({"normal", "elevated", "critical"})


class SessionEnvironmentUnavailable(RuntimeError):
    """A persisted or selected environment provider cannot serve a session."""


class SessionEnvironmentPrefetchUnavailable(SessionEnvironmentUnavailable):
    """A speculative reconnect cannot safely reuse an existing environment."""


@dataclass(frozen=True)
class SessionEnvironmentConfiguration:
    """One public, selectable configuration owned by an environment provider."""

    id: str
    name: str

    def __post_init__(self) -> None:
        if self.id and not _CONFIGURATION_ID_RE.fullmatch(self.id):
            raise ValueError("configuration must be one safe identifier")
        if not self.name or len(self.name) > _RESOURCE_NAME_MAX_CHARS:
            raise ValueError("configuration name must be a bounded non-empty string")

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "name": self.name}


@dataclass(frozen=True)
class SessionEnvironmentSelection:
    """Allocation intent selected before a managed resource exists."""

    provider: str
    configuration: str = ""

    def __post_init__(self) -> None:
        _validate_provider(self.provider)
        if self.configuration and not _CONFIGURATION_ID_RE.fullmatch(self.configuration):
            raise ValueError("configuration must be one safe identifier")


@dataclass(frozen=True)
class SessionEnvironmentBinding:
    """Dashboard-safe durable identity for one session environment."""

    provider: str
    configuration: str = ""
    resource_name: str = ""

    def __post_init__(self) -> None:
        _validate_provider(self.provider)
        if self.configuration and not _CONFIGURATION_ID_RE.fullmatch(self.configuration):
            raise ValueError("configuration must be one safe identifier")
        if len(self.resource_name) > _RESOURCE_NAME_MAX_CHARS or any(
            char in self.resource_name for char in "\r\n\x00"
        ):
            raise ValueError("resource_name must be one bounded display value")

    @property
    def selection(self) -> SessionEnvironmentSelection:
        return SessionEnvironmentSelection(self.provider, self.configuration)

    @classmethod
    def from_dict(cls, value: object) -> SessionEnvironmentBinding | None:
        if not isinstance(value, dict):
            return None
        provider = value.get("provider")
        configuration = value.get("configuration")
        resource_name = value.get("resource_name")
        if not isinstance(provider, str):
            return None
        if not isinstance(configuration, str):
            return None
        if not isinstance(resource_name, str):
            return None
        try:
            return cls(provider, configuration, resource_name)
        except ValueError:
            return None

    def to_dict(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "configuration": self.configuration,
            "resource_name": self.resource_name,
        }


@dataclass(frozen=True)
class SessionEnvironmentMemoryHealth:
    """Provider-neutral memory pressure projected for dashboard display only."""

    available_gb: float
    total_gb: float
    used_percent: float
    pressure: str

    def __post_init__(self) -> None:
        if (
            not math.isfinite(self.available_gb)
            or not math.isfinite(self.total_gb)
            or not math.isfinite(self.used_percent)
            or self.available_gb < 0
            or self.total_gb <= 0
            or self.available_gb > self.total_gb
            or self.used_percent < 0
            or self.used_percent > 100
            or self.pressure not in _MEMORY_PRESSURES
        ):
            raise ValueError("session environment memory health is invalid")

    def to_dict(self) -> dict[str, float | str]:
        return {
            "available_gb": self.available_gb,
            "total_gb": self.total_gb,
            "used_percent": self.used_percent,
            "pressure": self.pressure,
        }


@dataclass(frozen=True)
class SessionEnvironmentHealth:
    """Ephemeral provider-neutral health for one session-bound environment."""

    provider: str
    resource_name: str
    state: str
    memory: SessionEnvironmentMemoryHealth | None = None

    def __post_init__(self) -> None:
        _validate_provider(self.provider)
        if self.state not in _ENVIRONMENT_STATES:
            raise ValueError("session environment state is invalid")
        if len(self.resource_name) > _RESOURCE_NAME_MAX_CHARS or any(
            char in self.resource_name for char in "\r\n\x00"
        ):
            raise ValueError("resource_name must be one bounded display value")

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "provider": self.provider,
            "resource_name": self.resource_name,
            "state": self.state,
        }
        if self.memory is not None:
            value["memory"] = self.memory.to_dict()
        return value


class SessionEnvironmentProvider(Protocol):
    """Lifecycle adapter for one managed session-environment system."""

    provider_id: str
    display_name: str
    icon: str

    def configurations(self) -> Sequence[SessionEnvironmentConfiguration]: ...

    def validate_configuration(self, configuration: str) -> str: ...

    def binding_for_session(self, session_key: str) -> SessionEnvironmentBinding | None: ...

    def create_session_host(self, session_key: str, configuration: str) -> Any: ...

    async def request_stop_for_session(self, session_key: str) -> str | None: ...

    async def stop_for_session(self, session_key: str) -> str | None: ...


class SessionEnvironmentLifecycleProvider:
    """Positive opt-in for gateway-owned provider maintenance.

    Catalog-shaped objects are intentionally not enough to gain periodic mutation
    authority. A concrete adapter must inherit this class and keep its own trust
    checks at the provider boundary.
    """

    @property
    def lifecycle_interval_seconds(self) -> int | None:
        return None

    @property
    def runtime_warm_seconds(self) -> int | None:
        return None

    async def reconcile_lifecycle(self) -> None:
        return None


class SessionEnvironmentPrefetchProvider:
    """Positive opt-in for reconnecting without allocating or starting compute.

    Prefetch is intentionally a separate capability from the public provider
    catalog. Adapters must first prove synchronously that a session has an
    already-running binding, then return a host whose provider boundary still
    refuses every create/start path in case that state changes mid-reconnect.
    """

    def can_prefetch_session(self, session_key: str) -> bool:
        return False

    def create_prefetch_session_host(self, session_key: str, configuration: str) -> Any:
        raise SessionEnvironmentPrefetchUnavailable(
            "session environment cannot be safely prefetched"
        )


class SessionEnvironmentHealthProvider:
    """Positive opt-in for bounded, on-demand environment health inspection."""

    async def health_for_session(self, session_key: str) -> SessionEnvironmentHealth:
        raise SessionEnvironmentUnavailable("session environment health is unavailable")


class SessionEnvironmentRegistry:
    """Lookup and public catalog for enabled environment providers."""

    def __init__(
        self,
        providers: Sequence[SessionEnvironmentProvider],
        *,
        default_provider_id: str = "",
    ) -> None:
        self._providers: dict[str, SessionEnvironmentProvider] = {}
        for provider in providers:
            _validate_provider(provider.provider_id)
            if provider.provider_id in self._providers:
                raise ValueError(f"duplicate environment provider: {provider.provider_id}")
            self._providers[provider.provider_id] = provider
        if default_provider_id:
            _validate_provider(default_provider_id)
            if default_provider_id not in self._providers:
                raise ValueError("default environment provider is unavailable")
        self._default_provider_id = default_provider_id

    def get(self, provider_id: str) -> SessionEnvironmentProvider | None:
        return self._providers.get(provider_id)

    def require(self, provider_id: str) -> SessionEnvironmentProvider:
        provider = self.get(provider_id)
        if provider is None:
            raise SessionEnvironmentUnavailable(
                f"session environment provider {provider_id!r} is unavailable"
            )
        return provider

    def catalog(self) -> list[dict[str, object]]:
        return [
            {
                "id": provider.provider_id,
                "name": provider.display_name,
                "icon": provider.icon,
                "configurations": [item.to_dict() for item in provider.configurations()],
            }
            for provider in self._providers.values()
        ]

    def providers(self) -> tuple[SessionEnvironmentProvider, ...]:
        return tuple(self._providers.values())

    def supports_prefetch(
        self,
        session_key: str,
        selection: SessionEnvironmentSelection | None,
    ) -> bool:
        """Return whether a bound session can reconnect without new compute."""
        provider_id = selection.provider if selection is not None else self._default_provider_id
        provider = self.get(provider_id) if provider_id else None
        return isinstance(
            provider, SessionEnvironmentPrefetchProvider
        ) and provider.can_prefetch_session(session_key)

    def prefetch_host(
        self,
        session_key: str,
        selection: SessionEnvironmentSelection | None,
    ) -> Any:
        """Create a fail-closed reconnect host for one positively opted-in provider."""
        provider_id = selection.provider if selection is not None else self._default_provider_id
        provider = self.require(provider_id) if provider_id else None
        if not isinstance(provider, SessionEnvironmentPrefetchProvider):
            raise SessionEnvironmentPrefetchUnavailable(
                "session environment cannot be safely prefetched"
            )
        configuration = provider.validate_configuration(
            selection.configuration if selection is not None else ""
        )
        return provider.create_prefetch_session_host(session_key, configuration)


class CoderSessionEnvironmentProvider(
    SessionEnvironmentLifecycleProvider,
    SessionEnvironmentPrefetchProvider,
    SessionEnvironmentHealthProvider,
):
    """Managed Coder lifecycle exposed through the environment contract."""

    provider_id = "coder"
    display_name = "Coder"
    icon = "coder"

    def __init__(
        self,
        *,
        manager: Any,
        configurations: Mapping[str, tuple[str, str]],
        default_template: str,
        default_preset: str,
        remote_cwd: str,
        coder_bin: str,
        coder_url: str,
        session_token: str,
        runtime_warm_minutes: int,
    ) -> None:
        self.manager = manager
        self._configurations = dict(configurations)
        self._default_template = default_template
        self._default_preset = default_preset
        self._remote_cwd = remote_cwd
        self._coder_bin = coder_bin
        self._coder_url = coder_url
        self._session_token = session_token
        self._runtime_warm_minutes = runtime_warm_minutes

    def configurations(self) -> tuple[SessionEnvironmentConfiguration, ...]:
        return (
            SessionEnvironmentConfiguration(id="", name="default"),
            *(SessionEnvironmentConfiguration(id=name, name=name) for name in self._configurations),
        )

    def validate_configuration(self, configuration: str) -> str:
        if not isinstance(configuration, str):
            raise ValueError("configuration must be a string")
        if not configuration:
            return ""
        if configuration not in self._configurations:
            raise ValueError(f"Unknown Coder profile: {configuration}")
        return configuration

    def binding_for_session(self, session_key: str) -> SessionEnvironmentBinding | None:
        binding = self.manager.registry.get_by_session(session_key)
        if binding is None or getattr(binding, "state", "") == "deleted":
            return None
        return SessionEnvironmentBinding(
            provider=self.provider_id,
            resource_name=binding.workspace_name,
        )

    def _create_session_host(
        self,
        session_key: str,
        configuration: str,
        *,
        allow_start: bool,
    ) -> Any:
        from kiro_crew.acp.session_host import ManagedCoderWorkspaceSessionHost

        try:
            selected = self.validate_configuration(configuration)
            if selected:
                template, preset = self._configurations[selected]
            else:
                template, preset = self._default_template, self._default_preset
        except ValueError:
            # The protected binding is immutable allocation authority. Removing
            # a configuration must not strand an already-allocated session.
            binding = self.manager.registry.get_by_session(session_key)
            if binding is None:
                raise
            template, preset = binding.template, binding.preset
        return ManagedCoderWorkspaceSessionHost(
            session_key=session_key,
            manager=self.manager,
            remote_cwd=self._remote_cwd,
            coder_bin=self._coder_bin,
            coder_url=self._coder_url,
            session_token=self._session_token,
            runtime_warm_minutes=self._runtime_warm_minutes,
            template=template,
            preset=preset,
            allow_start=allow_start,
        )

    def create_session_host(self, session_key: str, configuration: str) -> Any:
        return self._create_session_host(session_key, configuration, allow_start=True)

    def can_prefetch_session(self, session_key: str) -> bool:
        binding = self.manager.registry.get_by_session(session_key)
        return bool(binding is not None and binding.workspace_uuid and binding.state == "running")

    def create_prefetch_session_host(self, session_key: str, configuration: str) -> Any:
        if not self.can_prefetch_session(session_key):
            raise SessionEnvironmentPrefetchUnavailable("Coder workspace is not already running")
        return self._create_session_host(session_key, configuration, allow_start=False)

    async def stop_for_session(self, session_key: str) -> str | None:
        return await self.manager.stop_for_session(session_key)

    async def request_stop_for_session(self, session_key: str) -> str | None:
        return await self.manager.request_stop_for_session(session_key)

    async def health_for_session(self, session_key: str) -> SessionEnvironmentHealth:
        from kiro_crew.coder.client import CoderClientError

        binding = self.binding_for_session(session_key)
        resource_name = binding.resource_name if binding is not None else ""
        try:
            workspace, memory = await self.manager.inspect_session_health(session_key)
        except CoderClientError:
            return SessionEnvironmentHealth(
                provider=self.provider_id,
                resource_name=resource_name,
                state="unavailable",
            )
        if workspace is None:
            return SessionEnvironmentHealth(
                provider=self.provider_id,
                resource_name=resource_name,
                state="unavailable",
            )
        state = {
            "pending": "starting",
            "starting": "starting",
            "running": "running",
            "stopped": "stopped",
        }.get(workspace.status, "unavailable")
        projected_memory = (
            SessionEnvironmentMemoryHealth(
                available_gb=memory.available_gb,
                total_gb=memory.total_gb,
                used_percent=memory.used_percent,
                pressure=memory.pressure,
            )
            if memory is not None
            else None
        )
        return SessionEnvironmentHealth(
            provider=self.provider_id,
            resource_name=workspace.name,
            state=state,
            memory=projected_memory,
        )

    @property
    def lifecycle_interval_seconds(self) -> int | None:
        from kiro_crew.coder.manager import CoderWorkspaceManager

        if not isinstance(self.manager, CoderWorkspaceManager):
            return None
        return self.manager.scope_reconcile_interval_seconds

    @property
    def runtime_warm_seconds(self) -> int | None:
        return max(0, self._runtime_warm_minutes) * 60

    async def reconcile_lifecycle(self) -> None:
        from kiro_crew.coder.manager import CoderWorkspaceManager

        # The public catalog is deliberately insufficient authority for control-
        # plane mutations. Only the concrete gateway-owned manager may reconcile
        # bindings, scopes, retention intents, and exact workspace identities.
        if not isinstance(self.manager, CoderWorkspaceManager):
            return
        await self.manager.reconcile_stop_requests()
        await self.manager.reconcile_active_scopes()
        await self.manager.reconcile_retention()


def _validate_provider(value: str) -> None:
    if not _PROVIDER_ID_RE.fullmatch(value):
        raise ValueError("provider must be one lowercase safe identifier")
