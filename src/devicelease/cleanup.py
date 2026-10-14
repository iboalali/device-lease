"""Keeps the state folder from growing: rotates logs, trims emulator boot logs, and removes
leftover temp files and broken claim and queue files."""

import json
import os
import time
from pathlib import Path

from devicelease import paths

LOG_LIMIT = 5 * 1024 * 1024
BOOT_LOG_LIMIT = 50 * 1024 * 1024
BOOT_LOG_AGE = 7 * 24 * 3600
SESSION_RECORD_AGE = 7 * 24 * 3600
LEFTOVER_AGE = 3600
INTERVAL = 3600

APPEND_LOGS = ("notify.log", "watcher.log", "agent-device.log")
"""Logs that grow by appending. Every other log in the folder is an emulator's boot log."""


def rotate(path: Path) -> None:
    """Moves a log over the limit to `<name>.1`, replacing the previous copy. Call before
    opening it for appending."""
    try:
        if path.stat().st_size > LOG_LIMIT:
            os.replace(path, path.with_name(path.name + ".1"))
    except FileNotFoundError:
        pass


def _age(path: Path, at: float) -> float:
    return at - path.stat().st_mtime


def _broken(path: Path, load) -> bool:
    """Whether the stores would skip this file, the same test they apply when reading it."""
    try:
        load(json.loads(path.read_text()))
    except (ValueError, KeyError, TypeError, AttributeError):
        return True
    except OSError:
        return False
    return False


def run(running_avds: set[str] | None) -> list[tuple[str, str]]:
    """Cleans the state folder. Returns each changed file with what happened to it.

    `running_avds` holds the AVD names of running emulators, or None when discovery failed,
    which leaves boot logs alone.
    """
    # Imported here: t3 rotates its log through this module, and queue imports t3.
    from devicelease.claims import Claim
    from devicelease.queue import Entry

    at = time.time()
    done = []
    state = paths.state_dir()
    logs = state / "logs"
    for path in sorted(logs.glob("*.log")):
        try:
            if path.name in APPEND_LOGS:
                if path.stat().st_size > LOG_LIMIT:
                    rotate(path)
                    done.append((str(path), "rotated"))
            elif running_avds is None:
                continue
            elif path.stem in running_avds:
                if path.stat().st_size > BOOT_LOG_LIMIT:
                    os.truncate(path, 0)
                    done.append((str(path), "emptied"))
            elif _age(path, at) > BOOT_LOG_AGE:
                path.unlink()
                done.append((str(path), "deleted, emulator not used for 7 days"))
        except FileNotFoundError:
            continue
    stores = ((paths.claims_dir(), Claim.from_dict), (paths.queue_dir(), Entry.from_dict))
    for folder, load in stores:
        for path in sorted(folder.glob(".tmp-*")) + sorted(folder.glob("*.json")):
            try:
                if _age(path, at) <= LEFTOVER_AGE:
                    continue
                if path.name.startswith(".tmp-"):
                    reason = "deleted, leftover temp file"
                elif _broken(path, load):
                    reason = "deleted, unreadable"
                else:
                    continue
                path.unlink()
                done.append((str(path), reason))
            except FileNotFoundError:
                continue
    for path in sorted(paths.sessions_dir().glob("*.json")):
        try:
            if _age(path, at) > SESSION_RECORD_AGE:
                path.unlink()
                done.append((str(path), "deleted, session not seen for 7 days"))
        except FileNotFoundError:
            continue
    stamp = state / ".cleanup"
    stamp.touch()
    os.utime(stamp, (at, at))
    return done


def due() -> bool:
    try:
        return time.time() - (paths.state_dir() / ".cleanup").stat().st_mtime > INTERVAL
    except FileNotFoundError:
        return True
