"""Read-only lookups in Claude Code's registries of running sessions, `sessions/<pid>.json`.

Every Claude Code config folder (`~/.claude`, `$CLAUDE_CONFIG_DIR`, `~/.claude-*` for further
accounts) keeps its own registry, and a hook running in one account looks up sessions of another.

The registry is Claude Code's internal detail, not a public API. Lookups return None when it
cannot be read, so callers treat the session's state as unknown.
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path

from devicelease.owner import process_start


@dataclass(frozen=True)
class Session:
    session_id: str
    pid: int
    proc_start: int | None
    name: str | None
    socket: str | None

    @property
    def alive(self) -> bool:
        current = process_start(self.pid)
        return current is not None and (self.proc_start is None or current == self.proc_start)

    @property
    def address(self) -> str | None:
        """The `to` address SendMessage accepts. Unlike the name, it is unique."""
        return f"uds:{self.socket}" if self.socket else None


def registry_dirs() -> list[Path]:
    override = os.environ.get("DEVICELEASE_CLAUDE_SESSIONS")
    if override:
        return [Path(entry) for entry in override.split(os.pathsep) if entry]
    home = Path.home()
    candidates = [home / ".claude", *sorted(home.glob(".claude-*"))]
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        candidates.insert(0, Path(config_dir).expanduser())
    dirs = []
    for candidate in candidates:
        registry = candidate / "sessions"
        if registry not in dirs:
            dirs.append(registry)
    return dirs


def find(session_id: str) -> Session | None:
    """The running session with this ID, or None when no registry entry has it."""
    for registry in registry_dirs():
        for path in sorted(registry.glob("*.json")):
            try:
                data = json.loads(path.read_text())
                if data.get("sessionId") != session_id:
                    continue
                start = data.get("procStart")
                return Session(session_id, int(data["pid"]), int(start) if start else None,
                               data.get("name"), data.get("messagingSocketPath"))
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                continue
    return None


def is_alive(session_id: str) -> bool | None:
    """Whether the session is running, or None when the registry cannot be read."""
    if not any(registry.is_dir() for registry in registry_dirs()):
        return None
    session = find(session_id)
    return session is not None and session.alive
