"""Claude Code hook handlers. Each reads the hook's JSON from stdin.

A hook that fails must not block the session's tools, so every handler fails open: errors go to
stderr and the tool call proceeds.
"""

import json
import os
import re
import sys

from devicelease.bashparse import EXPLICIT, IMPLICIT, Target, device_targets
from devicelease.claims import Claim
from devicelease.lease import Lease, LeaseError

GUIDE = ("Run `devicelease list`, claim a device with `devicelease claim <id>` "
         "(or `devicelease claim --any`), then target it: `adb -s <serial>`, "
         "`android ... --device=<serial>`, or `devicelease exec -- ./gradlew ...`. "
         "`devicelease help` explains it all.")


def _lease(data: dict) -> Lease:
    env = {"CLAUDE_CODE_SESSION_ID": data["session_id"]} if data.get("session_id") else {}
    return Lease.create(env=env, cwd=data.get("cwd"))


def _deny(reason: str) -> None:
    json.dump({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}, sys.stdout)


def handoff_notice(claim: Claim) -> str:
    serial = f" (serial {claim.serial})" if claim.serial and claim.serial != claim.device else ""
    return (f"{claim.device}{serial} was handed to you from the queue. "
            f"Release it with `devicelease release` when done.")


def _valid_claims(lease: Lease) -> list[Claim]:
    return [c for c in lease.mine() if not lease.stale(c)]


def _shorten(command: str, limit: int = 120) -> str:
    command = " ".join(command.split())
    return command if len(command) <= limit else command[:limit - 3] + "..."


def _implicit_hint(lease: Lease, target: Target, mine: list[Claim]) -> str:
    if len(mine) != 1:
        return GUIDE
    try:
        serial = lease.live_serial(mine[0])
    except LeaseError as e:
        return str(e)
    return {
        "adb": f"Use `adb -s {serial} ...`.",
        "android": f"Add `--device={serial}`.",
        "scrcpy": f"Add `-s {serial}`.",
        "fastboot": f"Add `-s {serial}`.",
    }.get(target.tool, f"Run it as `devicelease exec -- {_shorten(target.command, 60)}`.")


def _judge(lease: Lease, target: Target) -> str | None:
    """Returns why the session may not run this device command, or None if it may."""
    command = f"`{_shorten(target.command)}`"
    if target.mode == EXPLICIT:
        try:
            lease.check(target.device)
            return None
        except LeaseError as e:
            return f"{command}: {e}"
    mine = _valid_claims(lease)
    if target.mode != IMPLICIT:
        if len(mine) == 1:
            return None
        held = f"you hold {len(mine)} devices" if mine else "you hold no device"
        return f"{command}: cannot tell which device this uses, and {held}. {GUIDE}"
    online = [d for d in lease.devices if d.online and (target.kind is None or d.kind == target.kind)]
    if len(online) == 1 and any(c.device == online[0].id for c in mine):
        return None
    return (f"{command} does not name a device, so it may use one you have not claimed. "
            f"{_implicit_hint(lease, target, mine)}")


def pre_bash(data: dict) -> None:
    command = (data.get("tool_input") or {}).get("command") or ""
    env = {"ANDROID_SERIAL": os.environ["ANDROID_SERIAL"]} if os.environ.get("ANDROID_SERIAL") else {}
    targets = device_targets(command, env)
    if not targets:
        return
    lease = _lease(data)
    problems = [p for p in (_judge(lease, t) for t in targets) if p]
    if problems:
        if re.search(r"devicelease\s+claim\b", command):
            problems.append("This check runs before any part of the command, so a claim in the "
                            "same command does not count yet. Run `devicelease claim` on its own "
                            "first.")
        _deny("devicelease: " + "\n".join(dict.fromkeys(problems)))


def _t3_target(data: dict) -> str | None:
    """The deviceId of a T3 Code device tool call, or None when it does not concern a local
    Android device."""
    tool_input = data.get("tool_input") or {}
    if tool_input.get("hostId") not in (None, "local") or tool_input.get("platform") == "ios":
        return None
    return tool_input.get("deviceId") or ""


def pre_device_open(data: dict) -> None:
    device_id = _t3_target(data)
    if device_id is None:
        return
    lease = _lease(data)
    if not device_id:
        mine = [c.device for c in _valid_claims(lease)]
        suggestion = (f"Pass deviceId \"{mine[0]}\", the device you hold." if len(mine) == 1
                      else "Pass the deviceId of a device you claimed, or of a free device to "
                           "claim it now. " + GUIDE)
        _deny(f"devicelease: device_open needs a deviceId, so the device can be claimed. "
              f"{suggestion}")
        return
    try:
        lease.claim(device_id, t3_device=device_id)
    except LeaseError as e:
        message = str(e).rstrip(".") + "."
        if "devicelease list" not in message:
            message += " Run `devicelease list` to see free devices."
        _deny(f"devicelease: {message}")


def post_device_close(data: dict) -> None:
    device_id = _t3_target(data)
    if device_id is None:
        return
    lease = _lease(data)
    opened = [c for c in lease.mine() if c.t3_device_id]
    if device_id:
        opened = [c for c in opened if device_id in (c.t3_device_id, c.device, c.serial)]
    if len(opened) == 1:
        lease.release(opened[0].device)


def heartbeat(data: dict) -> None:
    lease = _lease(data)
    if lease.mine():
        lease.heartbeat()
    notices = [handoff_notice(c) for c in lease.take_handoff_notices()]
    if notices:
        json.dump({"hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": "devicelease: " + " ".join(notices),
        }}, sys.stdout)


def session_end(data: dict) -> None:
    lease = _lease(data)
    # T3 Code restarts the `claude` process between turns of one thread, so a session ending does
    # not mean the thread is done. Its claims end when the thread is settled instead.
    if lease.owner.t3_thread:
        return
    lease.unqueue(everything=True)
    lease.release(everything=True)


HANDLERS = {
    "pre-bash": pre_bash,
    "pre-device-open": pre_device_open,
    "post-device-close": post_device_close,
    "heartbeat": heartbeat,
    "session-end": session_end,
}


def run(name: str) -> int:
    try:
        data = json.load(sys.stdin)
        HANDLERS[name](data)
    except Exception as e:  # noqa: BLE001 - a broken hook must not block the session
        print(f"devicelease hook {name} failed, allowing the tool call: {e!r}", file=sys.stderr)
    return 0
