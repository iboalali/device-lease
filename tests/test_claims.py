import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import timedelta
from pathlib import Path
from unittest import mock

from devicelease.claims import Claim, Store, now, stale_reason
from devicelease.owner import process_start
from fake_t3 import add_thread, create_db, set_thread

EXPIRY = timedelta(minutes=30)


def make_claim(**overrides) -> Claim:
    at = now()
    values = dict(device="uitest_api37", kind="emulator", owner="session-a", project="/p",
                  created_at=at, heartbeat_at=at)
    values.update(overrides)
    return Claim(**values)


class StaleTest(unittest.TestCase):
    def test_fresh_claim_is_valid(self):
        self.assertIsNone(stale_reason(make_claim(), EXPIRY, set()))

    def test_heartbeat_expiry(self):
        claim = make_claim(heartbeat_at=now() - timedelta(minutes=31))
        self.assertIn("no heartbeat", stale_reason(claim, EXPIRY, set()))
        claim = make_claim(heartbeat_at=now() - timedelta(minutes=29))
        self.assertIsNone(stale_reason(claim, EXPIRY, set()))

    def test_dead_process(self):
        process = subprocess.Popen(["true"])
        start = process_start(process.pid)
        process.wait()
        claim = make_claim(pid=process.pid, pid_start=start)
        self.assertEqual(stale_reason(claim, EXPIRY, set()), "owner process exited")

    def test_live_process(self):
        claim = make_claim(pid=os.getpid(), pid_start=process_start(os.getpid()))
        self.assertIsNone(stale_reason(claim, EXPIRY, set()))

    def test_reused_pid_counts_as_dead(self):
        claim = make_claim(pid=os.getpid(), pid_start=process_start(os.getpid()) - 1)
        self.assertEqual(stale_reason(claim, EXPIRY, set()), "owner process exited")

    def test_human_claim_ignores_heartbeat(self):
        claim = make_claim(owner="me", seen_online=True,
                           heartbeat_at=now() - timedelta(days=2))
        self.assertIsNone(stale_reason(claim, EXPIRY, {"uitest_api37"}))

    def test_human_claim_ends_when_device_goes_away(self):
        claim = make_claim(owner="me", seen_online=True)
        self.assertEqual(stale_reason(claim, EXPIRY, set()), "emulator closed")
        claim = make_claim(owner="me", kind="physical", device="0A1B", seen_online=True)
        self.assertEqual(stale_reason(claim, EXPIRY, set()), "device disconnected")

    def test_human_claim_waits_for_device_to_come_online(self):
        claim = make_claim(owner="me")
        self.assertIsNone(stale_reason(claim, EXPIRY, set()))
        claim = make_claim(owner="me", created_at=now() - timedelta(minutes=31))
        self.assertEqual(stale_reason(claim, EXPIRY, set()), "device never came online")

    def test_human_claim_is_kept_when_discovery_failed(self):
        claim = make_claim(owner="me", seen_online=True)
        self.assertIsNone(stale_reason(claim, EXPIRY, None))


class T3StaleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "state.sqlite"
        patcher = mock.patch.dict(os.environ, {"DEVICELEASE_T3_DB": str(self.db)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_active_thread_ignores_heartbeat(self):
        create_db(self.db)
        add_thread(self.db, "thread-1", "session-1")
        claim = make_claim(t3_thread="thread-1", heartbeat_at=now() - timedelta(days=1))
        self.assertIsNone(stale_reason(claim, EXPIRY, set()))

    def test_settled_thread_ends_claim(self):
        create_db(self.db)
        add_thread(self.db, "thread-1", "session-1")
        set_thread(self.db, "thread-1", "settled_override", "settled")
        self.assertEqual(stale_reason(make_claim(t3_thread="thread-1"), EXPIRY, set()),
                         "thread settled")

    def test_falls_back_to_heartbeat_without_t3_code(self):
        claim = make_claim(t3_thread="thread-1", heartbeat_at=now() - timedelta(minutes=31))
        self.assertIn("no heartbeat", stale_reason(claim, EXPIRY, set()))
        self.assertIsNone(stale_reason(make_claim(t3_thread="thread-1"), EXPIRY, set()))


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name))

    def test_create_is_exclusive(self):
        self.assertTrue(self.store.create(make_claim(owner="a")))
        self.assertFalse(self.store.create(make_claim(owner="b")))
        self.assertEqual(self.store.read("uitest_api37").owner, "a")

    def test_round_trip(self):
        claim = make_claim(serial="emulator-5554", pid=12, pid_start=34, t3_thread="t",
                           note="hi")
        self.store.create(claim)
        self.assertEqual(self.store.read("uitest_api37"), claim)

    def test_serial_with_special_characters(self):
        claim = make_claim(device="adb-0A1B._adb-tls-connect._tcp/x:5555", kind="physical")
        self.assertTrue(self.store.create(claim))
        self.assertEqual([c.device for c in self.store.all()], [claim.device])

    def test_unreadable_claim_is_skipped(self):
        (Path(self.tmp.name) / "broken.json").write_text("{")
        self.store.create(make_claim())
        with redirect_stderr(io.StringIO()) as err:
            self.assertEqual(len(self.store.all()), 1)
        self.assertIn("broken.json", err.getvalue())


if __name__ == "__main__":
    unittest.main()
