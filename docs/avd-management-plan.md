# devicelease: managing the emulators themselves

## Goal

The user decides which emulators exist on the machine. Agents use them, but do not create, delete or change them. The user keeps a fixed pool: one emulator per chosen API level, a few of the latest, and special-purpose ones such as a rooted image for one app. A busy emulator means waiting in the queue, never making a new one. Every pool emulator gets its user data wiped after each agent is done with it, so the next agent starts clean and the AVD folder does not keep growing. An emulator can belong to a home project: other projects can still use it, but only by name, and the home project goes first in its queue.

## Decisions

| Topic | Decision | Reason |
|---|---|---|
| What is guarded | Creating, deleting and moving AVDs; installing and removing system images; patching images (`rootAVD`); editing files under the AVD folder or `system-images/` | These add, remove or change devices. App installs and `adb pair`/`adb connect` stay under the existing claim rules, with no approval. |
| Default answer | The hook refuses, and tells the agent to claim a free emulator or queue for a busy one | An approval dialog most likely goes unseen or gets declined, and while it waits the thread stalls. |
| Escape hatch | When the user asked for it in the conversation, the agent repeats the command with `DEVICELEASE_USER_REQUESTED=1` in front. The hook then returns `"ask"`, and the T3 Code approval dialog shows what the command does | Verified on 2026-11-18: a PreToolUse `"ask"` shows T3 Code's dialog with the reason text, also in full-access mode. A settings `ask` rule did not prompt there. |
| Pool | `wipeAfterUse` patterns in `policy.json` | Only emulators the user marks are wiped. The user's own working AVDs are never touched. |
| What a wipe is | A factory reset of user data: the next boot runs with `-wipe-data` | No baseline snapshot to maintain. Setup comes from provisioning scripts (`afterWipe`). |
| When | Whenever an agent's claim on a pool emulator ends: `release`, `release --force`, session end, settled thread, `device_close`, stale heartbeat. Not for `--owner me` claims | Every end of an agent's work, and nothing the user does by hand. |
| How | The claim's end stops the emulator and marks it "wipe pending". The next boot through devicelease wipes it, then runs `afterWipe` | Stopping is quick and fits in a hook. The wipe happens at boot, which `-wipe-data` does in one step and which was verified on `BRC_api36_magisk`. At most one used data set per AVD stays on disk. |
| Snapshots | Pool emulators always cold boot (`-no-snapshot`) | Quick Boot snapshots hold the previous agent's state and add to the folder size. |
| Boot on hand-off | A wiped emulator is booted for the waiter that gets it, not kept warm | Idle emulators would use RAM all day. The cost is a cold boot per agent, about 1-2 minutes. |
| Home project | `devices.<id>.home` in `policy.json` | `claim --any` from other projects skips the device, and the home project's `--any` picks it first. The home project goes first in the device's queue, with no preemption. |
| Ephemeral AVDs | Not built | Waiting in the queue covers it for now. |
| Physical devices | Out of scope | No wipe and no cleanup on phones. |

## Policy

```json
{
  "wipeAfterUse": ["pool_*", "BRC_api36_magisk"],
  "devices": {
    "BRC_api36_magisk": {
      "home": "~/Projects/private/android/Basic-Root-Checker",
      "afterWipe": "scripts/emulator/provision-magisk.sh"
    }
  }
}
```

- `wipeAfterUse`: shell-style globs, matched like the allow and deny lists (against the device ID and its display name).
- `devices.<id>.home`: a project path, matched like a `projects` key: the main repository root, so a worktree counts as its repository.
- `devices.<id>.afterWipe`: a command run after each wiped boot, with `ANDROID_SERIAL` set to the emulator. A relative path is resolved against `home`. It runs in `home` when there is one. Its output goes to `~/.local/state/devicelease/logs/<id>.log`. A non-zero exit fails the boot, as described in "Failures".
- A repository's `.devicelease.json` cannot set any of these. They describe the machine, not one project.

## Guarding the emulators

### Bash hook (`pre-bash`)

The parser in `src/devicelease/bashparse.py` gets a new target kind, `inventory`, for:

| Command | Matched |
|---|---|
| `avdmanager` | `create avd`, `delete avd`, `move avd` |
| `android emulator` | `create`, `remove` |
| `sdkmanager` | any package argument starting with `system-images;`, with or without `--uninstall` |
| `rootAVD.sh`, `rootAVD.bat` | any run |
| `rm`, `mv`, `cp`, `ln`, `truncate`, `tee`, `sed -i`, redirections (`>`, `>>`) | a path argument under the AVD folder (`paths.avd_home()`) or under `<sdk>/system-images/` |

A matched command is denied, unless the command line starts with `DEVICELEASE_USER_REQUESTED=1`, which turns the denial into `"ask"`. The denial text:

> Agents don't create, delete or change emulators on this machine, and approval will most likely not come. Use an existing one: `devicelease claim --any` takes a free emulator, and `devicelease queue <id> --wait` waits for a busy one (`devicelease list` shows them all). Only if the user asked you for this change in this conversation, run the same command again with `DEVICELEASE_USER_REQUESTED=1` in front. That asks the user for approval.

The `"ask"` reason names the action and the requester: `Create AVD "x" from system-images;android-37;google_apis;x86_64, requested by thread <title> in ~/Projects/foo`.

Booting a pool emulator outside devicelease (`emulator -avd <pool AVD>`, `android emulator start <pool AVD>`) is denied as well, with a pointer to `devicelease claim <id> --boot`, so a wipe cannot be skipped.

### Write and Edit hook (`pre-write`)

A new `PreToolUse` hook for `Write|Edit|MultiEdit|NotebookEdit` applies the same rule to file paths under the AVD folder and `system-images/`. `DEVICELEASE_USER_REQUESTED` cannot be passed to these tools, so they are always denied, with the text above minus the escape hatch.

### Limits

Hooks only see what Claude Code runs. A script that calls `avdmanager` itself, or an agent outside Claude Code (Codex, opencode), gets past them. This stops the common case, an agent that makes a new emulator because the one it wanted was busy, not a deliberate bypass.

## Wipe

- **State.** One marker file per AVD in `~/.local/state/devicelease/wipe/<avd>`, written when a claim on a pool emulator ends. It survives restarts, so a crash between stop and boot does not skip the wipe.
- **Ending a claim.** Everywhere a claim ends (`_release`, `sweep`, `hand_off`'s stale replacement), a pool emulator owned by an agent gets: the marker, then `adb -s <serial> emu kill`. The kill does not wait. A `--owner me` claim ends without either.
- **Booting.** `boot_emulator` gets a `wipe` flag. A pool emulator boots with `-no-snapshot`, plus `-wipe-data` while its marker exists. Before starting, it waits up to 60 seconds for the previous emulator process of that AVD to exit, because a kill from the last release may still be in progress. It checks the process, not the AVD's `multiinstance.lock` file, which stays on disk after the emulator exits. After `sys.boot_completed`, it runs `afterWipe` when the boot wiped, then removes the marker. Every boot path uses it: `claim --boot`, the `pre-device-open` hook, and the hand-off boot below.
- **Shown to agents.** `devicelease list` shows `stopped, wipe pending` for a marked emulator, and `pool` / `home: <project>` in a new column.

## Restarting and wiping during a task

An agent may restart or wipe the emulator it holds, without giving up the claim:

| Action | How | Data |
|---|---|---|
| Reboot Android | `adb -s <serial> reboot` | Kept |
| Restart the emulator | `adb -s <serial> emu kill`, then `devicelease claim <id> --boot` | Kept. An agent's claim does not end when its emulator stops, and no wipe runs because the claim did not end. The serial may change. |
| Wipe | `devicelease wipe [<id>]` | Reset. Stops the emulator, boots it with `-wipe-data -no-snapshot`, runs `afterWipe`, prints the new serial. The claim stays. |

- `devicelease wipe` only works on a pool emulator the caller holds. A non-pool AVD is the user's own, so its data is not the agent's to reset.
- `-wipe-data` among `claim --boot` arguments (`claim <id> -- -wipe-data`, which passes through today) is rejected for non-pool AVDs, and for pool AVDs it does the same as `devicelease wipe`.
- A wipe of an emulator with `afterWipe` can take several minutes, so the help text tells agents to run it with Bash `run_in_background` or a long timeout.

## Failures

A boot that fails, a wipe that fails, or an `afterWipe` that exits non-zero is something the user has to know about, and the agent is the one talking to the user.

- **The message tells the agent to report it.** Every such error, from `claim --boot`, `wipe`, `queue --wait`, the watcher's message and the `pre-device-open` hook denial, ends with: `Tell the user that <id> failed to <boot | wipe | run its afterWipe step>, with this log: <path>. Don't retry it. If another emulator can do the work, you may continue on one (devicelease claim --any, or devicelease queue <id> --wait), and still tell the user about the failure.` The failed emulator is already out of rotation (below), so `claim --any` does not pick it again. The log path points to `~/.local/state/devicelease/logs/<id>.log`, which holds the emulator's and `afterWipe`'s output.
- **Home devices.** When the failed emulator has a home, the message also names it (`<id> is the emulator for ~/Projects/...`), so an agent that needs, for example, the rooted image knows a stock emulator is no substitute.
- **The emulator is marked failed.** A marker in `~/.local/state/devicelease/failed/<id>` records what failed, when, for whom, and the log path. The emulator stays stopped, and its claim is released so the agent is not left holding a dead device.
- **A failed emulator is out of rotation.** `hand_off` and `claim --any` skip it, so the queue does not hand it to each waiter in turn and fail every time. A named `claim` or `queue` of it is refused with the message above. Waiters queued for it by name are told the same, once, and stay queued.
- **A desktop notification as well.** The agent may not pass the message on, and a waiter's background `--wait` may fail while nobody watches the thread. So devicelease also sends one notification when it writes the failed marker: `notify-send --app-name=devicelease --icon=devicelease --hint=string:desktop-entry:devicelease --urgency=critical "<id> failed to <what>" "<owner and project>. Log: <path>"`. The icon and the hidden `devicelease.desktop` entry come from `scripts/install-desktop-entry`. A hook may run without `DBUS_SESSION_BUS_ADDRESS`, so devicelease falls back to `unix:path=/run/user/<uid>/bus`. A missing `notify-send` or a failed send is logged and never fails the command. `DEVICELEASE_NOTIFY=off` turns it off, for tests.
- **Only the user clears it.** `devicelease list` shows `failed: <what> (<age>), see <log>`. After fixing the cause, the user runs `devicelease claim <id> --boot --owner me`. A successful boot by the user removes the marker, and the queue moves again. Agent boots of a failed emulator are refused.

## Hand-off to stopped emulators

`hand_off` today only hands out online devices. A wiped pool emulator is stopped, so a queue waiting for it would never move. The change:

- `hand_off` also hands out stopped pool emulators. The claim is created as now, with a new `boot_pending` flag.
- The waiter's side boots it:
  - `queue --wait`: the waiting command boots it after the hand-off and before it exits 0. Its "is free and now yours" line then has a serial.
  - `message` waiters (Claude Code in a terminal): the watcher boots it, then sends the message.
  - T3 Code `device_open`: the `pre-device-open` hook already boots a stopped emulator.
  - `devicelease serial` and `exec` on a `boot_pending` claim say to run `devicelease claim <id> --boot`.
- A direct `claim <id>` of a stopped pool emulator with no waiters works as today, with `--boot` running the wipe.

## Home project

- `claim --any` and `queue --any` from a project other than the device's home skip it. A named `claim` or `queue` still gets it.
- `claim --any` from the home project sorts its home devices first.
- `hand_off`, for a device with a home: the oldest live entry from the home project wins, then the oldest of the rest. Today it is first in, first out across all entries.
- `policy.check` is unchanged. A home is a preference, not an allow list. To keep other projects off a device entirely, the `deny` lists still apply.

## Help and README

- `devicelease help` gets a section "Emulators on this machine": agents never create, delete or change them; pool emulators are wiped after each use; what `wipe pending` and `home` mean; and the escape hatch for a user's explicit request.
- README: the policy keys, the two new hooks, and the limits above.
- `scripts/install-claude-hooks` and `docs/claude-hooks.json` add the `pre-write` hook.

## Phases

0. **Verify.** Done on 2026-11-18, on throwaway copies of `e2e_api36` and `BRC_api36_magisk`:
   - `adb emu kill` on a Quick Boot emulator saves a snapshot on exit. For `e2e_api36` its `ram.bin` alone is 3.3 GB, more than the user data. This is the main reason pool emulators cold boot.
   - A `-wipe-data -no-snapshot` boot removes the old state (a marker file and a setting were gone) and deletes the whole `snapshots/` folder. The copy shrank from 6.1 GB to 965 MB. It took 39 seconds.
   - `adb emu kill` on a cold-booted emulator ends the process in 2 seconds and saves no snapshot.
   - `BRC_api36_magisk`: the wiped cold boot took 84 seconds and `provision-magisk.sh` 22 seconds, 106 seconds in total, well inside the `pre-device-open` hook's 300 seconds.
1. **Guard.** The `inventory` target, the denial and `"ask"` texts, the pool-boot denial, and the `pre-write` hook. Useful on its own, before any wipe exists.
2. **Wipe.** `wipeAfterUse`, the marker, stopping on claim end, the wiping boot with `afterWipe`, `devicelease wipe`, the `-wipe-data` check on `claim --boot` arguments, and the failed marker with its messages.
3. **Hand-off to stopped emulators.** `boot_pending` and the three waiter paths.
4. **Home projects.** `--any` filtering and sorting, and queue priority.
5. **Docs.** Help, README (including `scripts/install-desktop-entry` in the install steps), hook install.

## Tests

- Parser: every row of the guarded-command table, with and without `DEVICELEASE_USER_REQUESTED=1`, inside `sh -c`, `&&` chains and `if` bodies, and paths given through variables set in the same command.
- Hook: deny vs `"ask"` output, the pool-boot denial, and the `pre-write` hook on paths inside and outside the guarded folders.
- Wipe: a fake emulator (the existing `DEVICELEASE_FAKE_DEVICES` mechanism, plus a fake `emulator` and `adb` that log their arguments). A release writes the marker and kills; a `--owner me` release does neither; the next boot passes `-wipe-data`, runs `afterWipe`, and removes the marker; a failing `afterWipe` keeps the marker.
- Hand-off: a waiter gets a stopped pool emulator, and `--wait` boots it before exiting.
- Failures: a failed boot or `afterWipe` writes the failed marker, releases the claim, and returns the "tell the user" text, and sends one notification (a fake `notify-send` on `PATH` logs its arguments); `hand_off` and `claim --any` skip the emulator; agent boots are refused; a successful `--owner me` boot clears the marker.
- Home: `--any` skipping and preferring, and queue order with mixed home and non-home waiters.

## Open risks

- **Boot time in hooks.** The slowest pool emulator measured, `BRC_api36_magisk`, needs 106 seconds for a wiped boot with `afterWipe`, against the `pre-device-open` hook's 300. A slower `afterWipe` added later could still hit the limit. If one does, that hook claims and starts the boot, and tells the agent to wait for it with `devicelease queue <id> --wait`.
- **Booting outside devicelease.** Android Studio's Device Manager can still boot a pool emulator without the wipe. `list` shows `wipe pending` so it is visible, and the wipe happens on the next devicelease boot.
- **Agents that release early.** Releasing means "done". An agent that releases and claims again in the same task gets a wiped emulator. The help text says to keep the claim for the whole task.
