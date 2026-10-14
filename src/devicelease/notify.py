"""Tells Claude Code sessions that the queue handed them a device.

Claude Code has no command that posts into another session, but a session can send one with its
SendMessage tool. So a headless `claude -p` run sends it. Sending takes a few seconds, and a
hand-off can happen inside any devicelease command or hook, so a detached watcher process does
the sending. The watcher also sweeps while Claude Code sessions wait, because nothing else runs
when a claim goes stale.
"""

import fcntl
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from devicelease import claude_sessions, cleanup, paths
from devicelease.claims import Claim, format_time, now, write_temp

SENT = "sent"
GONE = "gone"
FAILED = "failed"

SEND_TIMEOUT = 60
RETRIES = 2

BYPASS = "bypassPermissions"
"""A session in this mode holds messages from a sender in any other mode for the user's approval.
Senders in every other mode reach every other mode directly."""


def remember_mode(session_id: str | None, mode: str | None) -> None:
    """Records a session's permission mode, from the hook input, for sending to it later."""
    if not session_id or not mode:
        return
    path = paths.sessions_dir() / f"{session_id}.json"
    try:
        if json.loads(path.read_text()).get("permissionMode") == mode:
            return
    except (OSError, ValueError, AttributeError):
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    os.replace(write_temp(path.parent, {"permissionMode": mode}), path)


def recorded_mode(session_id: str) -> str | None:
    try:
        return json.loads((paths.sessions_dir() / f"{session_id}.json").read_text()) \
            .get("permissionMode")
    except (OSError, ValueError, AttributeError):
        return None


def _log(line: str) -> None:
    log = paths.state_dir() / "logs" / "notify.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    cleanup.rotate(log)
    with open(log, "a") as f:
        f.write(f"{format_time(now())} {line}\n")


def _claude_command() -> list[str] | None:
    override = os.environ.get("DEVICELEASE_CLAUDE")
    if override:
        return shlex.split(override)
    found = shutil.which("claude")
    return [found] if found else None


def message_text(claim: Claim) -> str:
    serial = claim.serial or claim.device
    return (f"devicelease: {claim.device} is free and now yours (serial {serial}). The queue "
            f"claimed it for you. Release it with `devicelease release` when done.")


def _prompt(address: str, text: str) -> str:
    return (f"Call the SendMessage tool exactly once, with `to` set to exactly {address} and "
            f"`message` set to exactly the text between the two marker lines below. "
            f"Do nothing else.\n<<<\n{text}\n>>>\n")


def _delivered(output: str, address: str) -> bool:
    """Whether the stream-json output shows a SendMessage to the address that succeeded."""
    sends = set()
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        content = (event.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") == "tool_use" and block.get("name") == "SendMessage" \
                    and (block.get("input") or {}).get("to") == address:
                sends.add(block.get("id"))
            elif block.get("type") == "tool_result" and block.get("tool_use_id") in sends:
                parts = block.get("content")
                texts = [p.get("text", "") for p in parts if isinstance(p, dict)] \
                    if isinstance(parts, list) else [str(parts)]
                for text in texts:
                    try:
                        if json.loads(text).get("success") is True:
                            return True
                    except (ValueError, AttributeError):
                        continue
    return False


def send(session_id: str, text: str) -> str:
    """Sends the text to a running Claude Code session. Returns SENT, GONE when the session is
    not running, or FAILED."""
    session = claude_sessions.find(session_id)
    if session is None or not session.alive:
        _log(f"session {session_id} is not running")
        return GONE
    if session.address is None:
        _log(f"session {session_id} has no messaging socket")
        return FAILED
    command = _claude_command()
    if command is None:
        _log("claude not found; set DEVICELEASE_CLAUDE")
        return FAILED
    # SendMessage is the only tool: --tools removes the other built-in ones and
    # --strict-mcp-config every MCP server, which matters most in bypass mode.
    mode = BYPASS if recorded_mode(session_id) == BYPASS else "manual"
    command += ["-p", "--model", "haiku", "--permission-mode", mode,
                "--tools", "SendMessage", "--allowedTools", "SendMessage", "--strict-mcp-config",
                "--output-format", "stream-json", "--verbose", "--no-session-persistence"]
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
    backoff = float(os.environ.get("DEVICELEASE_NOTIFY_BACKOFF_SECONDS", "5"))
    for attempt in range(RETRIES + 1):
        if attempt:
            time.sleep(backoff * attempt)
        try:
            result = subprocess.run(command, input=_prompt(session.address, text), env=env,
                                    cwd=paths.state_dir(), capture_output=True, text=True,
                                    timeout=SEND_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as e:
            _log(f"send to {session.address} failed: {e}")
            continue
        if _delivered(result.stdout, session.address):
            _log(f"sent to {session.address} ({session.name}): {text}")
            return SENT
        error = result.stderr.strip().splitlines()[-1:] or [f"exit {result.returncode}"]
        _log(f"send to {session.address} did not succeed: {error[0]}")
    return FAILED


def deliver(lease) -> None:
    """Sends a message for every hand-off that waits for one."""
    for claim in lease.store.all():
        if not claim.notify_session:
            continue
        outcome = send(claim.notify_session, message_text(claim))
        with lease.store.lock():
            current = lease.store.read(claim.device)
            if not current or current.queue_entry != claim.queue_entry:
                continue
            if outcome == GONE:
                lease.store.delete(claim.device)
                continue
            current.notify_session = None
            if outcome == SENT:
                current.handoff_pending = False
            lease.store.write(current)
    lease.hand_off()


def _has_work(lease) -> bool:
    from devicelease.queue import MESSAGE
    return (any(e.mode == MESSAGE for e in lease.queue.all())
            or any(c.notify_session for c in lease.store.all()))


def watch(once: bool = False) -> int:
    """Sweeps, hands off and delivers until no Claude Code session waits. One instance at most."""
    from devicelease.lease import Lease

    poll = float(os.environ.get("DEVICELEASE_POLL_SECONDS", "10"))
    lock_path = paths.queue_dir() / ".watcher.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        with open(lock_path, "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 0
            while True:
                lease = Lease.create("devicelease-watcher", cwd=str(paths.state_dir()))
                lease.sweep()
                deliver(lease)
                if once or not _has_work(lease):
                    break
                time.sleep(poll)
        # Work that arrived while this instance held the lock and decided to stop would find
        # the lock taken and leave it, so check once more after letting go.
        if once or not _has_work(Lease.create("devicelease-watcher", cwd=str(paths.state_dir()))):
            return 0


def spawn() -> None:
    """Starts a detached watcher. It exits right away when one is already running."""
    if os.environ.get("DEVICELEASE_WATCHER") == "off":
        return
    log = paths.state_dir() / "logs" / "watcher.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    cleanup.rotate(log)
    src = str(Path(__file__).resolve().parents[1])
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [src, env.get("PYTHONPATH")]))
    with open(log, "a") as out:
        subprocess.Popen([sys.executable, "-m", "devicelease", "watch"], cwd=paths.state_dir(),
                         env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                         start_new_session=True)
