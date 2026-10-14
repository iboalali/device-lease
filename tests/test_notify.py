import fcntl
import json
import subprocess
import sys
import time
import unittest
from pathlib import Path

from devicelease.notify import _delivered
from test_queue import QueueBase

FAKE_CLAUDE = Path(__file__).resolve().parent / "fake_claude.py"


class DeliveredTest(unittest.TestCase):
    def events(self, to, result):
        return "\n".join(json.dumps(e) for e in [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "t1", "name": "SendMessage", "input": {"to": to}}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1",
                 "content": [{"type": "text", "text": json.dumps(result)}]}]}},
        ])

    def test_success_to_the_address(self):
        self.assertTrue(_delivered(self.events("uds:/a.sock", {"success": True}), "uds:/a.sock"))

    def test_failure_or_other_address(self):
        self.assertFalse(_delivered(self.events("uds:/a.sock", {"success": False}), "uds:/a.sock"))
        self.assertFalse(_delivered(self.events("uds:/b.sock", {"success": True}), "uds:/a.sock"))
        self.assertFalse(_delivered("not json\n", "uds:/a.sock"))


class NotifyTest(QueueBase):
    def setUp(self):
        super().setUp()
        self.calls = self.root / "claude-calls.jsonl"
        self.env.update(
            DEVICELEASE_CLAUDE=f"{sys.executable} {FAKE_CLAUDE}",
            DEVICELEASE_NOTIFY_BACKOFF_SECONDS="0",
            FAKE_CLAUDE_LOG=str(self.calls),
        )

    def claude_calls(self):
        if not self.calls.exists():
            return []
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def claim_of(self, device):
        return next(c for c in self.status()["claims"] if c["device"] == device)

    def hand_to(self, *sessions):
        self.cli("claim", "uitest_api37", check=True)
        for session in sessions:
            self.register_session(session)
            self.cli("queue", "uitest_api37", owner=f"session:{session}", check=True)
        self.cli("release", check=True)

    def test_watcher_messages_the_session(self):
        self.hand_to("s-1")
        self.assertEqual(self.claim_of("uitest_api37")["notifySession"], "s-1")
        self.cli("watch", "--once", check=True)

        [call] = self.claude_calls()
        for flag in ("-p", "--no-session-persistence"):
            self.assertIn(flag, call["argv"])
        argv = " ".join(call["argv"])
        self.assertIn("--model haiku", argv)
        self.assertIn("--permission-mode manual", argv)
        self.assertIn("--tools SendMessage", argv)
        self.assertIn("--allowedTools SendMessage", argv)
        self.assertIn("--strict-mcp-config", argv)
        self.assertIn("to` set to exactly uds:/run/s-1.sock and", call["prompt"])
        self.assertIn("uitest_api37 is free and now yours (serial emulator-5554)", call["prompt"])
        claim = self.claim_of("uitest_api37")
        self.assertEqual((claim["notifySession"], claim["handoffPending"]), (None, False))
        self.assertNotIn("handed", self.cli("serial", owner="session:s-1", check=True).stderr)

    def test_bypass_session_gets_a_bypass_sender(self):
        self.cli("claim", "uitest_api37", check=True)
        self.register_session("s-1")
        data = {"session_id": "s-1", "cwd": str(self.workdir),
                "permission_mode": "bypassPermissions"}
        subprocess.run([sys.executable, "-m", "devicelease", "hook", "heartbeat"],
                       input=json.dumps(data), env=self.env, text=True, check=True, timeout=30)
        self.cli("queue", "uitest_api37", owner="session:s-1", check=True)
        self.cli("release", check=True)
        self.cli("watch", "--once", check=True)
        [call] = self.claude_calls()
        self.assertIn("--permission-mode bypassPermissions", " ".join(call["argv"]))

    def test_ended_session_passes_the_device_on(self):
        self.hand_to("s-1", "s-2")
        (self.registry / "s-1.json").unlink()
        self.cli("watch", "--once", check=True)
        self.assertEqual(self.claude_calls(), [])
        claim = self.claim_of("uitest_api37")
        self.assertEqual((claim["owner"], claim["notifySession"]), ("s-2", "s-2"))

    def test_failed_send_keeps_the_claim_and_the_notice(self):
        self.env["FAKE_CLAUDE_RESULT"] = "fail"
        self.hand_to("s-1")
        self.cli("watch", "--once", check=True)
        self.assertEqual(len(self.claude_calls()), 3)
        claim = self.claim_of("uitest_api37")
        self.assertEqual((claim["owner"], claim["notifySession"], claim["handoffPending"]),
                         ("s-1", None, True))
        log = (self.state / "logs" / "notify.log").read_text()
        self.assertIn("API Error: rate limited", log)

    def test_no_message_once_the_session_was_told(self):
        self.hand_to("s-1")
        self.assertIn("handed to you", self.cli("list", owner="session:s-1", check=True).stderr)
        self.cli("watch", "--once", check=True)
        self.assertEqual(self.claude_calls(), [])

    def test_watcher_hands_over_stale_claims(self):
        self.cli("claim", "uitest_api37", check=True)
        self.register_session("s-1")
        self.cli("queue", "uitest_api37", owner="session:s-1", check=True)
        self.edit_claim("uitest_api37", heartbeatAt="2000-01-01T00:00:00Z")
        self.cli("watch", "--once", check=True)
        self.assertEqual(self.holders(), {"uitest_api37": "s-1"})
        self.assertEqual(len(self.claude_calls()), 1)

    def test_queue_starts_a_watcher_that_delivers_and_exits(self):
        self.env["DEVICELEASE_WATCHER"] = "on"
        self.hand_to("s-1")
        deadline = time.monotonic() + 15
        while not self.claude_calls() or self.claim_of("uitest_api37")["notifySession"]:
            self.assertLess(time.monotonic(), deadline, "the watcher never delivered")
            time.sleep(0.1)
        self.assertEqual(len(self.claude_calls()), 1)
        lock = self.state / "queue" / ".watcher.lock"
        while True:
            with open(lock, "a") as f:
                try:
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    self.assertLess(time.monotonic(), deadline, "the watcher never exited")
            time.sleep(0.1)


if __name__ == "__main__":
    unittest.main()
