# devicelease

Shared Android device claims for agent sessions.

Several coding agents on one machine (Claude Code in a terminal, Claude Code in T3 Code, opencode) often want the same phone or emulator. devicelease gives each session a way to see which devices exist, claim one for the length of its work, and leave the others alone. Hooks block device commands on a device the session has not claimed, and claims end on their own when a session or T3 Code thread is done.

Linux only, Android only (physical devices and emulators), Python 3.11 or newer with no dependencies.

## Install

```sh
git clone https://github.com/iboalali/device-lease.git
cd device-lease
ln -s "$PWD/bin/devicelease" ~/.local/bin/devicelease
devicelease list
```

`~/.local/bin` must be on `PATH`. The symlink points into the clone, so moving the clone means recreating the symlink.

devicelease finds the Android SDK through `ANDROID_HOME`, then `ANDROID_SDK_ROOT`, then `~/Android/Sdk`. `DEVICELEASE_ADB` and `DEVICELEASE_EMULATOR` point it at specific `adb` and `emulator` binaries.

## Use

```sh
devicelease list                        # every device, its state and who holds it
devicelease claim --any                 # first free running device, emulators before phones
devicelease claim 0A1B2C3D4E           # a specific device, by serial or AVD name
devicelease claim Pixel_6a --boot       # claim a stopped emulator and start it
devicelease serial                      # the adb serial of the device you hold
adb -s "$(devicelease serial)" shell    # use it
devicelease exec -- ./gradlew connectedAndroidTest
devicelease release                     # free it again
devicelease queue Pixel_6a --wait       # wait until a taken device is yours
devicelease help                        # the full guide
```

`devicelease help` prints everything an agent needs to know in one go: the workflow, how to wait for a taken device, every command, ownership and files. The denial messages point to it.

| Command | What it does |
|---|---|
| `list [--json]` | Every device adb and the AVD folder know about: kind, API level, state (`online`, `stopped`, `offline`) and lease (`free`, `yours`, `taken by ...`, `not allowed here`). |
| `claim <id>` | Claims one device. A physical device is named by its serial, an emulator by its AVD name or current serial. Fails with the holder's name if the device is taken. `--boot` starts a stopped emulator and waits until Android has booted. `--note` stores free text with the claim. |
| `claim --any` | Claims the first free, allowed, running device, emulators before phones. `--emulator`, `--physical` and `--min-api N` narrow the choice. If nothing is free, it says how to get a device and lists the emulators you may boot. |
| `queue <id>` | Waits for a taken device. If it is free, claims it right away. When the device frees up, it is claimed for the first waiter in line, and the waiter is told. `--any` with `--emulator`, `--physical` and `--min-api N` waits for the first matching device. See "Wait for a taken device". |
| `unqueue [<id>]` | Leaves the queue. `--any` leaves the queue for any device, `--all` leaves every queue. |
| `release [<id>]` | Frees your claim. Without an ID it frees your only claim. `--all` frees every claim you hold. `--force <id>` frees another session's claim after a confirmation (`--yes` skips it). |
| `status [--json]` | Every claim, including stale ones, with owner, project and age. |
| `whoami [--json]` | Who devicelease thinks you are, your project, the policy that applies and the devices you may use. |
| `serial` | Prints the current adb serial of your single claimed device. An emulator's serial changes on every boot, so use this instead of remembering it. |
| `check <id or serial>` | Exits 0 if you hold the device, 1 otherwise. For scripts. |
| `exec [<id>] -- <command>` | Runs a command with `ANDROID_SERIAL` set to your device. Gradle device tasks use every connected device without it. |
| `heartbeat` | Refreshes your claims. The Claude Code hooks call it for you. |
| `gc` | Removes stale claims. Every command does this anyway before it acts. |
| `watch` | Hands devices to queued Claude Code sessions and messages them. `queue` starts it in the background. |
| `help [<command>]` | The full guide, or the options of one command. |

Every command takes `--owner <name>` and most take `--json`.

### Wait for a taken device

When a device is taken, the message says how to wait for it, worded for where the caller runs:

| Where | How it waits | How it hears |
|---|---|---|
| Claude Code inside T3 Code | `devicelease queue <id> --wait`, run with Bash `run_in_background`, then the agent ends its turn | The command exits when the device is yours, which wakes the thread. A running background command keeps the thread alive past T3 Code's 30-minute idle limit. |
| Claude Code in a terminal | `devicelease queue <id>`, then keep working | A message from a headless `claude -p --model haiku` run, sent by `devicelease watch` |
| Codex, opencode, scripts | `devicelease queue <id> --wait` | The command returns |

- `--wait` exits 0 with `<id> is free and now yours (serial ...)`, 3 with "still queued" after 115 minutes (below the 2-hour limit of a background command, run it again to keep the place), and 1 when the entry ended. Ctrl-C leaves the queue.
- First come, first served, across named and `--any` waiters. A direct `claim` cannot jump the queue.
- The waiter's own policy decides which devices it may get.
- An entry ends with its owner (session exits, thread settled), with `unqueue`, or 10 minutes after the last poll of its `--wait`.
- A session that got a device from the queue is also told on its next devicelease command or Bash call, if no message reached it.

### Who owns a claim

| Where the command runs | Owner | The claim ends when |
|---|---|---|
| Claude Code inside T3 Code | the thread, `t3:<thread id>` | the thread is settled, archived or deleted |
| Claude Code in a terminal | the session ID | the session exits, or 30 minutes pass without a heartbeat |
| `DEVICELEASE_OWNER=<name>` set | that name | 30 minutes pass without a heartbeat |
| Codex or opencode | `pid:<agent process>` | the agent process exits, or 30 minutes without a heartbeat |
| `--owner me` | you, by hand | the phone is unplugged or the emulator closes |

A claim on a stopped emulator by `--owner me` also ends if the emulator never starts within 30 minutes.

## Policy

Without a policy file every device is allowed everywhere. `~/.config/devicelease/policy.json` limits which devices each project may use:

```json
{
  "expiryMinutes": 30,
  "deny": ["Pixel_rooted"],
  "projects": {
    "~/Projects/shop-app": { "allow": ["0A1B2C3D4E", "shop_api36_*"] },
    "~/Projects/ui-tests": { "allow": ["uitest_*"], "deny": ["uitest_pixel"] },
    "t3:6f1c2a7e-0b9d-4e15-9a3c-2d8e7f0b4c61": { "allow": ["uitest_*"] }
  },
  "defaults": { "allow": ["*"] }
}
```

- Patterns are shell-style globs, matched against the device ID and its display name.
- The global `deny` list wins. Then the project's `deny`, then the project's `allow` (or `defaults.allow` for a project not listed).
- A project is the main repository root, so a git worktree follows the same entry as its repository. A path entry also covers the folders below it, and the longest matching path wins.
- A `t3:<project id>` entry applies to every thread of that T3 Code project, in any worktree, and wins over path entries. `devicelease whoami` inside a T3 Code thread prints the key.
- A repository can add a `.devicelease.json` at its root, with `allow` and `deny` in the same format. It can only narrow what the user policy allows, never widen it.

## Integrate

### Claude Code

```sh
scripts/install-claude-hooks
```

This adds five hooks to `~/.claude/settings.json`, with a backup next to it. They apply to Claude Code in a terminal and inside T3 Code.

- **Before every Bash command** the command line is checked for device use: `adb -s X`, `ANDROID_SERIAL=X`, `./gradlew connected*`/`install*`/`uninstall*`, `emulator -avd X`, `android ... --device=X`, `scrcpy` and `fastboot`, through pipes, `&&` chains, `$(...)`, backticks and `sh -c`. A command that uses a device the session has not claimed is blocked, and the message says what to run instead. A command that names no device (`adb shell`) is only allowed while the session's own device is the only one online.
- The check runs before any part of the command, so claim a device in its own command before using it.
- **T3 Code's `device_open`** claims the device it opens, and is blocked if another session holds it. **`device_close`** releases it.
- **After every Bash or device tool call** the session's claims are refreshed.
- **After every Bash or device tool call** a session that the queue handed a device is told so, once.
- **When a session ends** its claims are released and it leaves the queue. Inside T3 Code the thread keeps them until it is settled, because T3 Code restarts the `claude` process between turns.

A hook that fails lets the tool call through, so a broken devicelease never blocks your work.

### T3 Code

Nothing to set up beyond the Claude Code hooks. devicelease reads T3 Code's database (`~/.t3/userdata/state.sqlite`, read-only) to find the thread and project of a session.

- The thread owns its claims, and they end when the thread is settled.
- `t3:<project id>` policy entries apply per T3 Code project.
- T3 Code's `agent-device` keeps its own hold on a device opened with `device_open`, even after `device_close`. When devicelease releases such a claim, it also ends that `agent-device` session (`agent-device close`), which closes the app the session opened. The calls are logged to `~/.local/state/devicelease/logs/agent-device.log`.

T3 Code's database layout and session naming are not a public API. If a T3 Code update changes them, devicelease prints a warning and falls back to the 30-minute heartbeat.

### Agent instructions

Tell agents about devicelease in their global instructions (`~/.claude/AGENTS.md` or `CLAUDE.md` for Claude Code, `~/.config/opencode/AGENTS.md` for opencode, `~/.codex/AGENTS.md` for Codex), and let `devicelease help` carry the details, for example:

```markdown
## Claim an Android device before using it

Devices and emulators on this machine are shared between agent sessions. `devicelease` records which session holds which device, and hooks block device commands on a device this session has not claimed. Run `devicelease help` before your first device command in a session: it explains how to claim a device, name it in commands, run Gradle device tasks, wait for a taken device, and release it.

- Claim a device in its own command, never chained with its first use.
- Name the device in every command (`adb -s <serial>`, `--device=<serial>`).
- Never use a device another session holds. Pick a free one or queue for it, as `devicelease help` describes.
- Release the device when done.
```

Codex and opencode only get these instructions. Nothing blocks them, because the hooks exist in Claude Code only.

### Scripts and other tools

Any script can take part: claim with a fixed owner name, and use `check` or `exec`.

```sh
export DEVICELEASE_OWNER=nightly-e2e
devicelease claim --any --emulator --note "nightly run"
devicelease exec -- ./gradlew connectedAndroidTest
devicelease release --all
```

## Files

| Path | Contents |
|---|---|
| `~/.local/state/devicelease/claims/` | One JSON file per claimed device (`DEVICELEASE_STATE_DIR` moves it). |
| `~/.local/state/devicelease/queue/` | One JSON file per queue entry. |
| `~/.local/state/devicelease/logs/` | Emulator boot logs, `agent-device close` calls, queue messages (`notify.log`) and the watcher's output. |
| `~/.config/devicelease/policy.json` | The policy (`DEVICELEASE_POLICY` moves it). |

## Limits

- Claims are advisory between cooperating tools. Android Studio, or any process that skips the hooks, can still use any device.
- A claim is created atomically, so two sessions can never both win a device. Expiry is the safety net for sessions that crash.

## Development

```sh
PYTHONPATH=src:tests python3 -m unittest discover -s tests
```

[docs/plan.md](docs/plan.md) holds the design and the phases, and [docs/queue-plan.md](docs/queue-plan.md) the queue's.
