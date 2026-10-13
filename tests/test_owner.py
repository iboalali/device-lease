import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from devicelease.owner import resolve_owner
from fake_t3 import add_thread, create_db


class OwnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "state.sqlite"
        patcher = mock.patch.dict(os.environ, {"DEVICELEASE_T3_DB": str(self.db)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_explicit_owner_wins(self):
        owner = resolve_owner("me", {"CLAUDE_CODE_SESSION_ID": "s", "DEVICELEASE_OWNER": "x"})
        self.assertEqual(owner.id, "me")
        self.assertTrue(owner.human)

    def test_claude_session_is_not_tied_to_a_process(self):
        owner = resolve_owner(None, {"CLAUDE_CODE_SESSION_ID": "s", "CLAUDE_PID": "123",
                                     "DEVICELEASE_OWNER": "x"})
        self.assertEqual(owner.id, "s")
        self.assertIsNone(owner.pid)

    def test_t3_thread_owns_the_claim(self):
        create_db(self.db)
        add_thread(self.db, "thread-1", "session-1")
        owner = resolve_owner(None, {"CLAUDE_CODE_SESSION_ID": "session-1"})
        self.assertEqual(owner.id, "t3:thread-1")
        self.assertEqual(owner.t3_thread, "thread-1")
        self.assertEqual(owner.ids, ("t3:thread-1", "session-1"))

    def test_named_owner(self):
        owner = resolve_owner(None, {"DEVICELEASE_OWNER": "ci-job"})
        self.assertEqual(owner.id, "ci-job")
        self.assertFalse(owner.human)

    def test_fallback_is_a_live_process(self):
        owner = resolve_owner(None, {})
        self.assertRegex(owner.id, r"^(pid|sid):\d+$")
        self.assertIsNotNone(owner.pid_start)
        os.kill(owner.pid, 0)


if __name__ == "__main__":
    unittest.main()
