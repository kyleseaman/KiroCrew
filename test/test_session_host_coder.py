"""Coder session host: env allowlist, remote path mapping, transport invariants.

Everything here is a pure function over inputs -- no coderd, no workspace -- so
the security-relevant half (what an env may carry off-box) is testable without
infrastructure. Whether the transport actually connects is a separate, live
question this file deliberately does not claim to answer.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kiro_crew.acp.session_host import (
    REMOTE_ENV_ALLOWLIST,
    CoderWorkspaceHostError,
    CoderWorkspaceSessionHost,
    LocalSessionHost,
    default_session_host,
)


class TestEnvAllowlist:
    def test_cloud_credentials_are_not_forwarded(self) -> None:
        """The gateway's cloud credentials must never reach another machine."""
        host = CoderWorkspaceSessionHost("ws-small")
        env = {
            "AWS_SECRET_ACCESS_KEY": "SECRET-must-not-travel",
            "AWS_SESSION_TOKEN": "SESSION-must-not-travel",
            "AWS_ACCESS_KEY_ID": "AKID-must-not-travel",
            "KIROCREW_SESSION_KEY": "sess-1",
        }
        forwarded = host._forwarded_env(env)
        joined = " ".join(forwarded)
        assert "must-not-travel" not in joined
        assert forwarded == ["KIROCREW_SESSION_KEY=sess-1"]

    def test_allowlist_holds_no_credential_names(self) -> None:
        """A future edit must not quietly add a credential to the allowlist."""
        banned = ("TOKEN", "SECRET", "KEY", "PASSWORD", "CREDENTIAL")
        for name in REMOTE_ENV_ALLOWLIST:
            # SESSION_KEY is Crew's own slot identifier, not a credential.
            if name == "KIROCREW_SESSION_KEY":
                continue
            assert not any(b in name.upper() for b in banned), name

    def test_absent_and_empty_values_are_skipped(self) -> None:
        host = CoderWorkspaceSessionHost("ws-small")
        assert host._forwarded_env({}) == []
        assert host._forwarded_env({"KIROCREW_CHANNEL_ID": ""}) == []


class TestRemotePathMapping:
    def test_protocol_cwd_is_the_remote_path_not_the_gateway_path(self) -> None:
        host = CoderWorkspaceSessionHost("ws-small", remote_work_dir="/home/coder/ws")
        cwd = host.protocol_cwd(Path("/local/gateway/state/session-abc"))
        assert cwd == "/home/coder/ws/session-abc"
        assert "/local/gateway" not in cwd

    def test_trailing_slash_in_base_does_not_double(self) -> None:
        host = CoderWorkspaceSessionHost("ws-small", remote_work_dir="/home/coder/ws/")
        assert host.protocol_cwd(Path("/x/session-abc")) == "/home/coder/ws/session-abc"

    def test_local_host_still_reports_the_local_path(self) -> None:
        assert LocalSessionHost().protocol_cwd(Path("/x/y")) == "/x/y"


class TestTransportTokenInvariants:
    """coder ssh re-joins remote args, so a whitespace token cannot survive."""

    def test_workspace_with_whitespace_is_refused(self) -> None:
        with pytest.raises(CoderWorkspaceHostError, match="whitespace"):
            CoderWorkspaceSessionHost("ws small")

    def test_remote_dir_with_whitespace_is_refused(self) -> None:
        with pytest.raises(CoderWorkspaceHostError, match="whitespace"):
            CoderWorkspaceSessionHost("ws-small", remote_work_dir="/home/my dir")

    def test_blank_workspace_is_refused(self) -> None:
        with pytest.raises(CoderWorkspaceHostError, match="workspace name"):
            CoderWorkspaceSessionHost("   ")


class TestHostSelection:
    def test_default_is_local(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("KIROCREW_SESSION_HOST", raising=False)
        assert default_session_host().name == "local"

    def test_coder_requires_a_workspace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIROCREW_SESSION_HOST", "coder")
        monkeypatch.delenv("KIROCREW_CODER_WORKSPACE", raising=False)
        with pytest.raises(CoderWorkspaceHostError, match="workspace"):
            default_session_host()

    def test_coder_selected_when_named(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIROCREW_SESSION_HOST", "coder")
        monkeypatch.setenv("KIROCREW_CODER_WORKSPACE", "ws-build")
        host = default_session_host()
        assert host.name == "coder"
        assert host.is_remote is True

    def test_unknown_host_value_falls_back_to_local(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unrecognised value must not silently become a remote host."""
        monkeypatch.setenv("KIROCREW_SESSION_HOST", "somethingelse")
        assert default_session_host().name == "local"

    def test_is_remote_distinguishes_the_hosts(self) -> None:
        assert LocalSessionHost().is_remote is False
        assert CoderWorkspaceSessionHost("ws-small").is_remote is True


class TestRemoteArgvShape:
    def test_remote_command_carries_chdir_and_allowlisted_env_only(self) -> None:
        """The remote command sets its own cwd and forwards nothing extra."""
        host = CoderWorkspaceSessionHost("ws-small", remote_work_dir="/home/coder/ws")
        forwarded = host._forwarded_env(
            {"KIROCREW_SESSION_KEY": "s1", "AWS_SESSION_TOKEN": "nope"}
        )
        remote_argv = [
            "env",
            f"--chdir={host.protocol_cwd(Path('/x/session-1'))}",
            *forwarded,
            "kiro-cli",
            "acp",
        ]
        assert remote_argv[1] == "--chdir=/home/coder/ws/session-1"
        assert "KIROCREW_SESSION_KEY=s1" in remote_argv
        assert not any("nope" in tok for tok in remote_argv)

    def test_os_environ_is_not_the_source_of_forwarded_values(self) -> None:
        """Forwarding reads the passed env, never the gateway's own process env."""
        host = CoderWorkspaceSessionHost("ws-small")
        os.environ["KIROCREW_CHANNEL_ID"] = "leaked-from-process-env"
        try:
            assert host._forwarded_env({}) == []
        finally:
            os.environ.pop("KIROCREW_CHANNEL_ID", None)
