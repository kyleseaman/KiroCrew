"""Integrity-sensitive ownership records for managed Coder workspaces."""

from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

from kiro_crew.atomic_write import atomic_write
from kiro_crew.platform_compat import file_lock

_SCHEMA_VERSION = 1
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_BINDING_BYTES = 8


class WorkspaceRegistryCorrupt(RuntimeError):
    """The ownership registry cannot safely authorize lifecycle mutations."""


@dataclass(frozen=True)
class WorkspaceBinding:
    binding_id: str
    session_key: str
    workspace_name: str
    workspace_uuid: str
    owner_id: str
    organization_id: str
    deployment_id: str
    template: str
    preset: str
    generation: int
    state: str
    created_at: str
    last_activity_at: str
    deletion_due_at: str
    deleted_at: str
    failure_code: str

    @classmethod
    def from_dict(cls, value: object) -> WorkspaceBinding:
        if not isinstance(value, dict):
            raise WorkspaceRegistryCorrupt("Coder workspace binding is not an object")
        try:
            binding = cls(**{field: value[field] for field in cls.__dataclass_fields__})
        except (KeyError, TypeError) as exc:
            raise WorkspaceRegistryCorrupt("Coder workspace binding has an invalid shape") from exc
        if (
            not binding.binding_id
            or not binding.session_key
            or not _SAFE_NAME_RE.fullmatch(binding.workspace_name)
            or not isinstance(binding.generation, int)
            or binding.generation < 1
        ):
            raise WorkspaceRegistryCorrupt("Coder workspace binding has invalid identity fields")
        return binding


class WorkspaceBindingRegistry:
    """Atomic, lock-serialized binding registry; corrupt input fails closed."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock_path = path.with_suffix(".lock")

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def _read_unlocked(self) -> dict[str, WorkspaceBinding]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkspaceRegistryCorrupt("Coder workspace registry is unreadable") from exc
        if not isinstance(raw, dict) or raw.get("version") != _SCHEMA_VERSION:
            raise WorkspaceRegistryCorrupt("Coder workspace registry version is invalid")
        values = raw.get("bindings")
        if not isinstance(values, dict):
            raise WorkspaceRegistryCorrupt("Coder workspace registry bindings are invalid")
        parsed: dict[str, WorkspaceBinding] = {}
        sessions: set[str] = set()
        for key, value in values.items():
            binding = WorkspaceBinding.from_dict(value)
            if key != binding.binding_id or binding.session_key in sessions:
                raise WorkspaceRegistryCorrupt("Coder workspace registry identity is ambiguous")
            parsed[key] = binding
            sessions.add(binding.session_key)
        return parsed

    def _write_unlocked(self, bindings: dict[str, WorkspaceBinding]) -> None:
        payload = {
            "version": _SCHEMA_VERSION,
            "bindings": {key: asdict(value) for key, value in sorted(bindings.items())},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(
            self.path,
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            fsync=True,
            restrict_to_owner=True,
        )

    def _open_lock(self) -> BinaryIO:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = open(self.lock_path, "a+b")
        if os.fstat(fd.fileno()).st_size == 0:
            fd.write(b"0")
            fd.flush()
        return fd

    def allocate(
        self,
        session_key: str,
        *,
        template: str,
        preset: str,
        prefix: str,
    ) -> WorkspaceBinding:
        if not session_key:
            raise ValueError("session_key is required")
        if not _SAFE_NAME_RE.fullmatch(template):
            raise ValueError("template must be one safe Coder name")
        if preset and not _SAFE_NAME_RE.fullmatch(preset):
            raise ValueError("preset must be one safe Coder name")
        if not _SAFE_NAME_RE.fullmatch(prefix) or len(prefix) > 32:
            raise ValueError("prefix must be one safe Coder name of at most 32 characters")
        with self._open_lock() as lock_fd:
            with file_lock(lock_fd.fileno(), exclusive=True, required=True):
                bindings = self._read_unlocked()
                for binding in bindings.values():
                    if binding.session_key == session_key:
                        return binding
                now = self._now()
                while True:
                    binding_id = secrets.token_urlsafe(_BINDING_BYTES).rstrip("=")
                    workspace_name = f"{prefix}-{binding_id}"
                    if binding_id not in bindings and _SAFE_NAME_RE.fullmatch(workspace_name):
                        break
                binding = WorkspaceBinding(
                    binding_id=binding_id,
                    session_key=session_key,
                    workspace_name=workspace_name,
                    workspace_uuid="",
                    owner_id="",
                    organization_id="",
                    deployment_id="",
                    template=template,
                    preset=preset,
                    generation=1,
                    state="allocated",
                    created_at=now,
                    last_activity_at=now,
                    deletion_due_at="",
                    deleted_at="",
                    failure_code="",
                )
                bindings[binding_id] = binding
                self._write_unlocked(bindings)
                return binding

    def list_bindings(self) -> tuple[WorkspaceBinding, ...]:
        with self._open_lock() as lock_fd:
            with file_lock(lock_fd.fileno(), exclusive=False, required=True):
                return tuple(self._read_unlocked().values())

    def get(self, binding_id: str) -> WorkspaceBinding | None:
        return next((item for item in self.list_bindings() if item.binding_id == binding_id), None)

    def get_by_session(self, session_key: str) -> WorkspaceBinding | None:
        return next(
            (item for item in self.list_bindings() if item.session_key == session_key), None
        )

    def replace(self, binding: WorkspaceBinding) -> WorkspaceBinding:
        with self._open_lock() as lock_fd:
            with file_lock(lock_fd.fileno(), exclusive=True, required=True):
                bindings = self._read_unlocked()
                current = bindings.get(binding.binding_id)
                if current is None:
                    raise WorkspaceRegistryCorrupt("Coder workspace binding disappeared")
                if (
                    current.session_key != binding.session_key
                    or current.workspace_name != binding.workspace_name
                ):
                    raise WorkspaceRegistryCorrupt("Coder workspace binding identity changed")
                bindings[binding.binding_id] = binding
                self._write_unlocked(bindings)
                return binding
