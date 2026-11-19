"""Finds which Android devices a Bash command line would use.

This is a best-effort reading of shell syntax, not a shell parser. It splits the line into simple
commands at `;`, `&&`, `||`, `|`, `&`, newlines, parentheses and backticks, follows `sh -c`, skips
heredoc bodies and common prefixes (`sudo`, `env`, `timeout`, ...), and recognizes the tools that
talk to a device.
"""

import os
import re
import shlex
from dataclasses import dataclass, replace

EXPLICIT = "explicit"
"""The command names its device."""
IMPLICIT = "implicit"
"""The command uses whichever device the tool picks: the only one connected, or ANDROID_SERIAL."""
DYNAMIC = "dynamic"
"""The command names its device through the shell (`$SERIAL`), which cannot be read statically."""
UNPARSEABLE = "unparseable"
INVENTORY = "inventory"
"""The command adds, removes or changes an emulator or a system image. `device` describes how."""

USER_REQUESTED = "DEVICELEASE_USER_REQUESTED"
"""Set to 1 in front of an inventory command when the user asked for the change."""

QUICK_CHECK = re.compile(
    r"adb|gradle|emulator|android|agent-device|scrcpy|fastboot|avd|sdkmanager|system-images",
    re.IGNORECASE)
OPERATORS = {";", "&", "&&", "|", "||", "|&", ";;", "(", ")", "\n"}
SELF_SERIAL = {"$(devicelease serial)", "`devicelease serial`"}

ADB_HOST_COMMANDS = {
    "devices", "version", "help", "start-server", "kill-server", "connect", "disconnect", "pair",
    "mdns", "keygen", "host-features", "server-status", "track-devices", "nodaemon",
}
ANDROID_DEVICE_COMMANDS = {"install", "run", "layout", "screen"}
GRADLE_DEVICE_TASKS = ("connected", "install", "uninstall")
GRADLE_OPTIONS_WITH_VALUE = {
    "-p", "--project-dir", "-b", "--build-file", "-c", "--settings-file", "-g",
    "--gradle-user-home", "-I", "--init-script", "-x", "--exclude-task", "--console",
    "--warning-mode", "--max-workers", "--priority", "-D", "-P",
}
WRAPPERS = {"sudo", "env", "time", "nohup", "nice", "timeout", "exec", "command", "stdbuf",
            "xargs", "watch", "unbuffer", "caffeinate", "ionice", "chrt", "taskset"}
SHELLS = {"sh", "bash", "zsh", "dash"}
KEYWORDS = {"if", "then", "else", "elif", "while", "until", "do", "!", "{"}
VARIABLE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
WRAPPER_OPTIONS_WITH_VALUE = {
    "sudo": {"-u", "-g", "-C", "-p", "-U", "-h", "-r", "-t", "-D"},
    "env": {"-u", "-C", "-S", "--unset", "--chdir"},
    "timeout": {"-s", "-k", "--signal", "--kill-after"},
    "nice": {"-n", "--adjustment"},
    "ionice": {"-c", "-n", "-p"},
    "stdbuf": {"-i", "-o", "-e"},
    "xargs": {"-I", "-n", "-P", "-d", "-L", "-s", "-a", "-E"},
    "watch": {"-n", "-d"},
    "taskset": {"-c"},
}
REMOVING = {"rm", "unlink", "rmdir", "shred"}
"""File commands whose every path argument is removed. Removing a parent of a guarded folder
removes the folder too."""
COPYING = {"cp", "ln", "install", "rsync"}
"""File commands whose last path argument is the destination."""
WRITING = {"touch", "truncate", "tee", "mkdir"}
"""File commands that write every path argument."""
REMOVES = re.compile(r"(^|[\s;&|(/])(rm|mv|unlink|rmdir|shred)\s")
GLOB = re.compile(r"[*?\[]")
HEREDOC = re.compile(r"<<-?[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1[^\n]*\n")


@dataclass(frozen=True)
class Target:
    tool: str
    mode: str
    device: str | None = None
    kind: str | None = None
    """For `adb -d` / `adb -e`: only a physical device, or only an emulator, is picked."""
    command: str = ""
    boots: bool = False
    """The command starts an emulator."""
    gpu: str | None = None
    """The `-gpu` an emulator boot passes, or None when it leaves the choice to the AVD."""
    user_requested: bool = False
    """An inventory command carries `DEVICELEASE_USER_REQUESTED=1`."""

    @property
    def self_serial(self) -> bool:
        return self.device in SELF_SERIAL


def _strip_heredocs(text: str) -> str:
    out, pos = [], 0
    while match := HEREDOC.search(text, pos):
        out.append(text[pos:match.end()])
        delimiter = match.group(2)
        end = re.compile(rf"^[ \t]*{re.escape(delimiter)}[ \t]*$", re.M).search(text, match.end())
        if end is None:
            return "".join(out)
        pos = end.end()
    out.append(text[pos:])
    return "".join(out)


def _tokens(text: str) -> list[str]:
    lexer = shlex.shlex(text.replace("`", "\n"), posix=True, punctuation_chars=";&|()\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = "#"
    return list(lexer)


def _simple_commands(text: str) -> list[list[str]]:
    commands, current = [], []
    for token in _tokens(text):
        if token in OPERATORS or set(token) <= set(";&|()\n"):
            if current:
                commands.append(current)
            current = []
        else:
            current.append(token)
    if current:
        commands.append(current)
    return commands


def _split_redirections(words: list[str]) -> tuple[list[str], list[str]]:
    """Separates redirections from the command's words. Returns the words and the files the
    command writes through `>`, `>>` and `&>`."""
    result, written, skip = [], [], None
    for word in words:
        if skip is not None:
            if skip:
                written.append(word)
            skip = None
            continue
        if match := re.fullmatch(r"\d*(>>?|<<?<?|>&|&>>?|>\|)", word):
            skip = ">" in match.group(1) and match.group(1) != ">&"
            continue
        if match := re.match(r"^\d*(>>?|<|&>>?)(\S+)", word):
            if ">" in match.group(1) and not match.group(2).startswith("&"):
                written.append(match.group(2))
            continue
        result.append(word)
    return result, written


def _skip_wrappers(words: list[str], env: dict[str, str]) -> list[str]:
    """Drops leading assignments and wrapper commands. Assignments are recorded in `env`."""
    while words:
        if ASSIGNMENT.match(words[0]):
            key, _, value = words[0].partition("=")
            env[key] = value
            words = words[1:]
            continue
        if words[0] in KEYWORDS:
            words = words[1:]
            continue
        name = os.path.basename(words[0])
        if name not in WRAPPERS:
            return words
        with_value = WRAPPER_OPTIONS_WITH_VALUE.get(name, set())
        words = words[1:]
        while words:
            word = words[0]
            if name == "env" and ASSIGNMENT.match(word):
                key, _, value = word.partition("=")
                env[key] = value
            elif word in with_value:
                words = words[1:]
            elif not word.startswith("-") or word == "-":
                break
            words = words[1:]
        if name == "timeout" and words:
            words = words[1:]
    return words


def _expand(word: str, shell_env: dict[str, str]) -> str:
    """Substitutes `$NAME` and `${NAME}` from variables set earlier in the line. A word with any
    variable that cannot be resolved stays as it is."""
    names = [a or b for a, b in VARIABLE.findall(word)]
    if not names or any(n not in shell_env for n in names):
        return word
    return VARIABLE.sub(lambda m: shell_env[m.group(1) or m.group(2)], word)


def _assign(shell_env: dict[str, str], key: str, value: str) -> None:
    """Records a shell variable, or forgets it when its value is only known at run time."""
    value = _expand(value, shell_env)
    if "$" in value or "`" in value:
        shell_env.pop(key, None)
    else:
        shell_env[key] = value


def _device(tool: str, value: str | None, line: str, kind: str | None = None) -> Target:
    if value is None:
        return Target(tool, IMPLICIT, kind=kind, command=line)
    if value in SELF_SERIAL:
        return Target(tool, DYNAMIC, value, command=line)
    if "$" in value or "`" in value:
        return Target(tool, DYNAMIC, value, command=line)
    return Target(tool, EXPLICIT, value, command=line)


def _option_value(words: list[str], *names: str) -> tuple[bool, str | None]:
    for i, word in enumerate(words):
        for name in names:
            if word == name:
                return True, words[i + 1] if i + 1 < len(words) else None
            if name.startswith("--") and word.startswith(name + "="):
                return True, word[len(name) + 1:]
    return False, None


def _adb(words: list[str], env: dict[str, str], line: str) -> list[Target]:
    serial, kind, transport = None, None, False
    i = 1
    subcommand = None
    while i < len(words):
        word = words[i]
        if word == "-s" and i + 1 < len(words):
            serial, i = words[i + 1], i + 2
        elif word.startswith("-s") and len(word) > 2:
            serial, i = word[2:], i + 1
        elif word == "-t":
            transport, i = True, i + 2
        elif word.startswith("-t") and len(word) > 2:
            transport, i = True, i + 1
        elif word == "-d":
            kind, i = "physical", i + 1
        elif word == "-e":
            kind, i = "emulator", i + 1
        elif word in ("-H", "-P", "-L"):
            i += 2
        elif word.startswith("-"):
            i += 1
        else:
            subcommand = word
            break
    if subcommand is None or subcommand in ADB_HOST_COMMANDS:
        return []
    if transport:
        return [Target("adb", DYNAMIC, "transport id", command=line)]
    return [_device("adb", serial or env.get("ANDROID_SERIAL"), line, None if serial else kind)]


def _android(words: list[str], env: dict[str, str], line: str) -> list[Target]:
    args = [w for w in words[1:] if not (w.startswith("--sdk") or w in ("-v", "--verbose"))]
    if not args:
        return []
    command = args[0]
    if command == "emulator":
        if len(args) >= 3 and args[1] in ("start", "stop"):
            avd = next((a for a in args[2:] if not a.startswith("-")), None)
            if avd:
                return [replace(_device("android", avd, line), boots=args[1] == "start")]
        return []
    if command not in ANDROID_DEVICE_COMMANDS:
        return []
    if command == "screen" and (len(args) < 2 or args[1] != "capture"):
        return []
    found, device = _option_value(args, "--device")
    if found and device:
        return [_device("android", device, line)]
    return [_device("android", env.get("ANDROID_SERIAL"), line)]


def _emulator(words: list[str], line: str) -> list[Target]:
    found, avd = _option_value(words, "-avd", "--avd")
    if not found:
        avd = next((w[1:] for w in words[1:] if w.startswith("@") and len(w) > 1), None)
    if not avd:
        return []
    _, gpu = _option_value(words, "-gpu", "--gpu")
    return [replace(_device("emulator", avd, line), boots=True, gpu=gpu)]


def _gradle(words: list[str], env: dict[str, str], line: str) -> list[Target]:
    tasks, i = [], 1
    while i < len(words):
        word = words[i]
        if word in GRADLE_OPTIONS_WITH_VALUE:
            i += 2
            continue
        if not word.startswith("-"):
            tasks.append(word)
        i += 1
    names = [t.rsplit(":", 1)[-1] for t in tasks]
    if not any(n.startswith(GRADLE_DEVICE_TASKS) for n in names):
        return []
    return [_device("gradle", env.get("ANDROID_SERIAL"), line)]


def _flagged(tool: str, words: list[str], env: dict[str, str], line: str, *flags: str,
             implicit: bool = True) -> list[Target]:
    found, device = _option_value(words, *flags)
    if found and device:
        return [_device(tool, device, line)]
    if implicit:
        return [_device(tool, env.get("ANDROID_SERIAL"), line)]
    return []


def _guarded_path(word: str, env: dict[str, str], guarded: tuple[str, ...],
                  removes: bool) -> str | None:
    """The path a command argument names, when it lies under a guarded folder. With `removes`,
    a parent of a guarded folder counts too."""
    if not guarded or not word or word == "-" or word.startswith("-"):
        return None
    path = os.path.expanduser(os.path.expandvars(_expand(word, env)))
    if "$" in path or "`" in path:
        return None
    if not os.path.isabs(path):
        if "PWD" not in env:
            return None
        path = os.path.join(env["PWD"], path)
    path = os.path.normpath(path)
    match = GLOB.search(path)
    full = os.path.realpath(os.path.dirname(path[:match.start()]) if match else path)
    for root in guarded:
        root = os.path.realpath(root)
        common = os.path.commonpath([full, root])
        if common == root or (removes and common == full):
            home = os.path.expanduser("~")
            return "~" + path[len(home):] if path == home or path.startswith(home + os.sep) \
                else path
    return None


def _file_changes(name: str, words: list[str], written: list[str], env: dict[str, str],
                  guarded: tuple[str, ...]) -> list[str]:
    """What a file command does to guarded folders, as "delete <path>" or "write <path>"."""
    args = [w for w in words[1:] if not w.startswith("-")]
    removed, changed = [], list(written)
    if name in REMOVING:
        removed = args
    elif name == "mv" and len(args) >= 2:
        removed, changed = args[:-1], changed + args[-1:]
    elif name in COPYING and len(args) >= 2:
        changed += args[-1:]
    elif name in WRITING or (name == "sed" and any(
            w.startswith(("-i", "--in-place")) for w in words[1:])):
        changed += args
    changes = []
    for verb, paths, removes in (("delete", removed, True), ("write", changed, False)):
        for path in paths:
            if found := _guarded_path(path, env, guarded, removes):
                changes.append(f"{verb} {found}")
    return changes


def _avdmanager(words: list[str]) -> list[str]:
    for i, word in enumerate(words[1:-1], start=1):
        if words[i + 1] != "avd" or word not in ("create", "delete", "move"):
            continue
        _, name = _option_value(words, "-n", "--name")
        name = name or "(unnamed)"
        if word == "create":
            _, package = _option_value(words, "-k", "--package")
            return [f"create AVD {name}" + (f" from {package}" if package else "")]
        return [f"delete AVD {name}" if word == "delete" else f"move or rename AVD {name}"]
    return []


def _sdk_packages(verb: str, packages: list[str]) -> list[str]:
    return [f"{verb} system image {p}" for p in packages if p.startswith("system-images")]


def _sdkmanager(words: list[str]) -> list[str]:
    args = words[1:]
    if "--update" in args:
        return ["update every SDK package, system images included"]
    packages = [w for w in args if not w.startswith("-")]
    return _sdk_packages("uninstall" if "--uninstall" in args else "install", packages)


def _android_inventory(args: list[str]) -> list[str]:
    positional = [a for a in args[2:] if not a.startswith("-")]
    if args[:2] == ["emulator", "create"] and "--list-profiles" not in args:
        return [f"create AVD" + (f" from profile {positional[0]}" if positional else "")]
    if args[:2] == ["emulator", "remove"]:
        return [f"delete AVD {positional[0] if positional else '(unnamed)'}"]
    if args[:2] == ["sdk", "install"]:
        return _sdk_packages("install", positional)
    if args[:2] == ["sdk", "remove"]:
        return _sdk_packages("uninstall", positional)
    if args[:2] == ["sdk", "update"]:
        if not positional:
            return ["update every SDK package, system images included"]
        return _sdk_packages("update", positional)
    return []


def _rootavd(words: list[str]) -> list[str]:
    args = [w for w in words[1:] if not w.startswith("-")]
    if args and args[0] == "ListAllAVDs":
        return []
    return ["patch a system image with rootAVD" + (f" ({args[0]})" if args else "")]


def _inventory(words: list[str], written: list[str], env: dict[str, str],
               guarded: tuple[str, ...], line: str) -> list[Target]:
    name = os.path.basename(words[0])
    if name in SHELLS and len(words) > 1 and os.path.basename(words[1]).startswith("rootAVD"):
        words, name = words[1:], os.path.basename(words[1])
    if name == "avdmanager":
        changes = _avdmanager(words)
    elif name == "sdkmanager":
        changes = _sdkmanager(words)
    elif name == "android":
        changes = _android_inventory(
            [w for w in words[1:] if not (w.startswith("--sdk") or w in ("-v", "--verbose"))])
    elif name.startswith("rootAVD"):
        changes = _rootavd(words)
    else:
        changes = []
    changes += _file_changes(name, words, written, env, guarded)
    requested = env.get(USER_REQUESTED) == "1"
    return [Target(name, INVENTORY, change, command=line, user_requested=requested)
            for change in changes]


def _cd(words: list[str], shell_env: dict[str, str]) -> None:
    target = words[1] if len(words) > 1 else "~"
    path = os.path.expanduser(os.path.expandvars(_expand(target, shell_env)))
    if target == "-" or "$" in path or "`" in path:
        shell_env.pop("PWD", None)
    elif os.path.isabs(path):
        shell_env["PWD"] = os.path.normpath(path)
    elif "PWD" in shell_env:
        shell_env["PWD"] = os.path.normpath(os.path.join(shell_env["PWD"], path))


def _command_targets(words: list[str], shell_env: dict[str, str], depth: int,
                     guarded: tuple[str, ...] = ()) -> list[Target]:
    env = dict(shell_env)
    words, written = _split_redirections(words)
    words = _skip_wrappers(words, env)
    if not words:
        # A line of bare assignments sets shell variables for the commands after it.
        for key, value in env.items():
            if shell_env.get(key) != value:
                _assign(shell_env, key, value)
        return []
    # Bash expands arguments before a prefix assignment (`S=x adb -s $S`) takes effect, so only
    # variables from earlier commands apply.
    words = [_expand(w, shell_env) for w in words]
    env = {k: _expand(v, shell_env) for k, v in env.items()}
    line = shlex.join(words)
    name = os.path.basename(words[0])
    inventory = _inventory(words, written, env, guarded, line)
    if name == "cd":
        _cd(words, shell_env)
        return inventory
    return inventory + _device_targets(name, words, env, shell_env, depth, guarded, line)


def _device_targets(name: str, words: list[str], env: dict[str, str], shell_env: dict[str, str],
                    depth: int, guarded: tuple[str, ...], line: str) -> list[Target]:
    if name == "export":
        for word in words[1:]:
            if ASSIGNMENT.match(word):
                key, _, value = word.partition("=")
                _assign(shell_env, key, value)
        return []
    if name == "unset" or name == "read":
        for word in words[1:]:
            shell_env.pop(word, None)
        return []
    if name in ("for", "select") and len(words) > 1:
        shell_env.pop(words[1], None)
        return []
    if name in SHELLS:
        found, script = _option_value(words, "-c", "-lc", "-ic")
        if found and script and depth < 3:
            return _targets(script, dict(env), depth + 1, guarded)
        return []
    if name == "devicelease":
        return []
    if name == "adb":
        return _adb(words, env, line)
    if name == "android":
        return _android(words, env, line)
    if name == "emulator":
        return _emulator(words, line)
    if name in ("gradlew", "gradle", "gradlew.bat"):
        return _gradle(words, env, line)
    if name == "agent-device":
        # Inside T3 Code, agent-device is pinned to the device that device_open claimed.
        return _flagged("agent-device", words, env, line, "--serial", "--device", "--udid",
                        implicit=False)
    if name == "scrcpy":
        return _flagged("scrcpy", words, env, line, "-s", "--serial")
    if name == "fastboot" and len(words) > 1 and words[1] not in ("devices", "--version", "help"):
        return _flagged("fastboot", words, env, line, "-s")
    return []


def _targets(text: str, env: dict[str, str], depth: int,
             guarded: tuple[str, ...] = ()) -> list[Target]:
    targets = []
    for words in _simple_commands(_strip_heredocs(text)):
        targets.extend(_command_targets(words, env, depth, guarded))
    return targets


def _inside(path: str, roots: tuple[str, ...]) -> bool:
    path = os.path.realpath(path)
    return any(os.path.commonpath([path, os.path.realpath(r)]) == os.path.realpath(r)
               for r in roots)


def device_targets(command: str, env: dict[str, str] | None = None, cwd: str | None = None,
                   guarded: tuple[str, ...] = ()) -> list[Target]:
    """Finds the devices a command uses, and the changes it makes to emulators.

    `env` holds the variables the command starts with, such as an exported ANDROID_SERIAL.
    `cwd` is where it runs, and `guarded` the folders whose files make up emulators and system
    images: a command that writes or removes files there is an inventory change.
    """
    if not (QUICK_CHECK.search(command) or (guarded and REMOVES.search(command))
            or (cwd and _inside(cwd, guarded))):
        return []
    env = dict(env or {})
    if cwd:
        env["PWD"] = cwd
    try:
        return _targets(command, env, 0, tuple(guarded))
    except ValueError:
        # Unbalanced quotes and the like: the command fails in the shell too, unless the parser
        # is wrong, so it is only flagged when it mentions a device tool as a word.
        if re.search(r"(^|[\s;&|(/])(adb|gradlew|emulator|android|scrcpy|fastboot)\s", command):
            return [Target("unknown", UNPARSEABLE, command=command)]
        return []
