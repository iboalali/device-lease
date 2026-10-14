import argparse
import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from devicelease.cli import build_parser
from devicelease.guide import GUIDE
from fake_t3 import add_thread, create_db, set_thread

SRC = str(Path(__file__).resolve().parents[1] / "src")

DEVICES = [
    {"id": "0A1B2C3D4E", "kind": "physical", "serial": "0A1B2C3D4E", "state": "online",
     "name": "Pixel 8", "api": 36},
    {"id": "uitest_api37", "kind": "emulator", "serial": "emulator-5554", "state": "online",
     "name": "uitest api37", "api": 37},
    {"id": "uitest_api36", "kind": "emulator", "serial": "emulator-5556", "state": "online",
     "name": "uitest api36", "api": 36},
    {"id": "Pixel_6a", "kind": "emulator", "serial": None, "state": "stopped",
     "name": "Pixel 6a", "api": 34},
]


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.devices_file = root / "devices.json"
        self.set_devices(DEVICES)
        self.policy_file = root / "policy.json"
        self.workdir = root / "work"
        self.workdir.mkdir()
        self.env = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("CLAUDE", "DEVICELEASE_", "XDG_"))
        }
        self.env.update(
            PYTHONPATH=SRC,
            DEVICELEASE_FAKE_DEVICES=str(self.devices_file),
            DEVICELEASE_STATE_DIR=str(root / "state"),
            DEVICELEASE_POLICY=str(self.policy_file),
            DEVICELEASE_T3_DB=str(root / "t3.sqlite"),
            DEVICELEASE_CLAUDE_SESSIONS=str(root / "sessions"),
            DEVICELEASE_WATCHER="off",
            DEVICELEASE_CLAUDE="false",
        )

    def set_devices(self, devices):
        self.devices_file.write_text(json.dumps(devices))

    def run_cli(self, *args, owner="session-a", session=None, check=False):
        env = dict(self.env, DEVICELEASE_OWNER=owner)
        if session:
            env["CLAUDE_CODE_SESSION_ID"] = session
        result = subprocess.run([sys.executable, "-m", "devicelease", *args], cwd=self.workdir,
                                env=env, capture_output=True, text=True, timeout=30)
        if check and result.returncode != 0:
            self.fail(f"{args} failed: {result.stderr}")
        return result

    def run_json(self, *args, owner="session-a", session=None):
        result = self.run_cli(*args, "--json", owner=owner, session=session, check=True)
        return json.loads(result.stdout)

    def test_claim_and_release(self):
        claim = self.run_json("claim", "uitest_api37")
        self.assertEqual(claim["owner"], "session-a")
        self.assertEqual(claim["project"], str(self.workdir.resolve()))
        self.assertFalse(claim["alreadyHeld"])
        self.assertTrue(self.run_json("claim", "uitest_api37")["alreadyHeld"])

        taken = self.run_cli("claim", "uitest_api37", owner="session-b")
        self.assertEqual(taken.returncode, 1)
        self.assertIn("taken by session-a", taken.stderr)

        refused = self.run_cli("release", "uitest_api37", owner="session-b")
        self.assertEqual(refused.returncode, 1)
        self.assertIn("--force", refused.stderr)

        self.assertEqual([c["device"] for c in self.run_json("release")], ["uitest_api37"])
        self.assertEqual(self.run_json("claim", "uitest_api37", owner="session-b")["owner"],
                         "session-b")

    def test_claim_by_serial(self):
        self.assertEqual(self.run_json("claim", "emulator-5556")["device"], "uitest_api36")

    def test_list_shows_holders(self):
        self.run_cli("claim", "uitest_api37", check=True)
        self.run_cli("claim", "0A1B2C3D4E", owner="session-b", check=True)
        leases = {d["id"]: d["lease"] for d in self.run_json("list")}
        self.assertEqual(leases, {"uitest_api37": "yours", "0A1B2C3D4E": "taken",
                                  "uitest_api36": "free", "Pixel_6a": "free"})
        text = self.run_cli("list", check=True).stdout
        self.assertIn("taken by session-b", text)

    def test_policy_blocks_claim(self):
        self.policy_file.write_text(json.dumps({"deny": ["uitest_*"]}))
        result = self.run_cli("claim", "uitest_api37")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not allowed here: global deny list (uitest_*)", result.stderr)
        leases = {d["id"]: d["lease"] for d in self.run_json("list")}
        self.assertEqual(leases["uitest_api37"], "not-allowed")

    def test_claim_any_prefers_emulators(self):
        self.assertEqual(self.run_json("claim", "--any")["device"], "uitest_api36")
        self.assertEqual(self.run_json("claim", "--any", "--min-api", "37")["device"], "uitest_api37")
        self.assertEqual(self.run_json("claim", "--any")["device"], "0A1B2C3D4E")

    def test_claim_any_filters(self):
        self.assertEqual(self.run_json("claim", "--any", "--physical")["kind"], "physical")
        result = self.run_cli("claim", "--any", "--physical", owner="session-b")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Connect a device", result.stderr)

    def test_claim_any_with_nothing_free_explains_what_to_do(self):
        self.set_devices([DEVICES[3]])
        result = self.run_cli("claim", "--any")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Start an emulator from the Android Studio Device Manager", result.stderr)
        self.assertIn("or connect a device", result.stderr)
        self.assertIn("Emulators you may boot: Pixel_6a", result.stderr)

    def test_check_and_serial(self):
        self.run_cli("claim", "uitest_api37", check=True)
        self.assertEqual(self.run_cli("check", "emulator-5554").returncode, 0)
        self.assertEqual(self.run_cli("check", "uitest_api37").returncode, 0)
        self.assertEqual(self.run_cli("serial", check=True).stdout.strip(), "emulator-5554")

        other = self.run_cli("check", "emulator-5554", owner="session-b")
        self.assertEqual(other.returncode, 1)
        self.assertIn("taken by session-a", other.stderr)
        free = self.run_cli("check", "0A1B2C3D4E")
        self.assertEqual(free.returncode, 1)
        self.assertIn("devicelease claim", free.stderr)

    def test_exec_sets_android_serial(self):
        self.run_cli("claim", "uitest_api37", check=True)
        result = self.run_cli("exec", "--", "sh", "-c", "echo $ANDROID_SERIAL", check=True)
        self.assertEqual(result.stdout.strip(), "emulator-5554")

    def test_stopped_emulator_has_no_serial(self):
        self.run_cli("claim", "Pixel_6a", check=True)
        result = self.run_cli("serial")
        self.assertEqual(result.returncode, 1)
        self.assertIn("--boot", result.stderr)

    def test_no_snapshot_needs_a_device_name(self):
        result = self.run_cli("claim", "--any", "--no-snapshot")
        self.assertEqual(result.returncode, 1)
        self.assertIn("need a device name", result.stderr)

    def test_expired_claim_is_taken_over(self):
        self.run_cli("claim", "uitest_api37", check=True)
        claim_file = Path(self.env["DEVICELEASE_STATE_DIR"]) / "claims" / "uitest_api37.json"
        data = json.loads(claim_file.read_text())
        data["heartbeatAt"] = "2000-01-01T00:00:00Z"
        claim_file.write_text(json.dumps(data))
        claim = self.run_json("claim", "uitest_api37", owner="session-b")
        self.assertEqual(claim["owner"], "session-b")

    def test_human_claim_ends_when_device_disconnects(self):
        self.run_cli("claim", "0A1B2C3D4E", "--owner", "me", check=True)
        self.run_cli("gc", check=True)
        self.assertEqual([c["device"] for c in self.run_json("status")["claims"]], ["0A1B2C3D4E"])
        self.set_devices(DEVICES[1:])
        removed = self.run_json("gc")["claims"]
        self.assertEqual([(c["device"], c["reason"]) for c in removed],
                         [("0A1B2C3D4E", "device disconnected")])

    def test_t3_thread_claim_ends_when_thread_is_settled(self):
        db = Path(self.env["DEVICELEASE_T3_DB"])
        create_db(db)
        self.run_cli("claim", "uitest_api37", session="session-1", check=True)
        add_thread(db, "11111111-2222-3333-4444-555555555555", "session-1")

        claim = self.run_json("claim", "uitest_api37", session="session-1")
        self.assertTrue(claim["alreadyHeld"])
        self.assertEqual(claim["owner"], "t3:11111111-2222-3333-4444-555555555555")
        self.assertIn("t3:11111111 (you)", self.run_cli("status", session="session-1").stdout)

        set_thread(db, "11111111-2222-3333-4444-555555555555", "settled_override", "settled")
        removed = self.run_json("gc")["claims"]
        self.assertEqual([(c["device"], c["reason"]) for c in removed],
                         [("uitest_api37", "thread settled")])

    def test_force_release_needs_confirmation(self):
        self.run_cli("claim", "uitest_api37", check=True)
        result = self.run_cli("release", "--force", "uitest_api37", owner="session-b")
        self.assertEqual(result.returncode, 1)
        self.assertIn("--yes", result.stderr)
        self.run_cli("release", "--force", "uitest_api37", "--yes", owner="session-b", check=True)
        self.assertEqual(self.run_json("status")["claims"], [])

    def test_concurrent_claim_any_never_double_assigns(self):
        devices = [
            {"id": f"emu_{i}", "kind": "emulator", "serial": f"emulator-{5554 + 2 * i}",
             "state": "online", "name": f"emu {i}", "api": 36}
            for i in range(5)
        ]
        self.set_devices(devices)

        def claim(i):
            return self.run_cli("claim", "--any", "--json", owner=f"session-{i}")

        with ThreadPoolExecutor(max_workers=20) as pool:
            results = list(pool.map(claim, range(20)))

        won = [json.loads(r.stdout)["device"] for r in results if r.returncode == 0]
        self.assertEqual(sorted(won), sorted(d["id"] for d in devices))
        self.assertEqual(sum(r.returncode == 1 for r in results), 15)
        owners = {c["device"]: c["owner"] for c in self.run_json("status")["claims"]}
        self.assertEqual(len(owners), 5)
        self.assertEqual(len(set(owners.values())), 5)

    def test_help_covers_every_command(self):
        commands = next(a for a in build_parser()._actions
                        if isinstance(a, argparse._SubParsersAction)).choices
        listed = {line.split()[0] for line in GUIDE.splitlines() if line.startswith("  ")
                  and not line.startswith("   ") and line.split()}
        self.assertEqual(sorted(set(commands) - listed), [])
        self.assertEqual(self.run_cli("help", check=True).stdout, GUIDE)

    def test_help_for_one_command(self):
        self.assertIn("--wait", self.run_cli("help", "queue", check=True).stdout)
        missing = self.run_cli("help", "nope")
        self.assertEqual(missing.returncode, 1)
        self.assertIn("no command nope", missing.stderr)


if __name__ == "__main__":
    unittest.main()
