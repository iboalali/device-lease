import argparse
import json
import os
import sys

from devicelease import paths
from devicelease.claims import Claim
from devicelease.devices import EMULATOR, MISSING, PHYSICAL, Device, DiscoveryError
from devicelease.hook import HANDLERS, run as run_hook
from devicelease.lease import Lease, LeaseError, age, describe_holder, short_owner
from devicelease.policy import PolicyError


def _print_json(value) -> None:
    print(json.dumps(value, indent=2))


def _table(rows: list[list[str]]) -> None:
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]) - 1)]
    for row in rows:
        cells = [cell.ljust(width) for cell, width in zip(row, widths)] + [row[-1]]
        print("  ".join(cells).rstrip())


def _owner_label(lease: Lease, owner: str) -> str:
    label = short_owner(owner)
    return f"{label} (you)" if owner in lease.owner.ids else label


def cmd_list(lease: Lease, args) -> int:
    lease.sweep()
    claims = {c.device: c for c in lease.store.all()}
    devices = list(lease.devices)
    known = {d.id for d in devices}
    for claim in claims.values():
        if claim.device not in known:
            devices.append(Device(claim.device, claim.kind, claim.serial, MISSING, claim.device))
    devices.sort(key=lambda d: (d.state != "online", d.kind != EMULATOR, d.id))

    entries = []
    for device in devices:
        claim = claims.get(device.id)
        reason = lease.not_allowed(device)
        if claim and lease.is_mine(claim):
            status, text = "yours", f"yours (since {age(claim.created_at)})"
        elif claim:
            status, text = "taken", f"taken by {describe_holder(claim)}"
        elif reason:
            status, text = "not-allowed", f"not allowed here: {reason}"
        else:
            status, text = "free", "free"
        entries.append((device, claim, status, reason, text))

    if args.json:
        _print_json([
            {**device.to_dict(), "lease": status, "reason": reason,
             "claim": claim.to_dict() if claim else None}
            for device, claim, status, reason, _ in entries
        ])
        return 0
    if not entries:
        print("No devices. Start an emulator from the Android Studio Device Manager, "
              "or connect a device.")
        return 0
    rows = [["DEVICE", "KIND", "API", "STATE", "NAME", "LEASE"]]
    for device, _, _, _, text in entries:
        rows.append([device.id, device.kind, str(device.api or "-"), device.state, device.name, text])
    _table(rows)
    return 0


def _print_claim(lease: Lease, claim: Claim, verb: str) -> None:
    print(f"{verb} {claim.device} ({claim.kind}) for {_owner_label(lease, claim.owner)} "
          f"in {paths.display_path(claim.project)}")
    if claim.kind == EMULATOR:
        try:
            serial = lease.live_serial(claim)
            print(f"serial: {serial}")
        except LeaseError:
            print(f"{claim.device} is not running; boot it with "
                  f"`devicelease claim {claim.device} --boot`")
    else:
        print(f"serial: {claim.device}")


def cmd_claim(lease: Lease, args) -> int:
    if args.any == bool(args.device):
        raise LeaseError("name a device or pass --any")
    if args.any:
        if args.boot:
            raise LeaseError("--boot needs a device name")
        kind = PHYSICAL if args.physical else EMULATOR if args.emulator else None
        claim, already = lease.claim_any(kind, args.min_api, args.note), False
    else:
        if args.physical or args.emulator or args.min_api is not None:
            raise LeaseError("--physical, --emulator and --min-api only apply with --any")
        claim, already = lease.claim(args.device, args.note, args.boot)
    if args.json:
        _print_json({**claim.to_dict(), "alreadyHeld": already})
    else:
        _print_claim(lease, claim, "Already holding" if already else "Claimed")
    return 0


def cmd_release(lease: Lease, args) -> int:
    if args.force:
        if args.device or args.all:
            raise LeaseError("--force takes the device on its own")
        claim = lease.find_any_claim(args.force)
        if claim is None:
            raise LeaseError(f"nobody holds {args.force}")
        if not lease.is_mine(claim) and not args.yes:
            if not sys.stdin.isatty():
                raise LeaseError("--force asks for confirmation; pass --yes when not on a terminal")
            answer = input(f"Release {claim.device}, held by {describe_holder(claim)}? [y/N] ")
            if answer.strip().lower() not in ("y", "yes"):
                print("Nothing released.")
                return 1
        released = lease.force_release(claim)
    else:
        if args.device and args.all:
            raise LeaseError("pass a device or --all, not both")
        released = lease.release(args.device, args.all)
    if args.json:
        _print_json([c.to_dict() for c in released])
    elif released:
        for claim in released:
            print(f"Released {claim.device}")
    else:
        print("Nothing to release.")
    return 0


def cmd_status(lease: Lease, args) -> int:
    claims = lease.store.all()
    online = lease.online_ids() if any(c.human for c in claims) else None
    entries = [(c, lease.stale(c, online)) for c in claims]
    if args.json:
        _print_json([{**c.to_dict(), "stale": reason} for c, reason in entries])
        return 0
    if not entries:
        print("No claims.")
        return 0
    rows = [["DEVICE", "OWNER", "PROJECT", "SINCE", "HEARTBEAT", "STATE"]]
    for claim, reason in entries:
        heartbeat = "-" if claim.human else f"{age(claim.heartbeat_at)} ago"
        rows.append([
            claim.device, _owner_label(lease, claim.owner), paths.display_path(claim.project),
            age(claim.created_at), heartbeat, f"stale: {reason}" if reason else "valid",
        ])
    _table(rows)
    return 0


def cmd_heartbeat(lease: Lease, args) -> int:
    refreshed = lease.heartbeat()
    if args.json:
        _print_json([c.to_dict() for c in refreshed])
    return 0


def cmd_whoami(lease: Lease, args) -> int:
    try:
        devices = lease.devices
        error = None
    except DiscoveryError as e:
        devices, error = [], str(e)
    allowed = [d.id for d in devices if not lease.not_allowed(d)]
    t3_project = lease.t3_project
    key, _ = lease.policy.project_rules(lease.project.root, t3_project[0] if t3_project else None)
    info = {
        "owner": lease.owner.id,
        "ownerPid": lease.owner.pid,
        "t3Thread": lease.owner.t3_thread,
        "t3Project": t3_project[0] if t3_project else None,
        "t3ProjectTitle": t3_project[1] if t3_project else None,
        "project": lease.project.root,
        "checkout": lease.project.checkout,
        "policy": lease.policy.source,
        "policyProject": key,
        "repoPolicy": lease.policy.repo_source,
        "expiryMinutes": lease.policy.expiry_minutes,
        "allowed": allowed,
        "claims": [c.device for c in lease.mine()],
        "discoveryError": error,
    }
    if args.json:
        _print_json(info)
        return 0
    owner = info["owner"] + (f" (pid {info['ownerPid']})" if info["ownerPid"] else "")
    print(f"owner:    {owner}")
    print(f"project:  {paths.display_path(info['project'])}")
    if info["checkout"] != info["project"]:
        print(f"checkout: {paths.display_path(info['checkout'])}")
    if t3_project:
        print(f"t3:       project {t3_project[1]} (policy key t3:{t3_project[0]})")
    print(f"policy:   {paths.display_path(info['policy']) if info['policy'] else 'none (allow all)'}"
          + (f", project entry {key}" if key else ""))
    if info["repoPolicy"]:
        print(f"repo:     {paths.display_path(info['repoPolicy'])}")
    print(f"expiry:   {info['expiryMinutes']:g} minutes")
    print(f"allowed:  {', '.join(allowed) or 'none'}")
    print(f"claims:   {', '.join(info['claims']) or 'none'}")
    if error:
        print(f"warning:  {error}")
    return 0


def cmd_serial(lease: Lease, args) -> int:
    print(lease.live_serial(lease.single_claim()))
    return 0


def cmd_check(lease: Lease, args) -> int:
    claim = lease.check(args.target)
    if args.json:
        _print_json(claim.to_dict())
    return 0


def cmd_exec(lease: Lease, args) -> int:
    claim = lease.single_claim(args.device)
    serial = lease.live_serial(claim)
    lease.check(claim.device)
    env = dict(os.environ, ANDROID_SERIAL=serial)
    try:
        os.execvpe(args.cmd[0], args.cmd, env)
    except OSError as e:
        raise LeaseError(f"cannot run {args.cmd[0]}: {e}") from e


def cmd_gc(lease: Lease, args) -> int:
    removed = lease.sweep()
    if args.json:
        _print_json([{**c.to_dict(), "reason": reason} for c, reason in removed])
    elif removed:
        for claim, reason in removed:
            print(f"Removed {claim.device} from {short_owner(claim.owner)}: {reason}")
    else:
        print("No stale claims.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--owner", default=argparse.SUPPRESS,
                        help="act as this owner; `me` is a human claim that holds until the "
                             "device disconnects or the emulator closes")
    common.add_argument("--json", action="store_true", help="print JSON")

    parser = argparse.ArgumentParser(
        prog="devicelease", description="Shared Android device claims for agent sessions.")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def add(name, handler, help_text):
        p = sub.add_parser(name, parents=[common], help=help_text, description=help_text)
        p.set_defaults(handler=handler)
        return p

    add("list", cmd_list, "list every known device and who holds it")

    p = add("claim", cmd_claim, "claim a device for this session")
    p.add_argument("device", nargs="?", help="device ID (AVD name or adb serial)")
    p.add_argument("--any", action="store_true",
                   help="claim the first free allowed device, emulators first")
    p.add_argument("--physical", action="store_true", help="with --any: physical devices only")
    p.add_argument("--emulator", action="store_true", help="with --any: emulators only")
    p.add_argument("--min-api", type=int, metavar="N", help="with --any: API level N or higher")
    p.add_argument("--boot", action="store_true", help="start the emulator after claiming it")
    p.add_argument("--note", help="free text stored with the claim")

    p = add("release", cmd_release, "release this session's claims")
    p.add_argument("device", nargs="?")
    p.add_argument("--all", action="store_true", help="release every claim of this owner")
    p.add_argument("--force", metavar="DEVICE", help="release another owner's claim")
    p.add_argument("--yes", action="store_true", help="skip the --force confirmation")

    add("status", cmd_status, "show all claims, including stale ones")
    add("heartbeat", cmd_heartbeat, "refresh this owner's claims")
    add("whoami", cmd_whoami, "show the resolved owner, project and allowed devices")
    add("serial", cmd_serial, "print the serial of this owner's single claimed device")

    p = add("check", cmd_check, "exit 0 if this owner holds the device, otherwise exit 1")
    p.add_argument("target", help="device ID or adb serial")

    p = add("exec", cmd_exec, "run a command with ANDROID_SERIAL set to the claimed device")
    p.add_argument("device", nargs="?")

    add("gc", cmd_gc, "remove stale claims")

    p = sub.add_parser("hook", help="Claude Code hook handler; reads the hook JSON from stdin")
    p.add_argument("name", choices=sorted(HANDLERS))
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    command: list[str] = []
    if "--" in argv:
        split = argv.index("--")
        argv, command = argv[:split], argv[split + 1:]
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "exec":
        if not command:
            parser.error("exec needs a command after --")
        args.cmd = command
    elif command:
        parser.error("unexpected arguments after --")
    if args.command == "hook":
        return run_hook(args.name)

    try:
        lease = Lease.create(getattr(args, "owner", None))
        return args.handler(lease, args)
    except (LeaseError, DiscoveryError, PolicyError) as e:
        if args.json:
            _print_json({"error": str(e)})
        else:
            print(f"devicelease: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
