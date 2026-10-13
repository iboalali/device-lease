# devicelease: shared device claims for agent sessions

## Goal

Every agent session on this machine (Claude Code in T3 Code or in a terminal, Codex, opencode) can see which Android devices exist, which are free, and which it may use. It claims a device for the length of its work, and the device is freed when the session ends. Per project or repo, an allow list limits the devices a session may use and a deny list blocks specific ones. T3 Code integration is optional and sits on top.

## Decisions (defaults, change any of them)

| Topic | Default | Reason |
|---|---|---|
| Language | Python 3.11 or newer, stdlib only | Hooks are short-lived processes; no build step, no dependencies. `sqlite3` is in the stdlib for the T3 lookup. |
| Source location | A clone of this repository, with `bin/devicelease` symlinked into `~/.local/bin` | Versioned and editable. |
| State location | `~/.local/state/devicelease/claims/` | One shared folder for all agents and tools. |
| Policy location | `~/.config/devicelease/policy.json`, plus optional `.devicelease.json` per repo | User file is the source of truth; a repo file can only narrow it. |
| Claim expiry | 30 minutes without a heartbeat | Frees devices from crashed or killed sessions. |
| No claim yet | Block and tell the agent to claim | Explicit claims are easier to reason about than silent auto-claims. Revisit after a week of use. |
| Platforms | Android only (physical devices and emulators) | iOS is not available on this Linux machine. The data model keeps a `platform` field so iOS can be added later. |

## Concepts

- **Device ID.** A physical device is identified by its adb serial (`0A1B2C3D4E`). An emulator is identified by its AVD name (`Pixel_6a`), because its serial (`emulator-5554`) changes on every boot. A booted emulator's AVD name comes from `adb -s emulator-NNNN emu avd name`.
- **Owner.** Inside T3 Code the owner is the thread, `t3:<thread id>`, found by matching `CLAUDE_CODE_SESSION_ID` against `provider_session_runtime.resume_cursor_json` in `~/.t3/userdata/state.sqlite` (read-only). The session ID stays an alias, so claims made before T3 Code recorded the session still belong to the thread. Outside T3 Code the owner is `CLAUDE_CODE_SESSION_ID` when present, which every Bash call and hook in Claude Code has. Otherwise it is `DEVICELEASE_OWNER` if set, otherwise `pid:<agent process id>` of the nearest agent ancestor process (codex, opencode), otherwise `sid:<session id>` of the calling shell. A human can claim with `--owner me`. A Claude Code claim is not tied to a process ID, because the harness restarts the `claude` process and resumes the same session (T3 Code does this between turns).
- **Project.** The `git rev-parse --show-toplevel` of the working directory, with T3 worktrees mapped back to the main repo root (via `git rev-parse --git-common-dir`). Optionally the T3 project, looked up through the session ID in `~/.t3/userdata/state.sqlite` (read-only).
- **Claim.** One JSON file per device in the claims folder: `{device, platform, kind, serial, owner, project, pid, pidStart, createdAt, heartbeatAt, seenOnline, t3Thread, note}`. It is written to a temporary file and hard-linked into place, which fails if the device already has a claim, so only one session can win a device and readers never see a half-written file. Every change to the claims folder also holds an `fcntl` lock on `claims/.lock`.
- **Expiry.** An agent claim is stale when `heartbeatAt` is older than the expiry, or when its recorded process is dead (`pidStart` guards against a reused PID). A T3 Code thread's claim has no heartbeat either: it holds until the thread is settled, archived or deleted (`projection_threads`), and falls back to the heartbeat when the database cannot be read. A human claim (`--owner me`) has no heartbeat: it holds until its device disconnects or its emulator closes. A human claim on an emulator that is not running yet becomes stale if it never comes online within the expiry. Any command may remove a stale claim before acting.

## Policy file

```json
{
  "expiryMinutes": 30,
  "deny": ["Pixel_rooted"],
  "projects": {
    "~/Projects/shop-app":   { "allow": ["0A1B2C3D4E", "shop_api36_*"] },
    "~/Projects/ui-tests": { "allow": ["uitest_*"], "deny": ["uitest_pixel"] }
  },
  "defaults": { "allow": ["*"] }
}
```

- Patterns are shell-style globs matched against the device ID and the device's display name.
- Resolution for a project: the global `deny` list wins, then the project's `deny`, then the project's `allow` (or `defaults.allow` if the project is not listed).
- A repo's `.devicelease.json` may add `allow` (intersected with the user policy) and `deny` (added). It can never make a device usable that the user policy blocks.
- Project keys may be a path or `t3:<project-id>`.

## CLI

| Command | Behavior |
|---|---|
| `devicelease list [--json]` | Every known device (adb devices plus `emulator -list-avds`), with state: `free`, `yours`, `taken (owner, project, since)`, `not allowed here`, `offline`. |
| `devicelease claim <id>` | Claims one device. Fails with the current owner if taken, or with the policy rule if not allowed. `--boot` starts an emulator after claiming it and records its serial. |
| `devicelease claim --any [--physical\|--emulator] [--min-api N]` | Claims the first free, allowed, running device that matches, emulators before physical devices. If none is free, it fails with: start an emulator from the Android Studio Device Manager (or `devicelease claim <avd> --boot`), or connect a device, and lists the emulators this session may boot. |
| `devicelease release [<id>\|--all]` | Frees this owner's claims. `--all` frees every claim of the current owner. `--force <id>` frees someone else's claim after a confirmation prompt. |
| `devicelease status [--json]` | All current claims, including stale ones, with owner and project. |
| `devicelease heartbeat` | Refreshes `heartbeatAt` on this owner's claims. |
| `devicelease whoami` | Prints the resolved owner, project and allowed devices. Useful for debugging. |
| `devicelease serial` | Prints the serial of this owner's single claimed device, or fails if it has zero or several. |
| `devicelease check <serial>` | Exit 0 if this owner holds the device, otherwise exit 1 with a message. Used by hooks and scripts. |
| `devicelease exec [<id>] -- <cmd>` | Runs a command with `ANDROID_SERIAL` set to the claimed device. Covers Gradle `connectedAndroidTest` and `installDebug`. |
| `devicelease gc` | Removes stale claims. |

Output is plain text by default and JSON with `--json`, so agents and hooks can both use it.

## Enforcement

### Claude Code hooks (user `~/.claude/settings.json`, applies in T3 Code and in the terminal)

| Event | Matcher | Action |
|---|---|---|
| `PreToolUse` | `Bash` | Parse the command for a device target: `adb -s X`, `adb` with no `-s`, `ANDROID_SERIAL=X`, `android ... --device=X`, `agent-device ... --serial X`, `emulator -avd X`, `./gradlew` tasks matching `connected*`/`install*`/`uninstall*`. If the target is not owned by this session, deny with a reason that tells the agent what to run (`devicelease list`, then `devicelease claim`). Commands that only read the device list (`adb devices`, `devicelease ...`) always pass. |
| `PreToolUse` | `mcp__t3-code__device_open` | Resolve the device from `deviceId`. Deny if policy blocks it or another owner has it. Otherwise claim it for this session. |
| `PostToolUse` | `mcp__t3-code__device_close` | Release the device. |
| `PostToolUse` | `Bash` and `mcp__t3-code__device_.*` | Run `devicelease heartbeat` (cheap, only touches this owner's files). |
| `SessionEnd` | any | `devicelease release --all` for this session. Skipped inside T3 Code, which restarts the `claude` process between turns of one thread; the thread's claims end when it is settled. |

The Bash parser is the riskiest part. It needs a test suite of real command lines, including pipes, `&&` chains, `cd x && ./gradlew ...` and `$(...)`. When the device comes from the shell (`adb -s $SERIAL`), the command is allowed if this session holds exactly one device, and denied otherwise. When a command names no device at all (`adb shell`, `./gradlew connectedAndroidTest`), the tool picks one itself, and Gradle uses every connected device. So it is allowed only when this session's device is the only one online; otherwise the denial names the exact `adb -s <serial>` or `devicelease exec -- ...` to use. The check runs before any part of the command, so a `devicelease claim` in the same command does not count yet.

### Other agents

Only Claude Code runs the hooks. Codex and opencode get the agent instructions below and nothing that enforces them. PATH wrappers for `adb` and `android` are left out: Google's `android` CLI installs itself into `~/.local/bin`, where a wrapper would have to go, Gradle talks to the adb server without the `adb` binary, and any caller using an absolute path skips a wrapper. An opencode plugin (`tool.execute.before`) could run the same `devicelease hook pre-bash` check; it is not built.

### Agent instructions

A section in each agent's global instructions (`~/.claude/AGENTS.md` or `CLAUDE.md` for Claude Code, `~/.config/opencode/AGENTS.md` for opencode, `~/.codex/AGENTS.md` for Codex):

> Before using any Android device or emulator, run `devicelease list` and claim one with `devicelease claim <id>` (or `--any`). Use only the device you claimed. Run `devicelease release <id>` when done. Use `devicelease exec -- ./gradlew ...` for Gradle device tasks.

The hooks enforce this for Claude Code anyway. The instruction stops agents from wasting turns on blocked commands.

## T3 Code integration (optional)

- `device_open` and `device_close` go through the claims via the hooks above, so the Device panel and `agent-device` share one lock with everything else.
- Releasing a claim that came from `device_open` (by `release`, `device_close`, session end or a settled thread) also runs `agent-device close --config <host config> --session t3-<hash>`. T3 Code's own `device_close` only removes the panel, so agent-device would otherwise keep its hold on the device. The session name is `t3-` plus the first 24 hex digits of `sha256(JSON.stringify([threadId, "local", deviceId]))`, and the config is `~/.t3/userdata/device/hosts/<same hash of "local">.json`, both taken from T3 Code's `AgentDeviceTarget`. The deviceId T3 Code hashes may be the one passed to `device_open` or the device's serial, so sessions for both are closed.
- The T3 project ID is used for policy when available (`t3:<uuid>` keys), so a policy can follow a T3 project across worktrees.
- T3 Code internals (the database schema, the session-name formula) are not public API. Every T3 lookup must fail open to the git-based project and log a warning, so a T3 update cannot break device work.

## Phases

### Phase 1: core CLI

- Repo skeleton, `pyproject.toml` with a `devicelease` entry point, symlink into `~/.local/bin`.
- Device discovery (adb, AVD list, AVD name of running emulators), claims store with atomic create and file locks, owner and project resolution, policy loading and matching.
- Commands: `list`, `claim`, `release`, `status`, `whoami`, `check`, `serial`, `heartbeat`, `gc`, `exec`.
- Unit tests for policy resolution, expiry, owner resolution. A concurrency test: 20 processes claim `--any` at once, and no device ends up with two owners.

Done when two Claude sessions in different terminals can each claim a different device, cannot claim each other's, and `list` shows both correctly.

### Phase 2: Claude Code hooks

- `devicelease hook pre-bash`, `hook pre-device-open`, `hook post-device-close`, `hook heartbeat`, `hook session-end` subcommands, reading the hook JSON from stdin.
- The Bash command parser with its table-driven test suite.
- Register the hooks in `~/.claude/settings.json` with `scripts/install-claude-hooks`.

Done when a session that has not claimed a device is blocked from `adb -s 0A1B2C3D4E shell ...` with a clear message, and a session's claims disappear after it exits.

### Phase 3: other agents

- Instructions in each agent's global `AGENTS.md`.
- No PATH wrappers and no opencode plugin (see "Other agents").

### Phase 4: T3 Code

- T3 project lookup from `state.sqlite` and `t3:` policy keys.
- `agent-device close` on release for T3-originated claims.
- Manual test in T3 Code: two threads, one device, second thread is denied in `device_open` and in `agent-device` commands.

### Phase 5: polish (later)

- `devicelease status --watch` for a terminal pane.
- Optional auto-claim on first use, if blocking turns out to be annoying.
- iOS, if a macOS host is added.

## Limits

- Android Studio, Codex, opencode and any process outside a Claude Code session skip the hooks. The design protects the paths agents normally use, not every process on the machine.
- Claims are advisory between cooperating tools. A determined process can ignore them.
- Expiry is the safety net for crashes. A session that is alive but idle for longer than the expiry loses its claim; the next device command fails with a message to reclaim.

## Open questions

1. Which projects get an allow list on day one, and which devices go on the global deny list?
