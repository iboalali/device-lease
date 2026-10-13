import hashlib
import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from devicelease import t3
from fake_t3 import add_thread, create_db, install_agent_device, set_thread


class T3Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "state.sqlite"
        patcher = mock.patch.dict(os.environ, {"DEVICELEASE_T3_DB": str(self.db)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_without_t3_code(self):
        self.assertIsNone(t3.thread_for_session("session"))
        self.assertIsNone(t3.thread_end_reason("thread"))

    def test_session_maps_to_thread(self):
        create_db(self.db)
        add_thread(self.db, "thread-1", "session-1")
        self.assertEqual(t3.thread_for_session("session-1"), "thread-1")
        self.assertIsNone(t3.thread_for_session("session-2"))

    def test_thread_end_reasons(self):
        create_db(self.db)
        add_thread(self.db, "thread-1", "session-1")
        self.assertEqual(t3.thread_end_reason("thread-1"), "")
        set_thread(self.db, "thread-1", "settled_override", "settled")
        self.assertEqual(t3.thread_end_reason("thread-1"), "thread settled")
        set_thread(self.db, "thread-1", "archived_at", "2026-10-13T10:00:00Z")
        self.assertEqual(t3.thread_end_reason("thread-1"), "thread archived")
        set_thread(self.db, "thread-1", "deleted_at", "2026-10-13T10:00:00Z")
        self.assertEqual(t3.thread_end_reason("thread-1"), "thread deleted")
        self.assertEqual(t3.thread_end_reason("thread-2"), "thread no longer exists")

    def test_project_for_thread(self):
        create_db(self.db)
        add_thread(self.db, "thread-1", "session-1", project="project-9")
        self.assertEqual(t3.project_for_thread("thread-1"), ("project-9", "Title of project-9"))
        self.assertIsNone(t3.project_for_thread("thread-2"))

    def test_agent_device_session_matches_t3_code(self):
        # T3 Code hashes JSON.stringify([threadId, hostId, deviceId]), which has no spaces.
        expected = hashlib.sha256(b'["thread-1","local","emulator-5554"]').hexdigest()[:24]
        self.assertEqual(t3.agent_device_session("thread-1", "emulator-5554"), f"t3-{expected}")

    def test_close_agent_device_calls_the_launcher(self):
        calls = install_agent_device(self.db)
        t3.close_agent_device("thread-1", ["uitest_api37", "emulator-5554", "uitest_api37"],
                              Path(self.tmp.name) / "log.txt")
        config = self.db.parent / "device" / "hosts" / "25bf8e1a2393f1108d37029b.json"
        self.assertEqual(calls.read_text().splitlines(), [
            f"close --config {config} --session {t3.agent_device_session('thread-1', d)}"
            for d in ("uitest_api37", "emulator-5554")
        ])

    def test_close_agent_device_without_t3_code_does_nothing(self):
        t3.close_agent_device("thread-1", ["uitest_api37"], Path(self.tmp.name) / "log.txt")
        self.assertFalse((Path(self.tmp.name) / "log.txt").exists())

    def test_unreadable_database_fails_open(self):
        self.db.write_text("not a database")
        with mock.patch.object(t3, "_warned", False), redirect_stderr(io.StringIO()) as err:
            self.assertIsNone(t3.thread_for_session("session-1"))
            self.assertIsNone(t3.thread_end_reason("thread-1"))
        self.assertEqual(err.getvalue().count("T3 Code lookup failed"), 1)


if __name__ == "__main__":
    unittest.main()
