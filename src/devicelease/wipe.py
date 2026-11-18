"""Wiping pool emulators between agents, and the marks that track it.

A pool emulator gets a "wipe pending" mark when an agent's claim on it ends, and is stopped.
The next boot through devicelease wipes its user data and runs its afterWipe step. A boot that
fails gets a "failed" mark instead, which keeps the emulator out of rotation until the user
boots it successfully.
"""

import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from devicelease import paths
from devicelease.devices import DiscoveryError, _adb

STOP_TIMEOUT = 60
AFTER_WIPE_TIMEOUT = 300


def _pending_dir() -> Path:
    return paths.state_dir() / "wipe"


def _failed_dir() -> Path:
    return paths.state_dir() / "failed"


def log_path(avd: str) -> Path:
    return paths.state_dir() / "logs" / f"{avd}.log"


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".tmp-{path.name}-{os.getpid()}")
    temp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(temp, path)


def _read(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def mark_pending(avd: str, owner: str, project: str) -> None:
    _write(_pending_dir() / avd, {"at": _now(), "owner": owner, "project": project})


def pending(avd: str) -> bool:
    return (_pending_dir() / avd).exists()


def clear_pending(avd: str) -> None:
    (_pending_dir() / avd).unlink(missing_ok=True)


def mark_failed(avd: str, what: str, owner: str, project: str) -> dict:
    """Records a failure and keeps a copy of the boot log, which the next boot overwrites."""
    log = log_path(avd)
    kept = log.with_name(f"{avd}.failed.log")
    try:
        shutil.copyfile(log, kept)
    except OSError:
        kept.parent.mkdir(parents=True, exist_ok=True)
        kept.write_text("No boot log was written.\n")
    record = {"what": what, "at": _now(), "owner": owner, "project": project, "log": str(kept)}
    _write(_failed_dir() / avd, record)
    return record


def failed(avd: str) -> dict | None:
    return _read(_failed_dir() / avd)


def failed_avds() -> set[str]:
    return {p.name for p in _failed_dir().glob("*") if not p.name.startswith(".")}


def clear_failed(avd: str) -> None:
    (_failed_dir() / avd).unlink(missing_ok=True)


def _emulator_pids(avd: str) -> list[int]:
    """The emulator processes running this AVD, found by `-avd <name>` or `@<name>`."""
    pids = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            args = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        names = [a.decode(errors="replace") for a in args]
        if f"@{avd}" in names or any(a == "-avd" and b == avd for a, b in zip(names, names[1:])):
            pids.append(int(entry.name))
    return pids


def running(avd: str) -> bool:
    return bool(_emulator_pids(avd))


def stop(serial: str | None) -> None:
    """Asks a running emulator to shut down, without waiting for it."""
    if not serial:
        return
    try:
        _adb("emu", "kill", serial=serial, timeout=10)
    except DiscoveryError:
        pass


def wait_stopped(avd: str, timeout: float = STOP_TIMEOUT) -> bool:
    """Waits until no emulator process runs the AVD. The AVD's multiinstance.lock stays on disk
    after the emulator exits, so the process is what counts."""
    deadline = time.monotonic() + timeout
    while running(avd):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.5)
    return True


def run_after_wipe(command: str, home: str | None, serial: str, avd: str) -> int:
    """Runs an afterWipe command, appending its output to the emulator's log. Returns its exit
    code."""
    log = log_path(avd)
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a") as out:
        out.write(f"\n== devicelease afterWipe: {command} ==\n")
        out.flush()
        try:
            result = subprocess.run(
                command, shell=True, cwd=home or None, stdin=subprocess.DEVNULL, stdout=out,
                stderr=subprocess.STDOUT, timeout=AFTER_WIPE_TIMEOUT,
                env={**os.environ, "ANDROID_SERIAL": serial},
            )
        except subprocess.TimeoutExpired:
            out.write(f"\n== afterWipe timed out after {AFTER_WIPE_TIMEOUT}s ==\n")
            return 124
        except OSError as e:
            out.write(f"\n== afterWipe could not run: {e} ==\n")
            return 127
        out.write(f"\n== afterWipe exited with {result.returncode} ==\n")
        return result.returncode
