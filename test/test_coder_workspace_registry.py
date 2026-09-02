"""Durable ownership registry for per-parent Coder workspaces."""

from __future__ import annotations

import json
import re
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

import kiro_crew.coder.registry as registry_mod
from kiro_crew.coder.registry import (
    WorkspaceBindingRegistry,
    WorkspaceRegistryCorrupt,
)
from kiro_crew.security import is_sensitive_path


def test_allocate_is_idempotent_for_one_parent_and_distinct_between_parents(
    tmp_path: Path,
) -> None:
    registry = WorkspaceBindingRegistry(tmp_path / "coder_workspaces.json")

    first = registry.allocate(
        "dashboard:one",
        template="kirocrew-arm",
        preset="arm-small",
        prefix="crew-session",
        owner_name="kyleseaman",
    )
    again = registry.allocate(
        "dashboard:one",
        template="kirocrew-arm",
        preset="arm-small",
        prefix="crew-session",
        owner_name="kyleseaman",
    )
    other = registry.allocate(
        "dashboard:two",
        template="kirocrew-arm",
        preset="arm-small",
        prefix="crew-session",
        owner_name="kyleseaman",
    )

    assert again == first
    assert other.binding_id != first.binding_id
    assert other.workspace_name != first.workspace_name
    assert first.workspace_name.startswith("crew-session-kyleseaman-")
    assert len(first.workspace_name) <= 32
    assert "dashboard" not in first.workspace_name
    assert "one" not in first.workspace_name
    assert re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", first.workspace_name)


def test_registry_round_trips_and_writes_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "coder_workspaces.json"
    created = WorkspaceBindingRegistry(path).allocate(
        "cron:job:run",
        template="kirocrew-arm",
        preset="",
        prefix="crew-session",
        owner_name="kyleseaman",
    )

    loaded = WorkspaceBindingRegistry(path).get_by_session("cron:job:run")

    assert loaded == created
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert payload["bindings"][created.binding_id]["workspace_uuid"] == ""


def test_unchanged_registry_is_parsed_once_per_registry_instance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "coder_workspaces.json"
    created = WorkspaceBindingRegistry(path).allocate(
        "dashboard:one",
        template="kirocrew-arm",
        preset="",
        prefix="crew-session",
        owner_name="kyleseaman",
    )
    loads = 0
    original_loads = registry_mod.json.loads

    def counting_loads(value: str):
        nonlocal loads
        loads += 1
        return original_loads(value)

    monkeypatch.setattr(registry_mod.json, "loads", counting_loads)
    registry = WorkspaceBindingRegistry(path)

    assert registry.get(created.binding_id) == created
    assert registry.get_by_session("dashboard:one") == created
    assert registry.list_bindings() == (created,)
    assert loads == 1


def test_owner_name_with_email_shape_is_not_exposed_in_workspace_name(tmp_path: Path) -> None:
    registry = WorkspaceBindingRegistry(tmp_path / "coder_workspaces.json")

    binding = registry.allocate(
        "dashboard:one",
        template="kirocrew-arm",
        preset="",
        prefix="crew-session",
        owner_name="operator@example.com",
    )

    assert binding.workspace_name.startswith("crew-session-user-")
    assert "operator" not in binding.workspace_name
    assert "example" not in binding.workspace_name


def test_corrupt_registry_fails_closed_without_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "coder_workspaces.json"
    original = "{not-json"
    path.write_text(original, encoding="utf-8")
    registry = WorkspaceBindingRegistry(path)

    with pytest.raises(WorkspaceRegistryCorrupt):
        registry.allocate(
            "dashboard:one",
            template="kirocrew-arm",
            preset="",
            prefix="crew-session",
            owner_name="kyleseaman",
        )

    assert path.read_text(encoding="utf-8") == original


def test_explicit_repair_quarantines_corrupt_registry_without_adopting_workspaces(
    tmp_path: Path,
) -> None:
    path = tmp_path / "coder_workspaces.json"
    original = "{not-json"
    path.write_text(original, encoding="utf-8")
    registry = WorkspaceBindingRegistry(path)

    quarantined = registry.quarantine_corrupt()

    assert quarantined is not None
    assert quarantined.read_text(encoding="utf-8") == original
    assert stat.S_IMODE(quarantined.stat().st_mode) == 0o600
    assert quarantined.parent.name == "coder_workspaces.json.corrupt"
    assert registry.list_bindings() == ()
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "bindings": {},
        "version": 1,
    }


def test_environment_repair_command_requires_confirmation_and_reports_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import kiro_crew.cli as cli

    path = tmp_path / "coder_workspaces.json"
    path.write_text("{not-json", encoding="utf-8")
    monkeypatch.setattr(cli, "config_dir", lambda: tmp_path)

    assert cli._environment_cmd(SimpleNamespace(environment_action="repair", yes=False)) == 2
    assert path.read_text(encoding="utf-8") == "{not-json"
    assert cli._environment_cmd(SimpleNamespace(environment_action="repair", yes=True)) == 0

    output = capsys.readouterr()
    assert "--yes" in output.err
    assert "coder_workspaces.json.corrupt" in output.out


def test_identity_allocation_fails_closed_after_bounded_collisions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = WorkspaceBindingRegistry(tmp_path / "coder_workspaces.json")
    registry.allocate(
        "dashboard:one",
        template="kirocrew-arm",
        preset="",
        prefix="crew-session",
        owner_name="kyleseaman",
    )
    monkeypatch.setattr(registry_mod.secrets, "token_bytes", lambda _size: b"\0" * 5)
    first_collision = registry.allocate(
        "dashboard:collision-seed",
        template="kirocrew-arm",
        preset="",
        prefix="crew-session",
        owner_name="kyleseaman",
    )
    assert first_collision.binding_id == "aaaaaaaa"

    with pytest.raises(WorkspaceRegistryCorrupt, match="unique Coder workspace identity"):
        registry.allocate(
            "dashboard:two",
            template="kirocrew-arm",
            preset="",
            prefix="crew-session",
            owner_name="kyleseaman",
        )


@pytest.mark.parametrize("prefix", (".kiro/crew", ".kirocrew"))
def test_registry_is_a_read_write_keystone(prefix: str) -> None:
    assert is_sensitive_path(f"~/{prefix}/coder_workspaces.json")
    assert is_sensitive_path(f"~/{prefix}/coder_workspaces.lock")
    assert is_sensitive_path(
        f"~/{prefix}/coder_workspaces.json.corrupt/20260901T120000000000Z.json"
    )
