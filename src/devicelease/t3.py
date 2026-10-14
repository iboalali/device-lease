"""Read-only lookups in T3 Code's state database, and ending its agent-device sessions.

The schema and the session naming are T3 Code's internal details, not a public API. Every lookup fails open: on any error it
prints one warning and returns None, so callers fall back to the behavior without T3 Code.
"""

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from devicelease import cleanup

LOCAL_HOST = "local"

_warned = False


def db_path() -> Path:
    override = os.environ.get("DEVICELEASE_T3_DB")
    if override:
        return Path(override)
    return Path.home() / ".t3" / "userdata" / "state.sqlite"


def _warn(error: Exception) -> None:
    global _warned
    if not _warned:
        print(f"devicelease: T3 Code lookup failed, ignoring it: {error}", file=sys.stderr)
        _warned = True


class _Unavailable(Exception):
    pass


def _query(sql: str, params: tuple):
    """Returns the first row or None. Raises _Unavailable when T3 Code's database cannot be read."""
    path = db_path()
    if not path.is_file():
        raise _Unavailable
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        try:
            return db.execute(sql, params).fetchone()
        finally:
            db.close()
    except sqlite3.Error as e:
        _warn(e)
        raise _Unavailable from e


def thread_for_session(session_id: str) -> str | None:
    """The T3 Code thread that runs this Claude Code session, or None outside T3 Code."""
    try:
        row = _query(
            "SELECT thread_id FROM provider_session_runtime"
            " WHERE json_extract(resume_cursor_json, '$.resume') = ?",
            (session_id,),
        )
    except _Unavailable:
        return None
    return row[0] if row else None


def thread_end_reason(thread_id: str) -> str | None:
    """Why the thread is finished, "" while it is active, or None when T3 Code cannot be read."""
    try:
        row = _query(
            "SELECT settled_override, archived_at, deleted_at FROM projection_threads"
            " WHERE thread_id = ?",
            (thread_id,),
        )
    except _Unavailable:
        return None
    if row is None:
        return "thread no longer exists"
    settled, archived, deleted = row
    if deleted:
        return "thread deleted"
    if archived:
        return "thread archived"
    if settled == "settled":
        return "thread settled"
    return ""


def project_for_thread(thread_id: str) -> tuple[str, str] | None:
    """The T3 Code project ID and title of a thread."""
    try:
        row = _query(
            "SELECT p.project_id, p.title FROM projection_threads t"
            " JOIN projection_projects p ON p.project_id = t.project_id WHERE t.thread_id = ?",
            (thread_id,),
        )
    except _Unavailable:
        return None
    return (row[0], row[1]) if row else None


def _key(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:24]


def agent_device_session(thread_id: str, device_id: str, host_id: str = LOCAL_HOST) -> str:
    """The agent-device session T3 Code pins a thread's device to. Must match T3 Code's
    `agentDeviceSession`: sha256 of the JSON array [threadId, hostId, deviceId]."""
    return "t3-" + _key(json.dumps([thread_id, host_id, device_id], separators=(",", ":")))


def close_agent_device(thread_id: str, device_ids: list[str], log_path: Path) -> None:
    """Ends the agent-device sessions of a thread's device.

    T3 Code's device_close only removes the Device panel, and agent-device keeps its own hold on
    the device until its session is closed. The deviceId T3 Code hashed is not known for certain
    (the ID passed to device_open, or the device's serial), so every candidate is tried. A session
    only exists once an agent-device command ran in it, and closing a missing one still leaves an
    empty session folder and a diagnostics log, so candidates without a session folder are skipped.
    """
    device_dir = db_path().parent / "device"
    launcher = device_dir / "bin" / "agent-device"
    config = device_dir / "hosts" / f"{_key(LOCAL_HOST)}.json"
    if not launcher.is_file() or not config.is_file():
        return
    log_path.parent.mkdir(parents=True, exist_ok=True)
    sessions = [
        (device_id, agent_device_session(thread_id, device_id))
        for device_id in dict.fromkeys(device_ids)
    ]
    sessions = [(d, s) for d, s in sessions if (device_dir / "agent-device" / "sessions" / s).is_dir()]
    if not sessions:
        return
    cleanup.rotate(log_path)
    with open(log_path, "a") as log:
        for device_id, session in sessions:
            log.write(f"close {session} (thread {thread_id}, device {device_id})\n")
            log.flush()
            try:
                subprocess.run([str(launcher), "close", "--config", str(config), "--session", session],
                               stdin=subprocess.DEVNULL, stdout=log, stderr=log, timeout=20)
            except (OSError, subprocess.TimeoutExpired) as e:
                log.write(f"failed: {e}\n")
