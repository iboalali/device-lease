import json
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from devicelease import claude_sessions, t3
from devicelease.claims import write_temp
from devicelease.devices import Device
from devicelease.owner import process_alive

WAIT = "wait"
"""The waiter runs `devicelease queue --wait`, which polls until the device is its."""
MESSAGE = "message"
"""The waiter is a Claude Code session that gets a SendMessage once the device is its."""

WAIT_GRACE = timedelta(minutes=10)
"""How long a wait entry outlives its last poll, so the agent can re-run the waiting command."""

# Microseconds, unlike claims: the order of entries made within one second decides who is first.
TIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def now() -> datetime:
    return datetime.now(timezone.utc)


def format_time(value: datetime) -> str:
    return value.strftime(TIME_FORMAT)


def parse_time(value: str) -> datetime:
    return datetime.strptime(value, TIME_FORMAT).replace(tzinfo=timezone.utc)


@dataclass
class Entry:
    id: str
    owner: str
    mode: str
    project: str
    checkout: str
    created_at: datetime
    refreshed_at: datetime
    target: str | None = None
    """The device ID waited for, or None for any device that matches `kind` and `min_api`."""
    kind: str | None = None
    min_api: int | None = None
    pid: int | None = None
    pid_start: int | None = None
    claude_session: str | None = None
    t3_thread: str | None = None
    t3_project: str | None = None
    note: str | None = None

    @property
    def label(self) -> str:
        if self.target:
            return self.target
        filters = [f for f in (self.kind, f"API {self.min_api}+" if self.min_api else None) if f]
        return "any " + " ".join(filters) + " device" if filters else "any device"

    def matches(self, device: Device) -> bool:
        if self.target:
            return device.id == self.target
        return ((self.kind is None or device.kind == self.kind)
                and (self.min_api is None or (device.api or 0) >= self.min_api))

    def queue_args(self) -> str:
        return queue_args(self.target, self.kind, self.min_api)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "owner": self.owner,
            "mode": self.mode,
            "target": self.target,
            "kind": self.kind,
            "minApi": self.min_api,
            "project": self.project,
            "checkout": self.checkout,
            "createdAt": format_time(self.created_at),
            "refreshedAt": format_time(self.refreshed_at),
            "pid": self.pid,
            "pidStart": self.pid_start,
            "claudeSession": self.claude_session,
            "t3Thread": self.t3_thread,
            "t3Project": self.t3_project,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Entry":
        return cls(
            id=data["id"],
            owner=data["owner"],
            mode=data["mode"],
            target=data.get("target"),
            kind=data.get("kind"),
            min_api=data.get("minApi"),
            project=data["project"],
            checkout=data.get("checkout", data["project"]),
            created_at=parse_time(data["createdAt"]),
            refreshed_at=parse_time(data["refreshedAt"]),
            pid=data.get("pid"),
            pid_start=data.get("pidStart"),
            claude_session=data.get("claudeSession"),
            t3_thread=data.get("t3Thread"),
            t3_project=data.get("t3Project"),
            note=data.get("note"),
        )


def queue_args(target: str | None, kind: str | None, min_api: int | None) -> str:
    """The `devicelease queue` arguments that wait for this target."""
    if target:
        return target
    return " ".join(["--any"] + ([f"--{kind}"] if kind else [])
                    + ([f"--min-api {min_api}"] if min_api else []))


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def ended_reason(entry: Entry, at: datetime | None = None) -> str | None:
    """Returns why a queue entry no longer waits, or None while it does."""
    at = at or now()
    if entry.t3_thread:
        reason = t3.thread_end_reason(entry.t3_thread)
        if reason:
            return reason
    if entry.mode == WAIT and at - entry.refreshed_at > WAIT_GRACE:
        return "waiting command stopped"
    if entry.mode == MESSAGE and entry.claude_session:
        if claude_sessions.is_alive(entry.claude_session) is False:
            return "session ended"
    if entry.pid is not None and not process_alive(entry.pid, entry.pid_start):
        return "owner process exited"
    return None


class QueueStore:
    """One JSON file per waiting entry. Changes hold the claims store's lock, so a hand-off and
    a claim cannot race."""

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def path(self, entry_id: str) -> Path:
        return self.root / f"{entry_id}.json"

    def _load(self, path: Path) -> Entry | None:
        try:
            with open(path) as f:
                return Entry.from_dict(json.load(f))
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError) as e:
            print(f"devicelease: ignoring unreadable queue entry {path}: {e}", file=sys.stderr)
            return None

    def read(self, entry_id: str) -> Entry | None:
        return self._load(self.path(entry_id))

    def all(self) -> list[Entry]:
        """Every entry, oldest first."""
        entries = (self._load(p) for p in self.root.glob("*.json"))
        return sorted((e for e in entries if e is not None), key=lambda e: (e.created_at, e.id))

    def write(self, entry: Entry) -> None:
        os.replace(write_temp(self.root, entry.to_dict()), self.path(entry.id))

    def delete(self, entry_id: str) -> None:
        try:
            self.path(entry_id).unlink()
        except FileNotFoundError:
            pass
