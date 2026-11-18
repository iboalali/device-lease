"""Claude Code hook handlers. Each reads the hook's JSON from stdin.

A hook that fails must not block the session's tools, so every handler fails open: errors go to
stderr and the tool call proceeds.
"""

import json
import os
import re
import sys

from devicelease import notify, paths, wipe
from devicelease.bashparse import EXPLICIT, IMPLICIT, INVENTORY, USER_REQUESTED, Target, \
    device_targets
from devicelease.claims import Claim
from devicelease.devices import EMULATOR, STOPPED, DiscoveryError, find
from devicelease.lease import Lease, LeaseError

GUIDE = ("Run `devicelease list`, claim a device with `devicelease claim <id>` "
         "(or `devicelease claim --any`), then target it: `adb -s <serial>`, "
         "`android ... --device=<serial>`, or `devicelease exec -- ./gradlew ...`. "
         "`devicelease help` explains it all.")


USE_EXISTING = ("Use an existing one: `devicelease claim --any` takes a free emulator, and "
                "`devicelease queue <id> --wait` waits for a busy one (`devicelease list` shows "
                "them all).")

INVENTORY_DENIAL = ("Agents don't create, delete or change emulators or system images on this "
                    "machine, and approval will most likely not come. " + USE_EXISTING + " Only "
                    "if the user asked you for this change in this conversation, run the same "
                    f"command again with `{USER_REQUESTED}=1` in front. That asks the user for "
                    "approval.")


HUB_EMULATOR_ARGS = ["-no-window", "-no-audio"]
"""How the hook boots an emulator for T3 Code's device panel, which shows it without a window."""


def _lease(data: dict) -> Lease:
    env = {"CLAUDE_CODE_SESSION_ID": data["session_id"]} if data.get("session_id") else {}
    return Lease.create(env=env, cwd=data.get("cwd"))


def _decide(decision: str, reason: str) -> None:
    json.dump({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
        "permissionDecisionReason": reason,
    }}, sys.stdout)


def _deny(reason: str) -> None:
    _decide("deny", reason)


def guarded_folders() -> tuple[str, ...]:
    """The folders whose files make up the emulators and system images."""
    return str(paths.avd_home()), str(paths.sdk_root() / "system-images")


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


def _dynamic_hint(lease: Lease, mine: list[Claim]) -> str:
    if len(mine) != 1:
        return GUIDE
    try:
        serial = lease.live_serial(mine[0])
    except LeaseError as e:
        return str(e)
    return (f"Write the serial out (`adb -s {serial}`), set the variable earlier in the same "
            f"command (`S={serial}; adb -s \"$S\" ...`), or use `$(devicelease serial)`.")


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
    if target.self_serial:
        if len(mine) == 1:
            return None
        held = f"you hold {len(mine)} devices" if mine else "you hold no device"
        return f"{command}: cannot tell which device this uses, and {held}. {GUIDE}"
    online = [d for d in lease.devices if d.online and (target.kind is None or d.kind == target.kind)]
    if len(online) == 1 and any(c.device == online[0].id for c in mine):
        return None
    if target.mode != IMPLICIT:
        return (f"{command} names its device through `{target.device}`, which devicelease cannot "
                f"read, so it may use one you have not claimed. {_dynamic_hint(lease, mine)}")
    return (f"{command} does not name a device, so it may use one you have not claimed. "
            f"{_implicit_hint(lease, target, mine)}")


def _gpu_problem(target: Target) -> str | None:
    """Emulators render with the host GPU. Without `-gpu` the AVD's own setting applies, which
    is usually `auto` and falls back to software rendering when the emulator runs headless."""
    if not target.boots or target.gpu:
        return None
    avd = target.device or "<avd>"
    return (f"`{_shorten(target.command)}` boots an emulator without `-gpu host`, so it may render "
            f"in software. Boot it with `devicelease claim {avd} --boot` (add "
            f"`-- -no-window -no-audio` for no window), or pass `-gpu` yourself.")


def _pool_problem(lease: Lease, target: Target) -> str | None:
    """A pool emulator only boots through devicelease, which wipes it when its last agent is done."""
    if not target.boots or not target.device:
        return None
    try:
        device = find(lease.devices, target.device)
    except DiscoveryError:
        device = None
    name = device.name if device else None
    if not lease.policy.in_pool(target.device, name):
        return None
    return (f"`{_shorten(target.command)}` boots {target.device}, a pool emulator, outside "
            f"devicelease, which would skip its wipe. Boot it with "
            f"`devicelease claim {target.device} --boot`.")


def pre_bash(data: dict) -> None:
    command = (data.get("tool_input") or {}).get("command") or ""
    env = {"ANDROID_SERIAL": os.environ["ANDROID_SERIAL"]} if os.environ.get("ANDROID_SERIAL") else {}
    targets = device_targets(command, env, cwd=data.get("cwd"), guarded=guarded_folders())
    if not targets:
        return
    changes = [t for t in targets if t.mode == INVENTORY]
    targets = [t for t in targets if t.mode != INVENTORY]
    lease = _lease(data)
    problems = [p for t in targets
                for p in (_judge(lease, t), _gpu_problem(t), _pool_problem(lease, t)) if p]
    unrequested = [t for t in changes if not t.user_requested]
    if unrequested:
        listed = "; ".join(dict.fromkeys(t.device for t in unrequested))
        problems.append(f"This command would {listed}. {INVENTORY_DENIAL}")
    if not problems and changes:
        listed = "\n".join(f"- {c}" for c in dict.fromkeys(t.device for t in changes))
        _decide("ask", f"devicelease: an agent in {paths.display_path(lease.project.root)} asks "
                       f"to change the emulators on this machine:\n{listed}\nApprove only if "
                       f"you asked for this.")
        return
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
    device = find(lease.devices, device_id)
    # T3 Code boots a stopped emulator with `-gpu auto`, which renders in software. Booting it
    # here with the host GPU first lets device_open attach to the running emulator instead.
    # A pool emulator that is still stopping for its wipe is booted again here, wiped.
    boot = device is not None and device.kind == EMULATOR and (
        device.state == STOPPED or wipe.pending(device.id))
    try:
        claim, _ = lease.claim(device_id, t3_device=device_id, boot=boot,
                               emulator_args=HUB_EMULATOR_ARGS if boot else ())
        if boot and claim.serial:
            lease.claim(device_id, t3_device=claim.serial)
    except (LeaseError, DiscoveryError) as e:
        message = str(e).rstrip(".") + "."
        if "devicelease list" not in message:
            message += " Run `devicelease list` to see free devices."
        _deny(f"devicelease: {message}")
        return
    if boot and claim.serial:
        # T3 Code lists a stopped emulator by its AVD name and a running one by its serial.
        json.dump({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": {**data["tool_input"], "deviceId": claim.serial},
        }}, sys.stdout)


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
    # T3 Code stops a thread's idle `claude` process after about 30 minutes and starts a new one
    # when the thread goes on, so a session ending does not mean the thread is done. Its claims
    # end when the thread is settled instead.
    if lease.owner.t3_thread:
        return
    lease.unqueue(everything=True)
    lease.release(everything=True)


def pre_write(data: dict) -> None:
    tool_input = data.get("tool_input") or {}
    path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not path:
        return
    full = os.path.realpath(os.path.join(data.get("cwd") or os.getcwd(), os.path.expanduser(path)))
    for root in map(os.path.realpath, guarded_folders()):
        if os.path.commonpath([full, root]) == root:
            _deny(f"devicelease: {paths.display_path(full)} belongs to an emulator or a system "
                  f"image. Agents don't create, delete or change emulators on this machine. "
                  f"{USE_EXISTING} If the user asked for this change, tell them it has to be "
                  f"made by hand.")
            return


HANDLERS = {
    "pre-bash": pre_bash,
    "pre-write": pre_write,
    "pre-device-open": pre_device_open,
    "post-device-close": post_device_close,
    "heartbeat": heartbeat,
    "session-end": session_end,
}


def run(name: str) -> int:
    try:
        data = json.load(sys.stdin)
        notify.remember_mode(data.get("session_id"), data.get("permission_mode"))
        HANDLERS[name](data)
    except Exception as e:  # noqa: BLE001 - a broken hook must not block the session
        print(f"devicelease hook {name} failed, allowing the tool call: {e!r}", file=sys.stderr)
    return 0
