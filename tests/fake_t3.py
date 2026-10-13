import json
import sqlite3
from pathlib import Path


def create_db(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE provider_session_runtime ("
                   "thread_id TEXT PRIMARY KEY, resume_cursor_json TEXT)")
        db.execute("CREATE TABLE projection_threads (thread_id TEXT PRIMARY KEY, project_id TEXT,"
                   " settled_override TEXT, archived_at TEXT, deleted_at TEXT)")
        db.execute("CREATE TABLE projection_projects (project_id TEXT PRIMARY KEY, title TEXT)")
    db.close()


def add_thread(path: Path, thread: str, session: str, project: str = "project-1") -> None:
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO provider_session_runtime VALUES (?, ?)",
                   (thread, json.dumps({"threadId": thread, "resume": session})))
        db.execute("INSERT INTO projection_threads VALUES (?, ?, NULL, NULL, NULL)",
                   (thread, project))
        db.execute("INSERT OR IGNORE INTO projection_projects VALUES (?, ?)",
                   (project, f"Title of {project}"))
    db.close()


def set_thread(path: Path, thread: str, column: str, value: str | None) -> None:
    with sqlite3.connect(path) as db:
        db.execute(f"UPDATE projection_threads SET {column} = ? WHERE thread_id = ?",
                   (value, thread))
    db.close()


def install_agent_device(db_path: Path) -> Path:
    """Creates a fake agent-device launcher and host config next to the fake database. Returns
    the file each launcher call appends its arguments to."""
    device_dir = db_path.parent / "device"
    calls = db_path.parent / "agent-device-calls.txt"
    launcher = device_dir / "bin" / "agent-device"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text(f'#!/bin/sh\necho "$@" >> "{calls}"\n')
    launcher.chmod(0o755)
    (device_dir / "hosts").mkdir(parents=True, exist_ok=True)
    (device_dir / "hosts" / "25bf8e1a2393f1108d37029b.json").write_text("{}")
    return calls
