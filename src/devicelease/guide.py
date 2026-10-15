GUIDE = """\
devicelease: shared Android device claims for agent sessions

Android devices and emulators on this machine are shared between agent sessions.
devicelease records which session holds which device. In Claude Code, hooks block
device commands on a device your session has not claimed.

USE A DEVICE

  1. devicelease list
       Every device, its state and who holds it.
  2. devicelease claim <id>
       Claims one device, by AVD name or adb serial. `devicelease claim --any` takes
       the first free one, emulators before phones. `devicelease claim <avd> --boot`
       claims a stopped emulator and starts it with the host GPU. `--no-snapshot`
       instead of `--boot` cold boots it, without loading or saving the Quick Boot
       snapshot. Arguments after `--` go to the emulator and imply `--boot`:
       `devicelease claim <avd> -- -gpu auto` boots without the host GPU.
       Run the claim as its own command. Hooks check a whole command before any part
       of it runs, so `devicelease claim X && adb -s X ...` is still blocked.
  3. Name the device in every command:
       adb -s <serial> ...    android ... --device=<serial>    scrcpy -s <serial>
       `devicelease serial` prints the serial. An emulator gets a new serial on every
       boot, so ask for it instead of remembering it. A command that names no device
       only runs while your device is the only one online.
  4. devicelease exec -- ./gradlew connectedAndroidTest
       Runs Gradle device tasks (and anything else) with ANDROID_SERIAL set to your
       device. Without it, Gradle uses every connected device.
  5. devicelease release
       Frees the device when you are done.

  In T3 Code, device_open with a deviceId claims that device for the thread, and
  device_close releases it.

WHEN THE DEVICE IS TAKEN

  Never use a device another session holds. Claim a free one, or wait for it in the
  queue. When a device frees up, devicelease claims it for the first waiter in line
  and tells that waiter. How you wait depends on where you run:

  Claude Code inside T3 Code
      Run `devicelease queue <id> --wait` with Bash run_in_background, then end your
      turn. You are woken when the command exits:
        exit 0  "<id> is free and now yours (serial ...)". Use it.
        exit 3  "Still queued ...". It gives up after 115 minutes, below the 2-hour
                limit of a background command. Run the same command again to keep
                your place; the entry lapses 10 minutes after its last poll.
        exit 1  You are no longer queued: unqueued, or the thread ended.
  Claude Code in a terminal
      Run `devicelease queue <id>`, then keep working or end your turn. A message
      from devicelease arrives when the device is yours. `--wait` works here too.
  Codex, opencode, scripts
      Run `devicelease queue <id> --wait`. It blocks until the device is yours.

  `devicelease queue --any [--emulator|--physical] [--min-api N]` waits for the first
  matching device instead of a named one. `devicelease unqueue` leaves the queue.
  If you would rather not wait, boot a stopped emulator (`devicelease claim <avd>
  --boot`) or ask the user.

COMMANDS

  list [--json] [--watch [--interval SECONDS]]
      Every device adb and the AVD folder know about: kind, API level, state and lease
      (free, yours, taken by ..., not allowed here), and how many sessions wait for it.
      --watch redraws it every 2 seconds until Ctrl-C, for a terminal pane.
  claim <id> [--boot|--no-snapshot] [--note TEXT] [-- EMULATOR ARGS]
      Claims one device. Fails with the holder's name if the device is taken.
      --boot starts a stopped emulator with `-gpu host`. --no-snapshot cold boots it
      instead, without loading or saving the Quick Boot snapshot. Arguments after --
      go to the emulator and imply --boot. A `-gpu` there replaces `-gpu host`.
  claim --any [--emulator|--physical] [--min-api N] [--note TEXT]
      Claims the first free, allowed, running device that matches.
  queue <id> | --any [--emulator|--physical] [--min-api N]  [--wait [--timeout MIN]]
      Claims the device now if it is free, otherwise joins the queue for it. With
      --wait it polls until the device is yours (exit 0) or the timeout passes
      (exit 3, default 115 minutes). Ctrl-C leaves the queue.
  unqueue [<id> | --any | --all]
      Leaves the queue. Without an argument it leaves your only entry.
  release [<id> | --all]
      Frees your claim. Without an ID it frees your only claim.
  release --force <id> [--yes]
      Frees another session's claim, after a confirmation. Only when the user asks.
  status [--json] [--watch [--interval SECONDS]]
      Every claim, including stale ones, and every queue entry. --watch redraws it every
      2 seconds until Ctrl-C.
  whoami [--json]
      Your owner ID, project, policy and the devices you may use.
  serial
      The current adb serial of your single claimed device.
  check <id or serial>
      Exits 0 if you hold the device, 1 otherwise.
  exec [<id>] -- <command>
      Runs a command with ANDROID_SERIAL set to your device.
  heartbeat
      Refreshes your claims. The Claude Code hooks call it for you.
  gc
      Removes stale claims, and cleans the state folder: rotates logs over 5 MiB,
      empties a running emulator's log over 50 MiB, deletes boot logs of emulators
      unused for 7 days and broken or leftover files older than an hour. Sweeps do
      the cleaning too, at most once an hour.
  watch [--once]
      Hands devices to queued Claude Code sessions and messages them. `queue` starts
      it in the background; you never need to run it.
  hook <name>
      Claude Code hook handlers, installed by scripts/install-claude-hooks.
  help [<command>]
      This guide, or the options of one command.

  Every command takes --owner <name>, and most take --json.

WHO OWNS A CLAIM, AND WHEN IT ENDS

  Claude Code inside T3 Code   the thread, until it is settled, archived or deleted
  Claude Code in a terminal    the session, until it exits or 30 minutes pass
                               without a heartbeat
  DEVICELEASE_OWNER=<name>     that name, until 30 minutes pass without a heartbeat
  Codex, opencode              the agent process, until it exits or 30 minutes pass
                               without a heartbeat
  --owner me                   you, by hand, until the phone is unplugged or the
                               emulator closes

  A queue entry ends with its owner, with `unqueue`, or 10 minutes after the last
  poll of its `--wait`.

POLICY

  ~/.config/devicelease/policy.json limits which devices each project may use, and a
  repository's .devicelease.json can narrow it further. A device that is not allowed
  shows as "not allowed here" in `devicelease list`. `devicelease whoami` shows which
  policy applies.

FILES AND ENVIRONMENT

  ~/.local/state/devicelease/claims/   one JSON file per claimed device
  ~/.local/state/devicelease/queue/    one JSON file per queue entry
  ~/.local/state/devicelease/logs/     emulator boots, agent-device calls, messages

  DEVICELEASE_OWNER          owner name for scripts and tools outside Claude Code
  DEVICELEASE_STATE_DIR      moves ~/.local/state/devicelease
  DEVICELEASE_POLICY         moves the policy file
  ANDROID_HOME, ANDROID_SDK_ROOT, DEVICELEASE_ADB, DEVICELEASE_EMULATOR
                             where to find the Android SDK, adb and emulator
  DEVICELEASE_CLAUDE         the claude command that sends queue messages
  DEVICELEASE_WATCHER=off    never start the watcher
"""
