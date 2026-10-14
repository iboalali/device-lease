import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from devicelease.owner import process_start
from fake_t3 import add_thread, create_db, set_thread

SRC = str(Path(__file__).resolve().parents[1] / "src")

DEVICES = [
    {"id": "0A1B2C3D4E", "kind": "physical", "serial": "0A1B2C3D4E", "state": "online",
     "name": "Pixel 8", "api": 36},
    {"id": "uitest_api37", "kind": "emulator", "serial": "emulator-5554", "state": "online",
     "name": "uitest api37", "api": 37},
    {"id": "uitest_api36", "kind": "emulator", "serial": "emulator-5556", "state": "online",
     "name": "uitest api36", "api": 36},
]


class QueueBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.devices_file = self.root / "devices.json"
        self.devices_file.write_text(json.dumps(DEVICES))
        self.state = self.root / "state"
        self.registry = self.root / "sessions"
        self.registry.mkdir()
        self.workdir = self.root / "work"
        self.workdir.mkdir()
        self.env = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("CLAUDE", "DEVICELEASE_", "XDG_", "ANDROID_SERIAL"))
        }
        self.env.update(
            PYTHONPATH=SRC,
            DEVICELEASE_FAKE_DEVICES=str(self.devices_file),
            DEVICELEASE_STATE_DIR=str(self.state),
            DEVICELEASE_POLICY=str(self.root / "policy.json"),
            DEVICELEASE_T3_DB=str(self.root / "t3.sqlite"),
            DEVICELEASE_CLAUDE_SESSIONS=str(self.registry),
            DEVICELEASE_WATCHER="off",
            DEVICELEASE_CLAUDE="false",
            DEVICELEASE_POLL_SECONDS="0.2",
        )

    def env_for(self, owner):
        """`owner` is a DEVICELEASE_OWNER name, or `session:<id>` for a Claude Code session."""
        if owner.startswith("session:"):
            return dict(self.env, CLAUDE_CODE_SESSION_ID=owner.split(":", 1)[1])
        return dict(self.env, DEVICELEASE_OWNER=owner)

    def cli(self, *args, owner="session-a", cwd=None, check=False):
        result = subprocess.run([sys.executable, "-m", "devicelease", *args],
                                cwd=cwd or self.workdir, env=self.env_for(owner),
                                capture_output=True, text=True, timeout=30)
        if check and result.returncode != 0:
            self.fail(f"{args} failed: {result.stderr}")
        return result

    def json(self, *args, owner="session-a", cwd=None):
        return json.loads(self.cli(*args, "--json", owner=owner, cwd=cwd, check=True).stdout)

    def start_waiting(self, *args, owner):
        process = subprocess.Popen([sys.executable, "-m", "devicelease", "queue", *args, "--wait"],
                                   cwd=self.workdir, env=self.env_for(owner),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        deadline = time.monotonic() + 10
        while not self.status()["queue"]:
            self.assertLess(time.monotonic(), deadline, "the waiter never queued")
            time.sleep(0.05)
        return process

    def register_session(self, session_id):
        """Adds a running Claude Code session to the fake registry, backed by this process."""
        pid = os.getpid()
        (self.registry / f"{session_id}.json").write_text(json.dumps({
            "pid": pid, "procStart": str(process_start(pid)), "sessionId": session_id,
            "name": f"name-{session_id}", "messagingSocketPath": f"/run/{session_id}.sock",
        }))

    def status(self):
        return self.json("status")

    def holders(self):
        return {c["device"]: c["owner"] for c in self.status()["claims"]}

    def edit_claim(self, device, **changes):
        path = self.state / "claims" / f"{device}.json"
        path.write_text(json.dumps({**json.loads(path.read_text()), **changes}))


class QueueTest(QueueBase):
    def test_taken_message_offers_the_queue(self):
        self.cli("claim", "uitest_api37", check=True)
        plain = self.cli("claim", "uitest_api37", owner="session-b").stderr
        self.assertIn("taken by session-a", plain)
        self.assertIn("devicelease claim --any", plain)
        self.assertIn("`devicelease queue uitest_api37 --wait`, which exits once", plain)

        self.register_session("s-1")
        claude = self.cli("claim", "uitest_api37", owner="session:s-1").stderr
        self.assertIn("queue for it with `devicelease queue uitest_api37`; a message", claude)

        create_db(Path(self.env["DEVICELEASE_T3_DB"]))
        add_thread(Path(self.env["DEVICELEASE_T3_DB"]), "thread-1", "s-2")
        t3 = self.cli("check", "uitest_api37", owner="session:s-2").stderr
        self.assertIn("with Bash run_in_background and end your turn", t3)

    def test_claim_any_offers_the_queue_when_matching_devices_are_taken(self):
        self.cli("claim", "uitest_api37", check=True)
        self.cli("claim", "uitest_api36", check=True)
        result = self.cli("claim", "--any", "--emulator", owner="session-b")
        self.assertEqual(result.returncode, 1)
        self.assertIn("`devicelease queue --any --emulator --wait`", result.stderr)

    def test_queue_claims_a_free_device_right_away(self):
        result = self.json("queue", "uitest_api37", "--wait")
        self.assertEqual(result["status"], "yours")
        self.assertEqual(self.holders(), {"uitest_api37": "session-a"})
        self.assertEqual(self.status()["queue"], [])
        again = self.cli("queue", "uitest_api37", "--wait", check=True).stdout
        self.assertEqual(again, "You already hold uitest_api37; nothing to wait for.\n")

    def test_queue_without_wait_needs_a_claude_code_session(self):
        self.cli("claim", "uitest_api37", check=True)
        result = self.cli("queue", "uitest_api37", owner="session-b")
        self.assertEqual(result.returncode, 1)
        self.assertIn("pass --wait", result.stderr)

    def test_release_hands_the_device_to_a_message_waiter(self):
        self.cli("claim", "uitest_api37", check=True)
        self.register_session("s-1")
        queued = self.json("queue", "uitest_api37", owner="session:s-1")
        self.assertEqual((queued["status"], queued["position"]), ("queued", 1))
        self.assertIn("1 waiting", self.cli("list", check=True).stdout)

        self.cli("release", check=True)
        self.assertEqual(self.holders(), {"uitest_api37": "s-1"})
        self.assertEqual(self.status()["queue"], [])

        first = self.cli("serial", owner="session:s-1", check=True)
        self.assertEqual(first.stdout.strip(), "emulator-5554")
        self.assertIn("was handed to you from the queue", first.stderr)
        self.assertNotIn("handed", self.cli("serial", owner="session:s-1", check=True).stderr)

    def test_first_in_line_wins_across_named_and_any_waiters(self):
        self.cli("claim", "uitest_api37", check=True)
        self.cli("claim", "uitest_api36", check=True)
        for session in ("s-1", "s-2", "s-3"):
            self.register_session(session)
        self.cli("queue", "--any", "--emulator", owner="session:s-1", check=True)
        self.cli("queue", "uitest_api37", owner="session:s-2", check=True)
        self.cli("queue", "uitest_api37", owner="session:s-3", check=True)
        self.assertIn("3rd in line", self.cli("queue", "uitest_api37", owner="session:s-3",
                                              check=True).stdout)

        self.cli("release", "uitest_api37", check=True)
        self.assertEqual(self.holders()["uitest_api37"], "s-1")
        self.cli("release", "uitest_api36", check=True)
        self.assertNotIn("uitest_api36", self.holders())
        self.cli("release", owner="session:s-1", check=True)
        self.assertEqual(self.holders(), {"uitest_api37": "s-2"})

    def test_hand_off_uses_the_waiters_policy(self):
        self.cli("claim", "uitest_api37", check=True)
        strict = self.root / "strict"
        strict.mkdir()
        (strict / ".devicelease.json").write_text(json.dumps({"deny": ["uitest_api37"]}))
        self.cli("claim", "uitest_api36", owner="other", check=True)
        self.cli("claim", "0A1B2C3D4E", owner="other", check=True)
        for session in ("s-1", "s-2"):
            self.register_session(session)
        self.cli("queue", "--any", owner="session:s-1", cwd=strict, check=True)
        self.cli("queue", "--any", owner="session:s-2", check=True)

        self.cli("release", "uitest_api37", check=True)
        self.assertEqual(self.holders()["uitest_api37"], "s-2")

    def test_entries_of_ended_sessions_are_skipped(self):
        self.cli("claim", "uitest_api37", check=True)
        self.register_session("s-1")
        self.register_session("s-2")
        self.cli("queue", "uitest_api37", owner="session:s-1", check=True)
        self.cli("queue", "uitest_api37", owner="session:s-2", check=True)
        (self.registry / "s-1.json").unlink()

        self.cli("release", check=True)
        self.assertEqual(self.holders(), {"uitest_api37": "s-2"})

    def test_settled_thread_leaves_the_queue(self):
        db = Path(self.env["DEVICELEASE_T3_DB"])
        create_db(db)
        add_thread(db, "thread-1", "s-1")
        self.cli("claim", "uitest_api37", check=True)
        waiter = self.start_waiting("uitest_api37", owner="session:s-1")
        set_thread(db, "thread-1", "settled_override", "settled")
        _, err = waiter.communicate(timeout=10)
        self.assertEqual(waiter.returncode, 1)
        self.assertIn("no longer queued", err)
        self.assertEqual(self.status()["queue"], [])

    def test_wait_returns_when_the_device_is_released(self):
        self.cli("claim", "uitest_api37", check=True)
        waiter = self.start_waiting("uitest_api37", owner="session-b")
        self.cli("release", check=True)
        out, err = waiter.communicate(timeout=10)
        self.assertEqual(waiter.returncode, 0, err)
        self.assertIn("1st in line", out)
        self.assertIn("uitest_api37 is free and now yours (serial emulator-5554)", out)
        self.assertEqual(self.holders(), {"uitest_api37": "session-b"})
        self.assertNotIn("handed", self.cli("serial", owner="session-b", check=True).stderr)

    def test_wait_takes_over_a_stale_claim(self):
        self.cli("claim", "uitest_api37", check=True)
        waiter = self.start_waiting("uitest_api37", owner="session-b")
        self.edit_claim("uitest_api37", heartbeatAt="2000-01-01T00:00:00Z")
        out, _ = waiter.communicate(timeout=10)
        self.assertEqual(waiter.returncode, 0)
        self.assertIn("now yours", out)

    def test_claim_does_not_jump_the_queue(self):
        self.cli("claim", "uitest_api37", check=True)
        self.register_session("s-1")
        self.cli("queue", "uitest_api37", owner="session:s-1", check=True)
        self.edit_claim("uitest_api37", heartbeatAt="2000-01-01T00:00:00Z")

        result = self.cli("claim", "uitest_api37", owner="session-c")
        self.assertEqual(result.returncode, 1)
        self.assertIn("taken by s-1", result.stderr)

    def test_wait_gives_up_at_the_timeout(self):
        self.cli("claim", "uitest_api37", check=True)
        result = self.cli("queue", "uitest_api37", "--wait", "--timeout", "0.01",
                          owner="session-b")
        self.assertEqual(result.returncode, 3)
        self.assertIn("Still queued for uitest_api37, 1st in line; run the same command again",
                      result.stdout)
        self.assertEqual(len(self.status()["queue"]), 1)

        again = self.cli("queue", "uitest_api37", "--wait", "--timeout", "0.01", owner="session-b")
        self.assertEqual(again.returncode, 3)
        self.assertEqual(len(self.status()["queue"]), 1)

    def test_stopped_wait_entry_lapses(self):
        self.cli("claim", "uitest_api37", check=True)
        self.cli("queue", "uitest_api37", "--wait", "--timeout", "0.01", owner="session-b")
        path = next((self.state / "queue").glob("*.json"))
        path.write_text(json.dumps({**json.loads(path.read_text()),
                                    "refreshedAt": "2000-01-01T00:00:00Z"}))
        self.cli("release", check=True)
        self.assertEqual(self.holders(), {})
        self.assertEqual(self.status()["queue"], [])

    def test_unqueue_ends_a_wait(self):
        self.cli("claim", "uitest_api37", check=True)
        waiter = self.start_waiting("uitest_api37", owner="session-b")
        self.assertIn("Left the queue for uitest_api37",
                      self.cli("unqueue", owner="session-b", check=True).stdout)
        _, err = waiter.communicate(timeout=10)
        self.assertEqual(waiter.returncode, 1)
        self.assertIn("no longer queued", err)

    def test_terminating_a_wait_leaves_the_queue(self):
        self.cli("claim", "uitest_api37", check=True)
        waiter = self.start_waiting("uitest_api37", owner="session-b")
        waiter.terminate()
        waiter.communicate(timeout=10)
        self.assertEqual(waiter.returncode, 130)
        self.assertEqual(self.status()["queue"], [])

    def test_unqueue_needs_a_target_when_queued_twice(self):
        self.cli("claim", "uitest_api37", check=True)
        self.cli("claim", "0A1B2C3D4E", check=True)
        self.register_session("s-1")
        self.cli("queue", "uitest_api37", owner="session:s-1", check=True)
        self.cli("queue", "0A1B2C3D4E", owner="session:s-1", check=True)
        result = self.cli("unqueue", owner="session:s-1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("uitest_api37, 0A1B2C3D4E", result.stderr)
        self.cli("unqueue", "emulator-5554", owner="session:s-1", check=True)
        self.assertEqual([e["target"] for e in self.status()["queue"]], ["0A1B2C3D4E"])
        self.cli("unqueue", "--all", owner="session:s-1", check=True)
        self.assertEqual(self.status()["queue"], [])

    def test_status_lists_the_queue(self):
        self.cli("claim", "uitest_api37", check=True)
        self.register_session("s-1")
        self.cli("queue", "--any", "--min-api", "37", owner="session:s-1", check=True)
        text = self.cli("status", check=True).stdout
        self.assertIn("WAITING FOR", text)
        self.assertIn("any API 37+ device", text)

    def test_heartbeat_hook_reports_a_hand_off_once(self):
        self.cli("claim", "uitest_api37", check=True)
        self.register_session("s-1")
        self.cli("queue", "uitest_api37", owner="session:s-1", check=True)
        self.cli("release", check=True)

        def heartbeat():
            data = {"session_id": "s-1", "cwd": str(self.workdir)}
            result = subprocess.run([sys.executable, "-m", "devicelease", "hook", "heartbeat"],
                                    input=json.dumps(data), env=self.env, capture_output=True,
                                    text=True, timeout=30)
            return json.loads(result.stdout) if result.stdout else None

        context = heartbeat()["hookSpecificOutput"]["additionalContext"]
        self.assertIn("uitest_api37 (serial emulator-5554) was handed to you", context)
        self.assertIsNone(heartbeat())

    def test_session_end_leaves_the_queue(self):
        self.cli("claim", "uitest_api37", check=True)
        self.register_session("s-1")
        self.cli("queue", "uitest_api37", owner="session:s-1", check=True)
        data = {"session_id": "s-1", "cwd": str(self.workdir)}
        subprocess.run([sys.executable, "-m", "devicelease", "hook", "session-end"],
                       input=json.dumps(data), env=self.env, text=True, check=True,
                       timeout=30)
        self.assertEqual(self.status()["queue"], [])


if __name__ == "__main__":
    unittest.main()
