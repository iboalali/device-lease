import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from devicelease import cleanup

SRC = str(Path(__file__).resolve().parents[1] / "src")
OLD = time.time() - 8 * 24 * 3600


class CleanupTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        patcher = mock.patch.dict(os.environ, {"DEVICELEASE_STATE_DIR": tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.logs = self.state / "logs"
        self.claims = self.state / "claims"
        self.queue = self.state / "queue"
        for folder in (self.logs, self.claims, self.queue):
            folder.mkdir()

    def file(self, path: Path, content="x", mtime=None) -> Path:
        path.write_text(content)
        if mtime:
            os.utime(path, (mtime, mtime))
        return path

    def test_rotate_keeps_one_copy(self):
        log = self.file(self.logs / "notify.log", "new")
        self.file(self.logs / "notify.log.1", "older")
        with mock.patch.object(cleanup, "LOG_LIMIT", 2):
            cleanup.rotate(log)
        self.assertFalse(log.exists())
        self.assertEqual((self.logs / "notify.log.1").read_text(), "new")
        cleanup.rotate(log)

    def test_small_log_is_kept(self):
        log = self.file(self.logs / "watcher.log", "a")
        with mock.patch.object(cleanup, "LOG_LIMIT", 2):
            self.assertEqual(cleanup.run(set()), [])
        self.assertTrue(log.exists())

    def test_boot_logs(self):
        running = self.file(self.logs / "Pixel_6a.log", "x" * 10)
        stale = self.file(self.logs / "Old_avd.log", mtime=OLD)
        recent = self.file(self.logs / "Recent_avd.log")
        with mock.patch.object(cleanup, "BOOT_LOG_LIMIT", 5):
            done = dict(cleanup.run({"Pixel_6a"}))
        self.assertEqual(running.read_text(), "")
        self.assertEqual(done[str(running)], "emptied")
        self.assertFalse(stale.exists())
        self.assertTrue(recent.exists())

    def test_boot_logs_left_alone_without_discovery(self):
        stale = self.file(self.logs / "Old_avd.log", mtime=OLD)
        self.assertEqual(cleanup.run(None), [])
        self.assertTrue(stale.exists())

    def test_leftover_and_broken_files(self):
        hour_ago = time.time() - 2 * 3600
        leftover = self.file(self.claims / ".tmp-1-abc", mtime=hour_ago)
        fresh = self.file(self.claims / ".tmp-2-def")
        broken = self.file(self.queue / "abc.json", "{not json", mtime=hour_ago)
        missing_keys = self.file(self.claims / "Pixel_6a.json", "{}", mtime=hour_ago)
        valid = self.file(self.claims / "uitest.json", json.dumps({
            "device": "uitest", "kind": "emulator", "owner": "a", "project": "/p",
            "createdAt": "2026-01-01T00:00:00Z", "heartbeatAt": "2026-01-01T00:00:00Z",
        }), mtime=hour_ago)
        done = dict(cleanup.run(set()))
        self.assertFalse(leftover.exists())
        self.assertTrue(fresh.exists())
        self.assertFalse(broken.exists())
        self.assertFalse(missing_keys.exists())
        self.assertTrue(valid.exists())
        self.assertEqual(done[str(broken)], "deleted, unreadable")

    def test_old_session_records(self):
        sessions = self.state / "sessions"
        sessions.mkdir()
        old = self.file(sessions / "s-1.json", "{}", mtime=OLD)
        recent = self.file(sessions / "s-2.json", "{}")
        cleanup.run(set())
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())

    def test_due_after_an_hour(self):
        self.assertTrue(cleanup.due())
        cleanup.run(set())
        self.assertFalse(cleanup.due())
        stamp = self.state / ".cleanup"
        os.utime(stamp, (OLD, OLD))
        self.assertTrue(cleanup.due())


class CliCleanupTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        devices = root / "devices.json"
        devices.write_text(json.dumps([{"id": "uitest_api37", "kind": "emulator",
                                        "serial": "emulator-5554", "state": "online",
                                        "name": "uitest api37", "api": 37}]))
        self.state = root / "state"
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("CLAUDE", "DEVICELEASE_", "XDG_"))}
        self.env.update(PYTHONPATH=SRC, DEVICELEASE_OWNER="session-a",
                        DEVICELEASE_FAKE_DEVICES=str(devices),
                        DEVICELEASE_STATE_DIR=str(self.state),
                        DEVICELEASE_POLICY=str(root / "policy.json"),
                        DEVICELEASE_T3_DB=str(root / "t3.sqlite"),
                        DEVICELEASE_CLAUDE_SESSIONS=str(root / "sessions"),
                        DEVICELEASE_WATCHER="off", DEVICELEASE_CLAUDE="false")
        self.cwd = root

    def cli(self, *args, **kwargs):
        return subprocess.run([sys.executable, "-m", "devicelease", *args], cwd=self.cwd,
                              env=self.env, capture_output=True, text=True, timeout=30, **kwargs)

    def test_gc_reports_cleaned_files(self):
        self.cli("list")
        logs = self.state / "logs"
        logs.mkdir()
        old = logs / "Old_avd.log"
        old.write_text("x")
        os.utime(old, (OLD, OLD))
        result = json.loads(self.cli("gc", "--json").stdout)
        self.assertEqual(result["files"], [{"path": str(old),
                                            "action": "deleted, emulator not used for 7 days"}])
        self.assertIn("nothing to clean", self.cli("gc").stdout)

    def test_list_watch_redraws_until_interrupted(self):
        process = subprocess.Popen([sys.executable, "-m", "devicelease", "list", "--watch",
                                    "--interval", "0.2"], cwd=self.cwd, env=self.env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(2)
        process.send_signal(signal.SIGINT)
        out, _ = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0)
        self.assertGreater(out.count("devicelease list at"), 1)
        self.assertIn("uitest_api37", out)
        self.assertIn("--watch takes no --json", self.cli("list", "--watch", "--json").stdout)


if __name__ == "__main__":
    unittest.main()
