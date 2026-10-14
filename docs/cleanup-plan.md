# devicelease: cleanup and the live list

## Cleanup

Files in `~/.local/state/devicelease/` that grow or pile up, and what happens to them:

| What | Rule |
|---|---|
| Append-only logs (`notify.log`, `watcher.log`, `agent-device.log`) | Before a write, a log over 5 MiB is renamed to `<name>.1`, replacing the previous copy. At most 10 MiB per log. |
| Emulator boot logs (`logs/<avd>.log`) | Opened in append mode, so another process can trim them while the emulator writes. A running emulator's log over 50 MiB is emptied. The log of an AVD that is not running and was not written for 7 days is deleted. |
| Temp files (`.tmp-*` in `claims/` and `queue/`) | Deleted when older than 1 hour. A process that died mid-write leaves them. |
| Unreadable claim and queue files | Deleted when older than 1 hour. Every write is atomic, so such a file is broken for good, and it makes every command print a warning. |

- `devicelease gc` always runs the cleanup. Every sweep runs it too, at most once an hour: the mtime of `state/.cleanup` records the last run, so a sweep that is not due costs one `stat`.
- When device discovery fails, boot logs are left alone, because a running emulator cannot be told from a stopped one.
- `gc` prints what it removed or trimmed. `gc --json` prints `{"claims": [...], "files": [...]}`.

## Live list

`devicelease list --watch [--interval SECONDS]` redraws the `list` view in place every 2 seconds, for a terminal pane, until Ctrl-C. Each pass discovers devices again and sweeps, so stale claims disappear and hand-offs show. It takes no `--json`.

## Not planned

- Auto-claim on first use. Blocking an unclaimed device command is the behavior.
- iOS.
