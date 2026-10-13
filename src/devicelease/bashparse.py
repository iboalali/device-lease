"""Finds which Android devices a Bash command line would use.

This is a best-effort reading of shell syntax, not a shell parser. It splits the line into simple
commands at `;`, `&&`, `||`, `|`, `&`, newlines, parentheses and backticks, follows `sh -c`, skips
heredoc bodies and common prefixes (`sudo`, `env`, `timeout`, ...), and recognizes the tools that
talk to a device.
"""

import os
import re
import shlex
from dataclasses import dataclass

EXPLICIT = "explicit"
"""The command names its device."""
IMPLICIT = "implicit"
"""The command uses whichever device the tool picks: the only one connected, or ANDROID_SERIAL."""
DYNAMIC = "dynamic"
"""The command names its device through the shell (`$SERIAL`), which cannot be read statically."""
UNPARSEABLE = "unparseable"

QUICK_CHECK = re.compile(r"adb|gradle|emulator|android|agent-device|scrcpy|fastboot")
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
HEREDOC = re.compile(r"<<-?[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1[^\n]*\n")


@dataclass(frozen=True)
class Target:
    tool: str
    mode: str
    device: str | None = None
    kind: str | None = None
    """For `adb -d` / `adb -e`: only a physical device, or only an emulator, is picked."""
    command: str = ""

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


def _drop_redirections(words: list[str]) -> list[str]:
    result, skip = [], False
    for word in words:
        if skip:
            skip = False
            continue
        if re.fullmatch(r"\d*(>>?|<<?<?|>&|&>>?)", word):
            skip = True
            continue
        if re.match(r"^\d*(>>?|<|&>)\S", word):
            continue
        result.append(word)
    return result


def _skip_wrappers(words: list[str], env: dict[str, str]) -> list[str]:
    """Drops leading assignments and wrapper commands. Assignments are recorded in `env`."""
    while words:
        if ASSIGNMENT.match(words[0]):
            key, _, value = words[0].partition("=")
            env[key] = value
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
                return [_device("android", avd, line)]
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
    return [_device("emulator", avd, line)] if avd else []


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


def _command_targets(words: list[str], shell_env: dict[str, str], depth: int) -> list[Target]:
    env = dict(shell_env)
    words = _skip_wrappers(_drop_redirections(words), env)
    if not words:
        # A line of bare assignments sets shell variables for the commands after it.
        shell_env.update(env)
        return []
    line = shlex.join(words)
    name = os.path.basename(words[0])
    if name == "export":
        for word in words[1:]:
            if ASSIGNMENT.match(word):
                key, _, value = word.partition("=")
                shell_env[key] = value
        return []
    if name == "unset":
        for word in words[1:]:
            shell_env.pop(word, None)
        return []
    if name in SHELLS:
        found, script = _option_value(words, "-c", "-lc", "-ic")
        if found and script and depth < 3:
            return _targets(script, dict(env), depth + 1)
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


def _targets(text: str, env: dict[str, str], depth: int) -> list[Target]:
    targets = []
    for words in _simple_commands(_strip_heredocs(text)):
        targets.extend(_command_targets(words, env, depth))
    return targets


def device_targets(command: str, env: dict[str, str] | None = None) -> list[Target]:
    """`env` holds the variables the command starts with, such as an exported ANDROID_SERIAL."""
    if not QUICK_CHECK.search(command):
        return []
    try:
        return _targets(command, dict(env or {}), 0)
    except ValueError:
        # Unbalanced quotes and the like: the command fails in the shell too, unless the parser
        # is wrong, so it is only flagged when it mentions a device tool as a word.
        if re.search(r"(^|[\s;&|(/])(adb|gradlew|emulator|android|scrcpy|fastboot)\s", command):
            return [Target("unknown", UNPARSEABLE, command=command)]
        return []
