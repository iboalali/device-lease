import json
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

from devicelease import paths

PHYSICAL = "physical"
EMULATOR = "emulator"

ONLINE = "online"
STOPPED = "stopped"
MISSING = "missing"


class DiscoveryError(Exception):
    pass


@dataclass
class Device:
    id: str
    kind: str
    serial: str | None
    state: str
    name: str
    api: int | None = None
    platform: str = "android"

    @property
    def online(self) -> bool:
        return self.state == ONLINE

    def to_dict(self) -> dict:
        return asdict(self)


def _adb(*args: str, serial: str | None = None, timeout: float = 10) -> str:
    adb = paths.adb_path()
    if adb is None:
        raise DiscoveryError("adb not found; set ANDROID_HOME or DEVICELEASE_ADB")
    command = [adb] + (["-s", serial] if serial else []) + list(args)
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise DiscoveryError(f"{' '.join(command)} timed out") from e
    except OSError as e:
        raise DiscoveryError(f"cannot run adb: {e}") from e
    if result.returncode != 0:
        raise DiscoveryError(result.stderr.strip() or f"{' '.join(command)} failed")
    return result.stdout


def _adb_devices() -> list[tuple[str, str, dict[str, str]]]:
    entries = []
    for line in _adb("devices", "-l").splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2:
            continue
        props = dict(p.split(":", 1) for p in parts[2:] if ":" in p)
        entries.append((parts[0], parts[1], props))
    return entries


def _emulator_avd_name(serial: str) -> str | None:
    try:
        lines = _adb("emu", "avd", "name", serial=serial, timeout=5).splitlines()
    except DiscoveryError:
        return None
    name = lines[0].strip() if lines else ""
    return name or None


def _api_level(serial: str) -> int | None:
    try:
        value = _adb("shell", "getprop", "ro.build.version.sdk", serial=serial, timeout=5).strip()
    except DiscoveryError:
        return None
    return int(value) if value.isdigit() else None


def _read_ini(path: Path) -> dict[str, str]:
    values = {}
    try:
        with open(path, errors="replace") as f:
            for line in f:
                key, sep, value = line.partition("=")
                if sep:
                    values[key.strip()] = value.strip()
    except OSError:
        pass
    return values


def list_avds() -> dict[str, tuple[str, int | None]]:
    """Maps each AVD name to its display name and API level."""
    avds = {}
    home = paths.avd_home()
    for ini in sorted(home.glob("*.ini")):
        name = ini.stem
        avd_dir = _read_ini(ini).get("path") or str(home / f"{name}.avd")
        config = _read_ini(Path(avd_dir) / "config.ini")
        api = None
        for key in ("image.sysdir.1", "target"):
            match = re.search(r"android-(\d+)", config.get(key, ""))
            if match:
                api = int(match.group(1))
                break
        avds[name] = (config.get("avd.ini.displayname", name), api)
    return avds


def _fake_devices(path: str) -> list[Device]:
    with open(path) as f:
        return [Device(**entry) for entry in json.load(f)]


def discover() -> list[Device]:
    fake = os.environ.get("DEVICELEASE_FAKE_DEVICES")
    if fake:
        return _fake_devices(fake)

    entries = _adb_devices()
    avds = list_avds()
    online = [serial for serial, state, _ in entries if state == "device"]
    with ThreadPoolExecutor(max_workers=8) as pool:
        apis = dict(zip(online, pool.map(_api_level, online)))
        emulators = [s for s in online if s.startswith("emulator-")]
        avd_names = dict(zip(emulators, pool.map(_emulator_avd_name, emulators)))

    devices: list[Device] = []
    running_avds = set()
    for serial, state, props in entries:
        is_emulator = serial.startswith("emulator-")
        avd = avd_names.get(serial)
        if avd:
            running_avds.add(avd)
        if is_emulator and avd and avd in avds:
            name = avds[avd][0]
        else:
            name = props.get("model", "").replace("_", " ") or serial
        devices.append(Device(
            id=avd or serial,
            kind=EMULATOR if is_emulator else PHYSICAL,
            serial=serial,
            state=ONLINE if state == "device" else state,
            name=name,
            api=apis.get(serial) or (avds[avd][1] if avd in avds else None),
        ))
    for avd, (name, api) in avds.items():
        if avd not in running_avds:
            devices.append(Device(id=avd, kind=EMULATOR, serial=None, state=STOPPED, name=name, api=api))
    return devices


def find(devices: list[Device], target: str) -> Device | None:
    """Finds a device by ID, falling back to its current serial."""
    for device in devices:
        if device.id == target:
            return device
    for device in devices:
        if device.serial == target:
            return device
    return None


def boot_emulator(avd: str, log_path: Path, timeout: float = 240) -> str:
    """Starts an emulator, waits until Android finished booting and returns its serial."""
    emulator = paths.emulator_path()
    if emulator is None:
        raise DiscoveryError("emulator not found; set ANDROID_HOME or DEVICELEASE_EMULATOR")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.unlink(missing_ok=True)
    # Append mode, so the cleanup can empty the log while the emulator still writes to it.
    with open(log_path, "a") as log:
        process = subprocess.Popen(
            [emulator, "-avd", avd], stdin=subprocess.DEVNULL, stdout=log,
            stderr=subprocess.STDOUT, start_new_session=True,
        )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() not in (None, 0):
            raise DiscoveryError(f"emulator {avd} exited with code {process.returncode}; see {log_path}")
        for serial, state, _ in _adb_devices():
            if state != "device" or not serial.startswith("emulator-"):
                continue
            if _emulator_avd_name(serial) != avd:
                continue
            try:
                booted = _adb("shell", "getprop", "sys.boot_completed", serial=serial, timeout=5)
            except DiscoveryError:
                continue
            if booted.strip() == "1":
                return serial
        time.sleep(2)
    raise DiscoveryError(f"emulator {avd} did not finish booting within {int(timeout)}s; see {log_path}")
