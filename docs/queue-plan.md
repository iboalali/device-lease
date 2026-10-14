# devicelease: waiting for a taken device

## Goal

When a session wants a device another session holds, devicelease tells it what it can do: pick another device, or wait in a queue for this one. A queued session gets the device claimed for it as soon as it frees up, and is told so. A Claude Code session in a terminal gets a `SendMessage` from a headless `claude` run. A T3 Code thread waits with a background Bash command that exits when the device is its. A `devicelease help` command explains all of this, and every other command, in one place.

## Decisions

| Topic | Decision | Reason |
|---|---|---|
| Hand-off | When a device frees up, devicelease claims it for the first matching waiter, then notifies it | A bare "it's free" notice lets another session grab the device before the waiter reacts. |
| Order | First in, first out across all waiters, whether they named the device or used `--any` | Simple and fair. |
| T3 Code waiters | `devicelease queue <id> --wait`, run with Bash `run_in_background` | T3 Code has no supported way to post into a thread. A background command wakes the thread when it exits, and it keeps the thread's `claude` process alive while it runs, past T3 Code's 30-minute idle limit (see Phase 0). |
| Claude Code waiters (terminal) | `devicelease queue <id>`, then a headless `claude -p --model haiku` sends a `SendMessage` | Cross-session messaging is a supported Claude Code feature. The waiter can keep working or end its turn. Only for owners that are a Claude Code session ID, never for `t3:` owners. |
| Other waiters (Codex, opencode, scripts, `--owner me`) | `--wait` only | Nothing can message them. |
| Wait limit | `--wait` exits by itself after 1h55m with a "still queued, run it again" line | Bash `run_in_background` stops a command at 2 hours. One notification every 2 hours beats Monitor's re-arm every 30 minutes. |
| T3 Code message fallback | Not built | It would need T3 Code's internal `thread.turn.start` command on its local server. Revisit if T3 waiters turn out to be unreachable. A headless `SendMessage` also reaches a T3 thread while its `claude` process runs, so that is the first fallback to try. |
| Headless run | `claude -p --model haiku --permission-mode manual`, `SendMessage` as the only allowed tool | Cheap (about $0.007 and 5 seconds per message). Senders in `auto` and `manual` mode both arrive as `from-mode="prompting"` and are delivered directly, so the waiter's permission mode is not needed. |

## Concepts

- **Queue entry.** One JSON file per entry in `~/.local/state/devicelease/queue/`: `{id, owner, mode, target, kind, minApi, project, checkout, createdAt, refreshedAt, pid, pidStart, claudeSession, t3Thread, t3Project, note}`. Times carry microseconds, because the order of entries made within one second decides who is first. `target` is a device ID, or `null` for `--any`, with `kind` and `minApi` as its filters. `mode` is `wait` or `message`. Changes hold the same `claims/.lock` as the claims, so a hand-off and a claim cannot race.
- **Live entry.** A `wait` entry is live while its `refreshedAt` is younger than 10 minutes. The waiting command refreshes it on every poll, and the grace period leaves the agent time to re-run the command after the 1h55m exit. A `message` entry is live while its Claude Code session is running: its `~/.claude/sessions/<pid>.json` exists with a matching `sessionId`, and the pid's start time matches `procStart`. Every entry also ends when its owner would lose a claim: a settled, archived or deleted T3 thread, or `devicelease unqueue`. Dead entries are removed by the same sweep that removes stale claims.
- **Hand-off.** One function, run under the lock after every sweep and every release, and before every `claim`, so a direct claim cannot jump the queue for a device that has waiters. For each free, online device, it finds the oldest live entry that matches it and that the waiter's policy allows, claims the device for that owner, and removes the entry. The policy check uses the project, checkout and T3 project stored in the entry, so it applies the waiter's rules, not the caller's.

## CLI

| Command | Behavior |
|---|---|
| `devicelease queue <id>` | If the device is free, claims it and says so. If you already hold it, says so. Otherwise adds an entry and prints your place in line. For a Claude Code session it adds a `message` entry, starts the watcher (see "Noticing a free device") and says a message will arrive. For any other owner it fails and says to use `--wait`. |
| `devicelease queue --any [--emulator\|--physical] [--min-api N]` | The same, for the first matching device that frees up. |
| `... --wait [--timeout <minutes>]` | Adds a `wait` entry, then polls every 10 seconds until the device is claimed for you. Prints one line and exits 0: `emulator-5554 is free and now yours (serial emulator-5554)`. At the timeout (default 115) it prints `still queued for emulator-5554 (2nd in line); run the same command again to keep waiting` and exits 3. It exits 1 with a message if the entry ends for another reason (thread settled, `unqueue`). Ctrl-C or SIGTERM (a stopped background command) leaves the queue and exits 130. |
| `devicelease unqueue [<id>\|--all]` | Leaves the queue. Without an ID it leaves your only entry. |
| `devicelease list` | Claimed devices also show how many sessions wait for them, and your place if you are one: `taken by 3f2a1b9c (~/Projects/foo, since 12m), 2 waiting, you are 1st`. The holder sees `yours (since 4m), 1 waiting`. |
| `devicelease status` | Lists queue entries under the claims, with owner, target, mode, age and whether each is live. `--json` prints `{"claims": [...], "queue": [...]}`. |
| `devicelease help` | Prints the full guide for agents and people (see "Help"). `devicelease help <command>` prints that command's options. |

### The "taken" message

Wherever devicelease refuses a taken device (`claim`, `claim --any` when nothing is free, `check`, and the `pre-bash` and `pre-device-open` hook denials), the message ends with what to do next, worded for the caller:

> emulator-5554 is taken by 3f2a1b9c (~/Projects/foo, since 12m). Claim a free device (`devicelease list`, `devicelease claim --any`), or wait for this one: run `devicelease queue emulator-5554 --wait` with Bash `run_in_background` and end your turn; you are woken when it is yours.

For a Claude Code session in a terminal the second half is: `or wait for this one with devicelease queue emulator-5554; you get a message when it is yours`. For `claim --any` the queue command is `devicelease queue --any` with the same filters.

When a waiter's next device command or `devicelease` command finds a claim that was handed to it, the output says so once: `emulator-5554 was handed to you from the queue`.

## Noticing a free device

The CLI runs when a device frees by `release` (including `release --force`), by the `session-end` hook, and by `device_close`. Each of these runs the hand-off right away.

Nothing runs when a claim goes stale: a T3 thread is settled, a heartbeat times out, an owner process exits. Someone has to poll.

- `wait` waiters poll themselves. Each poll sweeps stale claims and runs the hand-off.
- `message` waiters get a watcher: `devicelease watch`, a detached background process that `queue` starts. A lock file (`queue/.watcher.lock`, held with `flock`) keeps it to one instance. It sweeps and hands off every 10 seconds, sends the messages, and exits when no `message` entry is left.

## Messaging a Claude Code session

After a hand-off to a `message` entry:

1. **Find the session** in `~/.claude/sessions/*.json` by its `sessionId`, before any model call. It is alive when the file exists and the pid's start time matches `procStart`. If it is gone, release the hand-off, drop the entry and hand the device to the next waiter.
2. **Send.** Run `claude -p --model haiku --permission-mode manual --output-format stream-json --verbose --no-session-persistence --allowedTools SendMessage`, from the devicelease state folder, with the prompt on stdin (`--allowedTools` takes a list and would swallow a prompt argument). The prompt gives `to` as `uds:` plus the session's `messagingSocketPath`, which is unique even when two sessions share a name, and the exact text: `emulator-5554 is free and now yours (serial emulator-5554). Release it with devicelease release when done.` The `claude` binary comes from `DEVICELEASE_CLAUDE`, else `PATH`.
3. **Read the outcome** from the `SendMessage` tool result in the output:
   - `"success": true`: the message is in the session's inbox. Done. A session that holds or refuses the message reports that later, after the headless run has exited, so devicelease cannot see it. The waiter then keeps the claim, finds it on its next command, and loses it through the normal heartbeat rule if it never comes back.
   - Any other result, or the run failed (not logged in, rate limit, timeout after 60 seconds): retry twice with backoff. Then keep the claim, as above.

Every send is logged to `~/.local/state/devicelease/logs/notify.log`.

## Help

`devicelease help` prints a self-contained guide, so an agent can learn the tool in one command and the denial messages can point to it. Sections:

1. What devicelease is, in two sentences.
2. The workflow: list, claim, name the device in every command, `exec` for Gradle, release.
3. When a device is taken: pick another, or queue. How to wait in T3 Code (background Bash, `--wait`, re-run on "still queued"), in a Claude Code terminal session (message), and anywhere else (`--wait`).
4. Every command with its options and one example each.
5. Who owns a claim and when a claim or queue entry ends.
6. What the hooks block, and why a claim in the same command as its first use does not count.
7. Files and environment variables.

The text lives in one module, and a test checks that every subcommand of the parser appears in it, so the guide cannot fall behind the CLI. The README's "Use" section and the agent instructions snippet point to `devicelease help`.

## Phases

### Phase 0: verify the unknowns

Done on 2026-10-14, from a T3 Code thread:

- A headless `claude -p --model haiku` sends a `SendMessage` to a local session. It works in `auto` and `manual` mode and takes under 5 seconds.
- `to` accepts a session's socket address, `uds:<messagingSocketPath>`. Session refs like `[597b3e]` cannot be derived from the registry and are not needed.
- The tool result only says the message reached the inbox. Held or refused messages are reported later, in a notice the headless run never sees.
- The message arrives as a `<cross-session-message>` in a busy session at its next tool round. In an idle session (here a T3 thread whose turn had ended) it starts a new turn.
- T3 Code stops idle `claude` processes after about 30 minutes: two idle threads left the registry within that time. A thread running a 45-minute background command stayed alive (same pid, about 44 minutes idle) and was woken when the command exited. So `--wait` in the background works in T3 Code.
- Untested: whether a receiver in `bypassPermissions` mode holds these messages. That needs a session started in that mode.

### Phase 1: queue and hand-off

- Queue store, live-entry rules, hand-off, `queue`, `unqueue`, `queue --wait`, the queue in `list` and `status`, the new "taken" messages, the "handed to you" notice.
- Tests: order across named and `--any` waiters, policy checked with the waiter's project, dead entries skipped, hand-off on release, `--wait` with a short timeout, concurrent queue and claim.

### Phase 2: messages for Claude Code sessions

- The watcher, the session lookup and the headless sender.
- Tests with a fake `claude` binary (through `DEVICELEASE_CLAUDE`) and a fake sessions folder, in the style of `tests/fake_t3.py`.

### Phase 3: help and docs

- `devicelease help` and its coverage test.
- README, `docs/plan.md` CLI table, the agent instructions snippet.
- The device section of the global `~/.claude/AGENTS.md` ("Claim an Android device before using it") shrinks to a mention of devicelease, the rules that matter on every device command, and a pointer to `devicelease help` for everything else. It changes only once `devicelease help` exists, so agents are never sent to a command that fails.

Done when: thread A holds a device, thread B in T3 Code queues with `--wait` in the background and ends its turn, A releases, and B wakes up holding the device. The same for a Claude Code terminal session, which gets a message. And `devicelease help` explains both.

## Limits

- If a `message` waiter's session holds or refuses the message, devicelease cannot tell. The claim is held for the waiter meanwhile, up to the heartbeat expiry.
- A T3 thread whose agent does not re-run `--wait` after a "still queued" exit loses its entry after 10 minutes, and T3 Code stops its idle `claude` process after about 30.
- Each message costs one small model call.
- Codex, opencode and scripts can only wait with `--wait`.
