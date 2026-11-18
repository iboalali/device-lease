"""Desktop notifications, for what the user has to know even when no agent passes it on."""

import os
import shutil
import subprocess

from devicelease import cleanup, paths


def _log(line: str) -> None:
    log = paths.state_dir() / "logs" / "notify.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    cleanup.rotate(log)
    with open(log, "a") as f:
        f.write(line + "\n")


def notify(summary: str, body: str) -> None:
    """Shows a critical notification with the devicelease icon. Never raises."""
    if os.environ.get("DEVICELEASE_NOTIFY") == "off":
        return
    command = shutil.which("notify-send")
    if command is None:
        _log(f"desktop: notify-send not found, not shown: {summary}")
        return
    env = dict(os.environ)
    # A hook may run without the session bus address. The user's bus has a fixed path.
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{os.getuid()}/bus")
    try:
        result = subprocess.run(
            [command, "--app-name=devicelease", "--icon=devicelease",
             "--hint=string:desktop-entry:devicelease", "--urgency=critical", summary, body],
            env=env, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as e:
        _log(f"desktop: notify-send failed for {summary}: {e}")
        return
    if result.returncode != 0:
        _log(f"desktop: notify-send exited {result.returncode} for {summary}: "
             f"{result.stderr.strip()}")
