import fcntl
import json
import os
import sys
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from devicelease import t3
from devicelease.owner import HUMAN, process_alive

TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def format_time(value: datetime) -> str:
    return value.strftime(TIME_FORMAT)


def parse_time(value: str) -> datetime:
    return datetime.strptime(value, TIME_FORMAT).replace(tzinfo=timezone.utc)


@dataclass
class Claim:
    device: str
    kind: str
    owner: str
    project: str
    created_at: datetime
    heartbeat_at: datetime
    serial: str | None = None
    platform: str = "android"
    pid: int | None = None
    pid_start: int | None = None
    seen_online: bool = False
    t3_thread: str | None = None
    t3_device_id: str | None = None
    """The deviceId T3 Code's device_open used, for claims made by opening the Device panel."""
    note: str | None = None

    @property
    def human(self) -> bool:
        return self.owner == HUMAN

    def to_dict(self) -> dict:
        return {
            "device": self.device,
            "platform": self.platform,
            "kind": self.kind,
            "serial": self.serial,
            "owner": self.owner,
            "project": self.project,
            "pid": self.pid,
            "pidStart": self.pid_start,
            "createdAt": format_time(self.created_at),
            "heartbeatAt": format_time(self.heartbeat_at),
            "seenOnline": self.seen_online,
            "t3Thread": self.t3_thread,
            "t3DeviceId": self.t3_device_id,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Claim":
        return cls(
            device=data["device"],
            platform=data.get("platform", "android"),
            kind=data["kind"],
            serial=data.get("serial"),
            owner=data["owner"],
            project=data["project"],
            pid=data.get("pid"),
            pid_start=data.get("pidStart"),
            created_at=parse_time(data["createdAt"]),
            heartbeat_at=parse_time(data["heartbeatAt"]),
            seen_online=data.get("seenOnline", False),
            t3_thread=data.get("t3Thread"),
            t3_device_id=data.get("t3DeviceId"),
            note=data.get("note"),
        )


def stale_reason(claim: Claim, expiry: timedelta, online: set[str] | None,
                 at: datetime | None = None) -> str | None:
    """Returns why a claim no longer holds its device, or None while it is valid.

    `online` is the set of device IDs currently online, or None when discovery failed. A human
    claim holds until its device disconnects or its emulator closes, so it needs that set. A T3
    Code thread's claim holds until the thread is settled, archived or deleted; when T3 Code's
    database cannot be read it falls back to the heartbeat.
    """
    at = at or now()
    if claim.human:
        if online is None or claim.device in online:
            return None
        if claim.seen_online:
            return "emulator closed" if claim.kind == "emulator" else "device disconnected"
        if at - claim.created_at > expiry:
            return "device never came online"
        return None
    if claim.t3_thread:
        reason = t3.thread_end_reason(claim.t3_thread)
        if reason is not None:
            return reason or None
    if at - claim.heartbeat_at > expiry:
        return f"no heartbeat for {int((at - claim.heartbeat_at).total_seconds() // 60)}m"
    if claim.pid is not None and not process_alive(claim.pid, claim.pid_start):
        return "owner process exited"
    return None


class Store:
    """One JSON file per claimed device. Every change happens under one exclusive lock."""

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def path(self, device: str) -> Path:
        return self.root / f"{quote(device, safe='')}.json"

    @contextmanager
    def lock(self):
        with open(self.root / ".lock", "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def _load(self, path: Path) -> Claim | None:
        try:
            with open(path) as f:
                return Claim.from_dict(json.load(f))
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError) as e:
            print(f"devicelease: ignoring unreadable claim {path}: {e}", file=sys.stderr)
            return None

    def read(self, device: str) -> Claim | None:
        return self._load(self.path(device))

    def all(self) -> list[Claim]:
        claims = (self._load(p) for p in sorted(self.root.glob("*.json")))
        return [c for c in claims if c is not None]

    def _write_temp(self, claim: Claim) -> Path:
        temp = self.root / f".tmp-{os.getpid()}-{uuid.uuid4().hex}"
        with open(temp, "w") as f:
            json.dump(claim.to_dict(), f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        return temp

    def create(self, claim: Claim) -> bool:
        """Adds a claim unless the device already has one. Atomic even without the lock."""
        temp = self._write_temp(claim)
        try:
            os.link(temp, self.path(claim.device))
            return True
        except FileExistsError:
            return False
        finally:
            temp.unlink()

    def write(self, claim: Claim) -> None:
        os.replace(self._write_temp(claim), self.path(claim.device))

    def delete(self, device: str) -> None:
        try:
            self.path(device).unlink()
        except FileNotFoundError:
            pass
