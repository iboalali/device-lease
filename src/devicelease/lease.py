from datetime import timedelta

from devicelease import paths, t3
from devicelease.claims import Claim, Store, now, stale_reason
from devicelease.devices import (
    EMULATOR, PHYSICAL, STOPPED, Device, DiscoveryError, boot_emulator, discover, find,
)
from devicelease.owner import Owner, resolve_owner
from devicelease.policy import Policy, load_policy
from devicelease.project import Project, resolve_project


class LeaseError(Exception):
    pass


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


def describe_holder(claim: Claim) -> str:
    return (f"{short_owner(claim.owner)} ({paths.display_path(claim.project)}, "
            f"since {age(claim.created_at)})")


class Lease:
    def __init__(self, owner: Owner, project: Project, policy: Policy, store: Store):
        self.owner = owner
        self.project = project
        self.policy = policy
        self.store = store
        self._devices: list[Device] | None = None
        self._t3_project: tuple[str, str] | None = None

    @classmethod
    def create(cls, owner: str | None = None, env=None, cwd: str | None = None) -> "Lease":
        project = resolve_project(cwd)
        return cls(resolve_owner(owner, env), project, load_policy(project),
                   Store(paths.claims_dir()))

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

    def is_mine(self, claim: Claim) -> bool:
        return claim.owner in self.owner.ids

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
        self._end_t3_sessions([claim for claim, _ in removed])
        return removed

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
              t3_device: str | None = None) -> tuple[Claim, bool]:
        """Claims a device. Returns the claim and whether this owner already held it."""
        device = find(self.devices, target)
        if device is None:
            raise LeaseError(f"unknown device {target}; run `devicelease list`")
        reason = self.not_allowed(device)
        if reason:
            raise LeaseError(f"{device.id} is not allowed here: {reason}")
        if boot and device.kind != EMULATOR:
            raise LeaseError(f"--boot only applies to emulators; {device.id} is a physical device")

        online = self.online_ids()
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
                        raise LeaseError(f"{device.id} is taken by {describe_holder(existing)}")
                    self.store.delete(device.id)
                claim, already = self._new_claim(device, note), False
                claim.t3_device_id = t3_device
                if not self.store.create(claim):
                    raise LeaseError(f"{device.id} was just claimed by another session")

        if boot and device.state == STOPPED:
            try:
                serial = boot_emulator(device.id, paths.state_dir() / "logs" / f"{device.id}.log")
            except DiscoveryError:
                if not already:
                    self._release([claim])
                raise
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
                    and not self.not_allowed(device))

        candidates = [d for d in self.devices if d.online and matches(d)]
        candidates.sort(key=lambda d: (d.kind != EMULATOR, d.id))
        online = self.online_ids()
        with self.store.lock():
            for device in candidates:
                existing = self.store.read(device.id)
                if existing:
                    if self.is_mine(existing) or not stale_reason(existing, self.expiry, online):
                        continue
                    self.store.delete(device.id)
                claim = self._new_claim(device, note)
                if self.store.create(claim):
                    return claim

        bootable = [
            d.id for d in self.devices
            if d.state == STOPPED and matches(d) and self.store.read(d.id) is None
        ]
        message = "no free device matches. "
        if kind == PHYSICAL:
            message += "Connect a device."
        else:
            message += ("Start an emulator from the Android Studio Device Manager "
                        "(or `devicelease claim <avd> --boot`), or connect a device.")
            if bootable:
                message += f"\nEmulators you may boot: {', '.join(bootable)}"
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
        self._end_t3_sessions(released)
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
                raise LeaseError(f"{other.device} is taken by {describe_holder(other)}; "
                                 f"run `devicelease list` and claim a free device")
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
