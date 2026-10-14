import os
from dataclasses import dataclass

from devicelease import t3

HUMAN = "me"
AGENT_NAMES = ("claude", "codex", "opencode")


@dataclass(frozen=True)
class Owner:
    id: str
    pid: int | None = None
    pid_start: int | None = None
    t3_thread: str | None = None
    aliases: tuple[str, ...] = ()
    claude_session: str | None = None

    @property
    def human(self) -> bool:
        return self.id == HUMAN

    @property
    def ids(self) -> tuple[str, ...]:
        return (self.id,) + self.aliases


def resolve_owner(explicit: str | None = None, env=None) -> Owner:
    env = os.environ if env is None else env
    if explicit:
        return Owner(explicit)
    # A Claude Code session outlives its process: the harness restarts `claude` and resumes the
    # same session, so the claim is never tied to a PID. Inside T3 Code the thread owns the claim,
    # and the claim ends when the thread is settled. The session ID stays an alias so claims made
    # before T3 Code recorded the session still count as this owner's.
    session = env.get("CLAUDE_CODE_SESSION_ID")
    if session:
        thread = t3.thread_for_session(session)
        if thread:
            return Owner(f"t3:{thread}", t3_thread=thread, aliases=(session,),
                         claude_session=session)
        return Owner(session, claude_session=session)
    named = env.get("DEVICELEASE_OWNER")
    if named:
        return Owner(named)
    pid = find_agent_ancestor()
    if pid is not None:
        return Owner(f"pid:{pid}", pid, process_start(pid))
    sid = os.getsid(0)
    return Owner(f"sid:{sid}", sid, process_start(sid))


def find_agent_ancestor() -> int | None:
    pid = os.getppid()
    while pid > 1:
        if _is_agent(pid):
            return pid
        parent = _parent(pid)
        if parent is None or parent == pid:
            return None
        pid = parent
    return None


def _is_agent(pid: int) -> bool:
    names = set()
    try:
        with open(f"/proc/{pid}/comm") as f:
            names.add(f.read().strip())
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            argv = f.read().split(b"\0")
    except OSError:
        return False
    # Node-based agents run as `node /path/to/codex ...`, so the script name counts too.
    for arg in argv[:2]:
        if arg:
            names.add(os.path.basename(arg.decode(errors="replace")))
    return any(name in AGENT_NAMES for name in names)


def _stat_fields(pid: int) -> list[str] | None:
    try:
        with open(f"/proc/{pid}/stat") as f:
            data = f.read()
    except OSError:
        return None
    # Field 2 (comm) may contain spaces and parentheses; everything after the last ")" is safe.
    return data.rsplit(")", 1)[1].split()


def _parent(pid: int) -> int | None:
    fields = _stat_fields(pid)
    return int(fields[1]) if fields else None


def process_start(pid: int) -> int | None:
    fields = _stat_fields(pid)
    return int(fields[19]) if fields else None


def process_alive(pid: int, start: int | None) -> bool:
    current = process_start(pid)
    if current is None:
        return False
    return start is None or current == start
