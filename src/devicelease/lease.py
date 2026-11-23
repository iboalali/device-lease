import os
import time
from dataclasses import replace
from datetime import timedelta

from devicelease import cleanup, desktop, notify, paths, t3, wipe
from devicelease.claims import Claim, Store, now, parse_time, stale_reason
from devicelease.devices import (
    EMULATOR, PHYSICAL, STOPPED, Device, DiscoveryError, boot_emulator, discover, find,
)
from devicelease.owner import Owner, resolve_owner
from devicelease.policy import Policy, PolicyError, load_policy
from devicelease.project import Project, resolve_project
from devicelease import queue
from devicelease.queue import MESSAGE, WAIT, Entry, QueueStore, ended_reason, new_id, queue_args


HEADLESS = ["-no-window", "-no-audio"]
"""How an emulator boots for a T3 Code thread, whose device panel shows it without a window."""


class LeaseError(Exception):
    pass


class TakenError(LeaseError):
    """The device has a valid claim of another owner."""


def short_owner(owner: str) -> str:
    # Session and thread IDs are UUIDs; the first block is enough to tell them apart.
    prefix = "t3:" if owner.startswith("t3:") else ""
    rest = owner[len(prefix):]
    if len(rest) == 36 and rest.count("-") == 4:
        return prefix + rest[:8]
    return owner


def age(claim_time) -> str:
    minutes = int((now() - claim_time).total_seconds() // 60)
    if minutes < 1:
        return "<1m"
    if minutes < 60:
        return f"{minutes}m"
    if minutes < 24 * 60:
        return f"{minutes // 60}h{minutes % 60:02d}m"
    return f"{minutes // (24 * 60)}d"


def ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def describe_holder(claim: Claim) -> str:
    return (f"{short_owner(claim.owner)} ({paths.display_path(claim.project)}, "
            f"since {age(claim.created_at)})")


class Lease:
    def __init__(self, owner: Owner, project: Project, policy: Policy, store: Store,
                 queue: QueueStore):
        self.owner = owner
        self.project = project
        self.policy = policy
        self.store = store
        self.queue = queue
        self._devices: list[Device] | None = None
        self._t3_project: tuple[str, str] | None = None

    @classmethod
    def create(cls, owner: str | None = None, env=None, cwd: str | None = None) -> "Lease":
        project = resolve_project(cwd)
        return cls(resolve_owner(owner, env), project, load_policy(project),
                   Store(paths.claims_dir()), QueueStore(paths.queue_dir()))

    @property
    def expiry(self) -> timedelta:
        return timedelta(minutes=self.policy.expiry_minutes)

    @property
    def devices(self) -> list[Device]:
        if self._devices is None:
            self._devices = discover()
        return self._devices

    def online_ids(self) -> set[str] | None:
        try:
            return {d.id for d in self.devices if d.online}
        except DiscoveryError:
            return None

    def stale(self, claim: Claim, online: set[str] | None = None) -> str | None:
        if claim.human and online is None:
            online = self.online_ids()
        return stale_reason(claim, self.expiry, online)

    @property
    def t3_project(self) -> tuple[str, str] | None:
        """The T3 Code project ID and title of this owner's thread."""
        if self._t3_project is None and self.owner.t3_thread:
            self._t3_project = t3.project_for_thread(self.owner.t3_thread)
        return self._t3_project

    def not_allowed(self, device: Device) -> str | None:
        t3_project = self.t3_project[0] if self.t3_project else None
        return self.policy.check(self.project.root, device.id, device.name, t3_project)

    @staticmethod
    def _end_t3_sessions(claims: list[Claim]) -> None:
        for claim in claims:
            if claim.t3_thread and claim.t3_device_id:
                t3.close_agent_device(
                    claim.t3_thread,
                    [d for d in (claim.t3_device_id, claim.device, claim.serial) if d],
                    paths.state_dir() / "logs" / "agent-device.log",
                )

    def _ended(self, claims: list[Claim]) -> None:
        """Runs what follows the end of claims: T3 Code's agent-device sessions close, and pool
        emulators an agent held are stopped and marked for a wipe."""
        self._end_t3_sessions(claims)
        for claim in claims:
            if claim.kind != EMULATOR or claim.human or not self.in_pool(claim.device):
                continue
            wipe.mark_pending(claim.device, claim.owner, claim.project)
            device = self._find(claim.device)
            if device is not None and device.online:
                wipe.stop(device.serial)
                self._devices = None

    def _find(self, device_id: str) -> Device | None:
        try:
            return find(self.devices, device_id)
        except DiscoveryError:
            return None

    def in_pool(self, device_id: str) -> bool:
        device = self._find(device_id)
        return self.policy.in_pool(device_id, device.name if device else None)

    @staticmethod
    def in_rotation(device: Device) -> bool:
        """Whether the queue and `claim --any` may hand out the device: not stopping for a wipe,
        and not failed."""
        return device.kind != EMULATOR or not (wipe.pending(device.id) or wipe.failed(device.id))

    def failure_message(self, device_id: str, record: dict, detail: str | None = None) -> str:
        what, log = record.get("what", "boot"), paths.display_path(record.get("log", "?"))
        message = (f"{device_id} failed to {what}" + (f": {detail}" if detail else "") + ". "
                   f"Tell the user that {device_id} failed to {what}, with this log: {log}. "
                   f"Don't retry it. If another emulator can do the work, you may continue on "
                   f"one (`devicelease claim --any`, or `devicelease queue <other id> --wait`), "
                   f"and still tell the user about the failure.")
        home = self.policy.device_rules(device_id).home
        if home:
            message += (f" {device_id} is the emulator for {paths.display_path(home)}, so an "
                        f"emulator without its setup is no substitute for work that needs it.")
        return message

    def out_of_rotation_message(self, device_id: str, record: dict) -> str:
        try:
            since = age(parse_time(record["at"]))
        except (KeyError, ValueError):
            since = "?"
        return (f"{device_id} is out of rotation since {since} ago. The user has to fix it, then "
                f"boot it once with `devicelease claim {device_id} --boot --owner me`. "
                + self.failure_message(device_id, record))

    def _boot_failed(self, claim: Claim, what: str, detail: str) -> None:
        """Marks a pool emulator failed, releases it, tells the user and raises."""
        record = wipe.mark_failed(claim.device, what, self.owner.id, self.project.root)
        self._release([claim])
        desktop.notify(f"{claim.device} failed to {what}",
                       f"{short_owner(self.owner.id)} in {paths.display_path(self.project.root)}. "
                       f"Log: {paths.display_path(record['log'])}")
        raise LeaseError(self.failure_message(claim.device, record, detail))

    def is_mine(self, claim: Claim | Entry) -> bool:
        return claim.owner in self.owner.ids

    @property
    def messageable(self) -> bool:
        """Whether a SendMessage can reach this owner: a Claude Code session outside T3 Code."""
        return bool(self.owner.claude_session) and not self.owner.t3_thread

    def queue_hint(self, args: str, subject: str = "it") -> str:
        """How this owner waits for a device, worded for where it runs."""
        if self.messageable:
            return (f"queue for {subject} with `devicelease queue {args}`; "
                    f"a message arrives when a device is yours")
        command = f"`devicelease queue {args} --wait`"
        if self.owner.t3_thread:
            return (f"wait for {subject}: run {command} with Bash run_in_background and end "
                    f"your turn; you are woken when a device is yours")
        return f"wait for {subject} with {command}, which exits once a device is yours"

    def taken_message(self, claim: Claim) -> str:
        return (f"{claim.device} is taken by {describe_holder(claim)}. Claim a free device "
                f"(`devicelease list`, `devicelease claim --any`), or "
                f"{self.queue_hint(claim.device)}.")

    def sweep(self) -> list[tuple[Claim, str]]:
        """Removes stale claims and records which human claims have seen their device online."""
        online = self.online_ids()
        removed = []
        with self.store.lock():
            for claim in self.store.all():
                reason = stale_reason(claim, self.expiry, online)
                if reason:
                    self.store.delete(claim.device)
                    removed.append((claim, reason))
                elif claim.human and online is not None and claim.device in online \
                        and not claim.seen_online:
                    claim.seen_online = True
                    self.store.write(claim)
        self._ended([claim for claim, _ in removed])
        self.hand_off()
        if cleanup.due():
            self.clean()
        return removed

    def clean(self) -> list[tuple[str, str]]:
        try:
            running = {d.id for d in self.devices if d.online and d.kind == EMULATOR}
        except DiscoveryError:
            running = None
        return cleanup.run(running)

    def _entry_allows(self, entry: Entry, device: Device, policies: dict) -> bool:
        """Whether the waiter's own policy allows the device, rather than the caller's."""
        if entry.checkout not in policies:
            try:
                policies[entry.checkout] = load_policy(Project(entry.project, entry.checkout))
            except PolicyError:
                policies[entry.checkout] = None
        policy = policies[entry.checkout]
        return policy is not None and policy.check(
            entry.project, device.id, device.name, entry.t3_project) is None

    def hand_off(self) -> list[Claim]:
        """Claims free online devices for the oldest live queue entries that match them, and
        drops the entries that no longer wait."""
        if not any(self.queue.root.glob("*.json")):
            return []
        try:
            devices = sorted((d for d in self.devices if self._handable(d)),
                             key=lambda d: (d.kind != EMULATOR, not d.online, d.id))
        except DiscoveryError:
            return []
        online = {d.id for d in devices if d.online}
        given, replaced, policies = [], [], {}
        with self.store.lock():
            claims = {c.device: c for c in self.store.all()}
            free = []
            for device in devices:
                claim = claims.get(device.id)
                if claim and stale_reason(claim, self.expiry, online):
                    self.store.delete(device.id)
                    replaced.append(claim)
                    claim = None
                if claim is None:
                    free.append(device)
            entries = self.queue.all()
            live = [e for e in entries if not ended_reason(e)]
            # A home project's waiter goes first for its emulator, ahead of older waiters
            # from other projects.
            homes = {d.id: self.home(d) for d in free}
            home_waiting = {
                d.id for d in free if homes[d.id] and any(
                    self.at_home(e.project, homes[d.id]) and e.matches(d) for e in live)
            }
            for entry in entries:
                if ended_reason(entry):
                    self.queue.delete(entry.id)
                    continue
                if entry.target and wipe.failed(entry.target):
                    continue

                def fits(d: Device, entry=entry) -> bool:
                    if not entry.matches(d) or not self._entry_allows(entry, d, policies):
                        return False
                    if self.at_home(entry.project, homes[d.id]):
                        return True
                    if entry.target is None and homes[d.id]:
                        return False
                    return d.id not in home_waiting

                device = next((d for d in sorted(
                    free, key=lambda d: not self.at_home(entry.project, homes[d.id]))
                    if fits(d)), None)
                if device is None:
                    continue
                at = now()
                claim = Claim(
                    device=device.id, kind=device.kind, owner=entry.owner, project=entry.project,
                    created_at=at, heartbeat_at=at, serial=device.serial, pid=entry.pid,
                    pid_start=entry.pid_start, seen_online=True, t3_thread=entry.t3_thread,
                    note=entry.note, queue_entry=entry.id, handoff_pending=True,
                    notify_session=entry.claude_session if entry.mode == MESSAGE else None,
                )
                if self.store.create(claim):
                    self.queue.delete(entry.id)
                    free.remove(device)
                    given.append(claim)
        self._ended(replaced)
        if any(c.notify_session for c in given):
            notify.spawn()
        return given

    def home(self, device: Device) -> str | None:
        """The project an emulator is meant for, from the policy's devices.<id>.home."""
        return self.policy.device_rules(device.id, device.name).home

    @staticmethod
    def at_home(root: str, home: str | None) -> bool:
        return home is not None and (root == home or root.startswith(home + os.sep))

    def _for_any(self, device: Device, root: str) -> bool:
        """Whether `--any` from this project may get the device: an emulator with a home only
        goes to its home project that way. Other projects can still name it."""
        home = self.home(device)
        return home is None or self.at_home(root, home)

    def _handable(self, device: Device) -> bool:
        """Whether the queue may hand out the device: a running one, or a stopped pool emulator,
        which the waiter's side boots (and wipes when it waits for its wipe)."""
        if device.kind == EMULATOR and wipe.failed(device.id):
            return False
        if device.online:
            return True
        return (device.kind == EMULATOR and device.state == STOPPED
                and self.policy.in_pool(device.id, device.name))

    def boot_handed(self, claim: Claim) -> Claim:
        """Boots a pool emulator that came from the queue stopped, or stopping for its wipe, as
        the claim's owner. Returns the claim with its new serial. Raises LeaseError when the
        boot fails."""
        if claim.kind != EMULATOR or not self.in_pool(claim.device):
            return claim
        device = self._find(claim.device)
        if device is None or (device.online and not wipe.pending(device.id)):
            return claim
        lease = self if self.is_mine(claim) else Lease.create(claim.owner, cwd=claim.project)
        args = HEADLESS if claim.t3_thread else []
        return lease.claim(claim.device, boot=True, emulator_args=args)[0]

    def _wiped_on_end(self, claim: Claim, device: Device) -> bool:
        """Whether ending this claim stops the device for a wipe, so it cannot be handed out."""
        return (device.kind == EMULATOR and not claim.human
                and self.policy.in_pool(device.id, device.name))

    def _new_claim(self, device: Device, note: str | None) -> Claim:
        at = now()
        return Claim(
            device=device.id, kind=device.kind, owner=self.owner.id, project=self.project.root,
            created_at=at, heartbeat_at=at, serial=device.serial, pid=self.owner.pid,
            pid_start=self.owner.pid_start, seen_online=device.online,
            t3_thread=self.owner.t3_thread, note=note,
        )

    def _touch(self, claim: Claim) -> None:
        claim.owner = self.owner.id
        claim.t3_thread = self.owner.t3_thread
        claim.heartbeat_at = now()
        claim.pid = self.owner.pid
        claim.pid_start = self.owner.pid_start

    def claim(self, target: str, note: str | None = None, boot: bool = False,
              t3_device: str | None = None, snapshot: bool = True,
              emulator_args: list[str] = (), wipe_data: bool = False) -> tuple[Claim, bool]:
        """Claims a device. Returns the claim and whether this owner already held it.

        A pool emulator always cold boots, and a boot after an agent's claim ended wipes its
        user data and runs its afterWipe step. `wipe_data` (or `-wipe-data` among
        `emulator_args`) restarts a pool emulator with its data wiped.
        """
        device = find(self.devices, target)
        if device is None:
            raise LeaseError(f"unknown device {target}; run `devicelease list`")
        reason = self.not_allowed(device)
        if reason:
            raise LeaseError(f"{device.id} is not allowed here: {reason}")
        if boot and device.kind != EMULATOR:
            raise LeaseError(f"--boot only applies to emulators; {device.id} is a physical device")
        pool = device.kind == EMULATOR and self.policy.in_pool(device.id, device.name)
        if "-wipe-data" in emulator_args:
            if not pool:
                raise LeaseError(f"{device.id} is not a pool emulator, so its data is the user's "
                                 f"own; -wipe-data only applies to pool emulators")
            emulator_args = [a for a in emulator_args if a != "-wipe-data"]
            wipe_data = True
        if wipe_data and not pool:
            raise LeaseError(f"{device.id} is not a pool emulator; only pool emulators are wiped")
        failure = wipe.failed(device.id) if device.kind == EMULATOR else None
        if failure and not self.owner.human:
            raise LeaseError(self.out_of_rotation_message(device.id, failure))

        self.hand_off()
        online = self.online_ids()
        replaced = None
        with self.store.lock():
            existing = self.store.read(device.id)
            if existing and self.is_mine(existing):
                self._touch(existing)
                existing.t3_device_id = t3_device or existing.t3_device_id
                self.store.write(existing)
                claim, already = existing, True
            else:
                if existing:
                    if not stale_reason(existing, self.expiry, online):
                        raise TakenError(self.taken_message(existing))
                    self.store.delete(device.id)
                    replaced = existing
                claim, already = self._new_claim(device, note), False
                claim.t3_device_id = t3_device
                if not self.store.create(claim):
                    raise TakenError(f"{device.id} was just claimed by another session")
        if replaced:
            self._ended([replaced])

        if pool and device.online and (wipe_data or wipe.pending(device.id)):
            # Stopping for a wipe: after the last agent's claim, or because this one asked.
            wipe.stop(device.serial)
            if not wipe.wait_stopped(device.id):
                raise LeaseError(f"emulator {device.id} did not stop within "
                                 f"{wipe.STOP_TIMEOUT}s; try again shortly")
            device, self._devices = replace(device, state=STOPPED, serial=None), None
        if wipe_data:
            wipe.mark_pending(device.id, self.owner.id, self.project.root)
            boot = True

        if boot and device.state == STOPPED:
            wiping = pool and wipe.pending(device.id)
            if pool and not wipe.wait_stopped(device.id):
                raise LeaseError(f"the last emulator process of {device.id} did not exit within "
                                 f"{wipe.STOP_TIMEOUT}s; try again shortly")
            try:
                serial = boot_emulator(device.id, wipe.log_path(device.id),
                                       snapshot=snapshot and not pool, gpu_hint=not pool,
                                       extra_args=[*emulator_args,
                                                   *(["-wipe-data"] if wiping else [])])
            except DiscoveryError as e:
                if pool:
                    self._boot_failed(claim, "wipe and boot" if wiping else "boot", str(e))
                if not already:
                    self._release([claim])
                raise
            if wiping:
                rules = self.policy.device_rules(device.id, device.name)
                if rules.after_wipe:
                    code = wipe.run_after_wipe(rules.after_wipe, rules.home, serial, device.id)
                    if code != 0:
                        wipe.stop(serial)
                        self._boot_failed(claim, "run its afterWipe step",
                                          f"`{rules.after_wipe}` exited with {code}")
                wipe.clear_pending(device.id)
            if pool and self.owner.human:
                wipe.clear_failed(device.id)
            self._devices = None
            with self.store.lock():
                current = self.store.read(device.id)
                if current and self.is_mine(current):
                    current.serial = serial
                    current.seen_online = True
                    self.store.write(current)
                    claim = current
        return claim, already

    def claim_any(self, kind: str | None = None, min_api: int | None = None,
                  note: str | None = None) -> Claim:
        def matches(device: Device) -> bool:
            return ((kind is None or device.kind == kind)
                    and (min_api is None or (device.api or 0) >= min_api)
                    and not self.not_allowed(device)
                    and self._for_any(device, self.project.root))

        self.hand_off()
        candidates = [d for d in self.devices if d.online and matches(d) and self.in_rotation(d)]
        candidates.sort(key=lambda d: (self.home(d) is None, d.kind != EMULATOR, d.id))
        online = self.online_ids()
        replaced, claim = [], None
        with self.store.lock():
            for device in candidates:
                existing = self.store.read(device.id)
                if existing:
                    if self.is_mine(existing) or not stale_reason(existing, self.expiry, online):
                        continue
                    self.store.delete(device.id)
                    replaced.append(existing)
                    if self._wiped_on_end(existing, device):
                        continue
                new = self._new_claim(device, note)
                if self.store.create(new):
                    claim = new
                    break
        self._ended(replaced)
        if claim:
            return claim

        bootable = [
            d.id for d in self.devices
            if d.state == STOPPED and matches(d) and self.store.read(d.id) is None
            and not wipe.failed(d.id)
        ]
        message = "no free device matches. "
        if kind == PHYSICAL:
            message += "Connect a device."
        else:
            message += ("Start an emulator from the Android Studio Device Manager "
                        "(or `devicelease claim <avd> --boot`), or connect a device.")
            if bootable:
                message += f"\nEmulators you may boot: {', '.join(bootable)}"
        if any(self.store.read(d.id) for d in candidates):
            hint = self.queue_hint(queue_args(None, kind, min_api), "the next one to free up")
            message += f"\nOr {hint}."
        raise LeaseError(message)

    def mine(self) -> list[Claim]:
        return [c for c in self.store.all() if self.is_mine(c)]

    def _resolve_claim(self, target: str, claims: list[Claim]) -> Claim | None:
        for claim in claims:
            if claim.device == target:
                return claim
        for claim in claims:
            if claim.serial == target:
                return claim
        try:
            device = find(self.devices, target)
        except DiscoveryError:
            return None
        if device is None:
            return None
        return next((c for c in claims if c.device == device.id), None)

    def _release(self, claims: list[Claim]) -> list[Claim]:
        released = []
        with self.store.lock():
            for claim in claims:
                current = self.store.read(claim.device)
                if current and current.owner == claim.owner:
                    self.store.delete(claim.device)
                    released.append(current)
        self._ended(released)
        if released:
            self.hand_off()
        return released

    def release(self, target: str | None = None, everything: bool = False) -> list[Claim]:
        mine = self.mine()
        if everything:
            return self._release(mine)
        if target is None:
            if len(mine) != 1:
                held = ", ".join(c.device for c in mine) or "none"
                raise LeaseError(f"name the device to release, or pass --all (you hold: {held})")
            return self._release(mine)
        claim = self._resolve_claim(target, mine)
        if claim is None:
            other = self._resolve_claim(target, self.store.all())
            if other:
                raise LeaseError(f"{other.device} is held by {describe_holder(other)}; "
                                 f"use --force {other.device} to take it away")
            raise LeaseError(f"you do not hold {target}")
        return self._release([claim])

    def wipe(self, target: str | None = None, emulator_args: list[str] = ()) -> Claim:
        """Restarts a pool emulator this owner holds with its user data wiped."""
        claim = self.single_claim(target)
        if claim.kind != EMULATOR or not self.in_pool(claim.device):
            raise LeaseError(f"{claim.device} is not a pool emulator; only pool emulators are "
                             f"wiped (wipeAfterUse in the policy)")
        return self.claim(claim.device, boot=True, emulator_args=emulator_args,
                          wipe_data=True)[0]

    def find_any_claim(self, target: str) -> Claim | None:
        return self._resolve_claim(target, self.store.all())

    def force_release(self, claim: Claim) -> list[Claim]:
        return self._release([claim])

    def heartbeat(self) -> list[Claim]:
        refreshed = []
        with self.store.lock():
            for claim in self.mine():
                self._touch(claim)
                self.store.write(claim)
                refreshed.append(claim)
        return refreshed

    def check(self, target: str) -> Claim:
        """Returns this owner's valid claim on the target, or raises with what to do instead."""
        claim = self._resolve_claim(target, self.mine())
        if claim is None:
            other = self._resolve_claim(target, self.store.all())
            if other and not self.stale(other):
                raise TakenError(self.taken_message(other))
            raise LeaseError(f"you have not claimed {target}; "
                             f"run `devicelease list`, then `devicelease claim <id>`")
        reason = self.stale(claim)
        if reason:
            self._release([claim])
            raise LeaseError(f"your claim on {claim.device} expired ({reason}); "
                             f"claim it again with `devicelease claim {claim.device}`")
        with self.store.lock():
            current = self.store.read(claim.device)
            if current and self.is_mine(current):
                self._touch(current)
                self.store.write(current)
        return claim

    def single_claim(self, target: str | None = None) -> Claim:
        mine = self.mine()
        if target is not None:
            claim = self._resolve_claim(target, mine)
            if claim is None:
                raise LeaseError(f"you have not claimed {target}; run `devicelease claim {target}`")
            return claim
        if not mine:
            raise LeaseError("you have not claimed a device; "
                             "run `devicelease list`, then `devicelease claim <id>`")
        if len(mine) > 1:
            raise LeaseError(f"you hold several devices ({', '.join(c.device for c in mine)}); "
                             f"name one")
        return mine[0]

    def live_serial(self, claim: Claim) -> str:
        """The adb serial of a claimed device right now. An emulator's serial changes on boot."""
        if claim.kind == PHYSICAL:
            return claim.device
        device = find(self.devices, claim.device)
        if device is None or not device.online or device.serial is None:
            raise LeaseError(f"emulator {claim.device} is not running; "
                             f"start it with `devicelease claim {claim.device} --boot`")
        return device.serial

    def _queued_claim(self, entry_id: str) -> Claim | None:
        return next((c for c in self.mine() if c.queue_entry == entry_id), None)

    def enqueue(self, target: str | None, kind: str | None = None, min_api: int | None = None,
                wait: bool = False, note: str | None = None
                ) -> tuple[Claim | None, Entry | None, bool]:
        """Claims a matching device now if one is free. Otherwise adds this owner's queue entry,
        or refreshes it when it exists. Returns the claim or the entry when it has to wait, and
        whether this owner already held the claimed device."""
        if not wait and not self.messageable:
            where = "In T3 Code" if self.owner.t3_thread else "Outside a Claude Code session"
            raise LeaseError(f"{where} nothing can message you when the device is free; "
                             f"pass --wait and run the command in the background")
        if target:
            device = find(self.devices, target)
            if device is None:
                raise LeaseError(f"unknown device {target}; run `devicelease list`")
            reason = self.not_allowed(device)
            if reason:
                raise LeaseError(f"{device.id} is not allowed here: {reason}")
            target = device.id
            try:
                claim, already = self.claim(target, note)
                return claim, None, already
            except TakenError:
                pass
        else:
            try:
                return self.claim_any(kind, min_api, note), None, False
            except LeaseError:
                pass

        mode = WAIT if wait else MESSAGE
        with self.store.lock():
            entry = next((e for e in self.queue.all() if self.is_mine(e) and e.target == target
                          and e.kind == kind and e.min_api == min_api), None)
            if entry:
                entry.mode = mode
                entry.refreshed_at = queue.now()
                entry.note = note or entry.note
            else:
                at = queue.now()
                t3_project = self.t3_project
                entry = Entry(
                    id=new_id(), owner=self.owner.id, mode=mode, project=self.project.root,
                    checkout=self.project.checkout, created_at=at, refreshed_at=at,
                    target=target, kind=kind, min_api=min_api, pid=self.owner.pid,
                    pid_start=self.owner.pid_start, claude_session=self.owner.claude_session,
                    t3_thread=self.owner.t3_thread,
                    t3_project=t3_project[0] if t3_project else None, note=note,
                )
            self.queue.write(entry)
        self.hand_off()
        claim = self._queued_claim(entry.id)
        if claim:
            return claim, None, False
        if mode == MESSAGE:
            notify.spawn()
        return None, entry, False

    def position(self, entry: Entry) -> int:
        """The entry's place in line, 1 for the next to get a device it waits for."""
        try:
            devices = [d for d in self.devices if entry.matches(d)]
        except DiscoveryError:
            devices = []
        ahead = [
            e for e in self.queue.all()
            if (e.created_at, e.id) < (entry.created_at, entry.id)
            and (not devices or any(e.matches(d) for d in devices))
        ]
        return len(ahead) + 1

    def wait(self, entry: Entry, timeout: timedelta, poll: float) -> tuple[str, Claim | None]:
        """Polls until the queue hands this entry a device. Returns ("yours", claim),
        ("timeout", None) while still queued, or ("ended", None) when the entry is gone."""
        deadline = time.monotonic() + timeout.total_seconds()
        while True:
            claim = self._queued_claim(entry.id)
            if claim:
                return "yours", claim
            with self.store.lock():
                current = self.queue.read(entry.id)
                if current:
                    current.refreshed_at = queue.now()
                    self.queue.write(current)
                failure = wipe.failed(entry.target) if entry.target else None
                if current and failure:
                    self.queue.delete(entry.id)
                    raise LeaseError("you are no longer queued: "
                                     + self.out_of_rotation_message(entry.target, failure))
            if current is None:
                claim = self._queued_claim(entry.id)
                return ("yours", claim) if claim else ("ended", None)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "timeout", None
            time.sleep(min(poll, remaining))
            self._devices = None
            self.sweep()

    def unqueue(self, target: str | None = None, any_device: bool = False,
                everything: bool = False) -> list[Entry]:
        mine = [e for e in self.queue.all() if self.is_mine(e)]
        if everything:
            chosen = mine
        elif target:
            try:
                device = find(self.devices, target)
            except DiscoveryError:
                device = None
            names = {target} | ({device.id} if device else set())
            chosen = [e for e in mine if e.target in names]
            if not chosen:
                raise LeaseError(f"you are not queued for {target}")
        elif any_device:
            chosen = [e for e in mine if e.target is None]
            if not chosen:
                raise LeaseError("you are not queued for any device")
        else:
            if len(mine) != 1:
                waiting = ", ".join(e.label for e in mine) or "nothing"
                raise LeaseError(f"name the device, or pass --any or --all "
                                 f"(you are queued for: {waiting})")
            chosen = mine
        with self.store.lock():
            for entry in chosen:
                self.queue.delete(entry.id)
        return chosen

    def take_handoff_notices(self) -> list[Claim]:
        """This owner's claims the queue handed over and nobody has told it about yet. The
        caller tells it, so the watcher no longer has to message it."""
        if not any(c.handoff_pending for c in self.mine()):
            return []
        pending = []
        with self.store.lock():
            for claim in self.mine():
                if claim.handoff_pending:
                    claim.handoff_pending = False
                    claim.notify_session = None
                    self.store.write(claim)
                    pending.append(claim)
        return pending
