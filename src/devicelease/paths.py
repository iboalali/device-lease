import os
import shutil
from pathlib import Path


def _xdg(var: str, fallback: str) -> Path:
    value = os.environ.get(var)
    return Path(value) if value else Path.home() / fallback


def state_dir() -> Path:
    override = os.environ.get("DEVICELEASE_STATE_DIR")
    if override:
        return Path(override)
    return _xdg("XDG_STATE_HOME", ".local/state") / "devicelease"


def claims_dir() -> Path:
    return state_dir() / "claims"


def queue_dir() -> Path:
    return state_dir() / "queue"


def policy_path() -> Path:
    override = os.environ.get("DEVICELEASE_POLICY")
    if override:
        return Path(override)
    return _xdg("XDG_CONFIG_HOME", ".config") / "devicelease" / "policy.json"


def sdk_root() -> Path:
    for var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        value = os.environ.get(var)
        if value:
            return Path(value)
    return Path.home() / "Android" / "Sdk"


def _sdk_tool(env_var: str, relative: str, name: str) -> str | None:
    override = os.environ.get(env_var)
    if override:
        return override
    candidate = sdk_root() / relative
    if candidate.is_file():
        return str(candidate)
    return shutil.which(name)


def adb_path() -> str | None:
    return _sdk_tool("DEVICELEASE_ADB", "platform-tools/adb", "adb")


def emulator_path() -> str | None:
    return _sdk_tool("DEVICELEASE_EMULATOR", "emulator/emulator", "emulator")


def avd_home() -> Path:
    value = os.environ.get("ANDROID_AVD_HOME")
    if value:
        return Path(value)
    user_home = os.environ.get("ANDROID_USER_HOME")
    if user_home:
        return Path(user_home) / "avd"
    return Path.home() / ".android" / "avd"


def display_path(path: str) -> str:
    home = str(Path.home())
    if path == home or path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path
