import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from devicelease import claude_sessions
from devicelease.owner import process_start


class ClaudeSessionsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        env = {key: value for key, value in os.environ.items()
               if key not in ("CLAUDE_CONFIG_DIR", "DEVICELEASE_CLAUDE_SESSIONS")}
        env["HOME"] = str(self.home)
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def register(self, config_dir, session_id):
        registry = config_dir / "sessions"
        registry.mkdir(parents=True, exist_ok=True)
        pid = os.getpid()
        (registry / f"{pid}.json").write_text(json.dumps({
            "pid": pid, "procStart": str(process_start(pid)), "sessionId": session_id,
            "name": f"name-{session_id}", "messagingSocketPath": f"/run/{session_id}.sock",
        }))

    def test_finds_session_of_second_account_folder(self):
        (self.home / ".claude" / "sessions").mkdir(parents=True)
        self.register(self.home / ".claude-private", "s-private")

        session = claude_sessions.find("s-private")

        self.assertIsNotNone(session)
        self.assertEqual(session.address, "uds:/run/s-private.sock")
        self.assertIs(claude_sessions.is_alive("s-private"), True)

    def test_searches_config_dir_outside_home(self):
        config_dir = self.root / "elsewhere"
        self.register(config_dir, "s-elsewhere")
        os.environ["CLAUDE_CONFIG_DIR"] = str(config_dir)

        self.assertIs(claude_sessions.is_alive("s-elsewhere"), True)

    def test_override_lists_several_registries(self):
        self.register(self.root / "a", "s-a")
        self.register(self.root / "b", "s-b")
        os.environ["DEVICELEASE_CLAUDE_SESSIONS"] = os.pathsep.join(
            [str(self.root / "a" / "sessions"), str(self.root / "b" / "sessions")])

        self.assertIs(claude_sessions.is_alive("s-a"), True)
        self.assertIs(claude_sessions.is_alive("s-b"), True)

    def test_unknown_session_is_dead_when_a_registry_exists(self):
        self.register(self.home / ".claude", "s-work")

        self.assertIs(claude_sessions.is_alive("s-missing"), False)

    def test_state_is_unknown_without_any_registry(self):
        self.assertIsNone(claude_sessions.is_alive("s-any"))


if __name__ == "__main__":
    unittest.main()
