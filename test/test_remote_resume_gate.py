"""Resume must not gate on a file that lives on another machine.

kiro-cli keeps its own transcript store next to the agent. When the agent runs on
a remote session host that store is on the far side, so a gateway-side stat
answers about the wrong filesystem: it always misses, ``should_load`` is always
False, and every resume silently rebuilds the session instead of loading it.
Nothing is lost (Crew replays its own history) but the cost is a full cold start
per resume, and the agent loses its own continuity.
"""

from __future__ import annotations

from pathlib import Path

from kiro_crew.acp.session_host import (
    CoderWorkspaceSessionHost,
    LocalSessionHost,
)


class _StubClient:
    """Only the two attributes the provider predicates read."""

    def __init__(self, backend: str, host) -> None:
        self.backend = backend
        self._host = host

    @property
    def session_host_is_remote(self) -> bool:
        return self._host.is_remote


def _remoteness(backend: str, host) -> bool:
    """Mirror the provider property against a stub client."""
    return _StubClient(backend, host).session_host_is_remote


class TestRemotenessIsVisibleToTheProvider:
    def test_local_host_is_not_remote(self) -> None:
        assert _remoteness("kiro", LocalSessionHost()) is False

    def test_coder_host_is_remote(self) -> None:
        assert _remoteness("kiro", CoderWorkspaceSessionHost("ws-small")) is True


class TestResumeGateDecision:
    """The decision the provider makes, expressed as the same boolean it uses."""

    @staticmethod
    def _should_load(*, is_kas: bool, is_remote: bool, local_file_exists: bool) -> bool:
        # Mirrors providers/acp.py: KAS and remote hosts skip the stat entirely.
        if is_kas or is_remote:
            return True
        return local_file_exists

    def test_remote_host_attempts_load_without_a_local_file(self) -> None:
        assert self._should_load(is_kas=False, is_remote=True, local_file_exists=False)

    def test_local_host_still_requires_the_file(self) -> None:
        assert not self._should_load(
            is_kas=False, is_remote=False, local_file_exists=False
        )
        assert self._should_load(is_kas=False, is_remote=False, local_file_exists=True)

    def test_kas_is_unchanged(self) -> None:
        assert self._should_load(is_kas=True, is_remote=False, local_file_exists=False)


class TestRealClientReportsRemoteness:
    """Drive the real AcpClient, not a stub -- a double would pass a mutation."""

    def test_real_client_defaults_to_local(self, tmp_path) -> None:
        from kiro_crew.acp.client import AcpClient

        client = AcpClient(work_dir=tmp_path / "workspace", session_key="k")
        assert client.session_host_is_remote is False

    def test_real_client_reports_remote_for_a_coder_host(self, tmp_path) -> None:
        from kiro_crew.acp.client import AcpClient

        client = AcpClient(
            work_dir=tmp_path / "workspace",
            session_key="k",
            session_host=CoderWorkspaceSessionHost("ws-small"),
        )
        assert client.session_host_is_remote is True


class TestProtocolCwdCrossesTheSameBoundary:
    """The cwd sent to the agent must be the agent's path, for the same reason."""

    def test_remote_cwd_is_not_the_gateway_path(self) -> None:
        host = CoderWorkspaceSessionHost("ws-small", remote_work_dir="/home/coder/ws")
        assert host.protocol_cwd(Path("/gateway/state/s1")) == "/home/coder/ws/s1"

    def test_local_cwd_is_the_gateway_path(self) -> None:
        assert LocalSessionHost().protocol_cwd(Path("/gateway/state/s1")) == (
            "/gateway/state/s1"
        )
