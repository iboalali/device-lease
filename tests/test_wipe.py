import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / "src")

DEVICES = [
    {"id": "pool_a", "kind": "emulator", "serial": None, "state": "stopped",
     "name": "pool a", "api": 36},
    {"id": "pool_b", "kind": "emulator", "serial": "emulator-5554", "state": "online",
     "name": "pool b", "api": 37},
    {"id": "own_avd", "kind": "emulator", "serial": "emulator-5556", "state": "online",
     "name": "own avd", "api": 37},
]


class WipeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = root = Path(self.tmp.name)
        self.workdir = root / "work"
        self.workdir.mkdir()
        self.state = root / "state"
        self.bin = root / "bin"
        self.bin.mkdir()
        self.devices_file = root / "devices.json"
        self.devices_file.write_text(json.dumps(DEVICES))
        self.policy_file = root / "policy.json"
        self.set_policy("echo $ANDROID_SERIAL > provisioned.txt")
        self.tool("emulator", f'echo "$@" >> {root}/emulator.log\n'
                              f'[ -f {root}/emulator-fails ] && exit 1\nexec sleep 3')
        self.tool("adb", f'echo "$@" >> {root}/adb.log\n'
                         'case "$*" in\n'
                         '  "devices -l") printf "List of devices attached\\n" ;\n'
                         f'    [ -f {root}/emulator-fails ] || echo "emulator-5560 device" ;;\n'
                         f'  *"emu avd name"*) printf "%s\\nOK\\n" "$(cat {root}/booting)" ;;\n'
                         '  *sys.boot_completed*) echo 1 ;;\n'
                         'esac')
        self.tool("notify-send", f'echo "$@" >> {root}/notify.log')
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("CLAUDE", "DEVICELEASE_", "XDG_", "ANDROID_"))}
        self.env.update(
            PYTHONPATH=SRC,
            PATH=f"{self.bin}:{os.environ['PATH']}",
            DEVICELEASE_FAKE_DEVICES=str(self.devices_file),
            DEVICELEASE_STATE_DIR=str(self.state),
            DEVICELEASE_POLICY=str(self.policy_file),
            DEVICELEASE_T3_DB=str(root / "t3.sqlite"),
            DEVICELEASE_CLAUDE_SESSIONS=str(root / "sessions"),
            DEVICELEASE_WATCHER="off",
            DEVICELEASE_CLAUDE="false",
            DEVICELEASE_EMULATOR=str(self.bin / "emulator"),
            DEVICELEASE_ADB=str(self.bin / "adb"),
        )

    def tool(self, name, body):
        path = self.bin / name
        path.write_text(f"#!/bin/sh\n{body}\n")
        path.chmod(0o755)

    def set_policy(self, after_wipe):
        self.policy_file.write_text(json.dumps({
            "wipeAfterUse": ["pool_*"],
            "devices": {"pool_a": {"home": str(self.workdir), "afterWipe": after_wipe}},
        }))

    def cli(self, *args, owner="session-a", booting=None):
        booting = booting or next((a for a in args if a.startswith(("pool_", "own_", "stopped_"))),
                                  None)
        if booting:
            (self.root / "booting").write_text(booting)
        env = dict(self.env, DEVICELEASE_OWNER=owner)
        return subprocess.run([sys.executable, "-m", "devicelease", *args], cwd=self.workdir,
                              env=env, capture_output=True, text=True, timeout=60)

    def ok(self, *args, owner="session-a", booting=None):
        result = self.cli(*args, owner=owner, booting=booting)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def log(self, name):
        path = self.root / f"{name}.log"
        return path.read_text().splitlines() if path.exists() else []

    def pending(self, avd):
        return (self.state / "wipe" / avd).exists()

    def failed(self, avd):
        path = self.state / "failed" / avd
        return json.loads(path.read_text()) if path.exists() else None

    def test_agent_release_stops_a_running_pool_emulator_and_marks_it(self):
        self.ok("claim", "pool_b")
        self.ok("release", "pool_b")
        self.assertTrue(self.pending("pool_b"))
        self.assertIn("-s emulator-5554 emu kill", self.log("adb"))
        listed = {d["id"]: d for d in json.loads(self.ok("list", "--json").stdout)}
        self.assertTrue(listed["pool_b"]["pool"])
        self.assertTrue(listed["pool_b"]["wipePending"])
        self.assertFalse(listed["own_avd"]["pool"])
        self.assertIn("wipe pending", self.ok("list").stdout)

    def test_human_and_non_pool_claims_are_not_wiped(self):
        self.ok("claim", "pool_b", owner="me")
        self.ok("release", "pool_b", owner="me")
        self.ok("claim", "own_avd")
        self.ok("release", "own_avd")
        self.assertFalse(self.pending("pool_b"))
        self.assertFalse(self.pending("own_avd"))
        self.assertEqual(self.log("adb"), [])

    def test_a_marked_emulator_is_not_handed_out(self):
        self.ok("claim", "pool_b")
        self.ok("release", "pool_b")
        result = self.cli("claim", "--any", "--emulator", owner="session-b")
        self.assertIn('"own_avd"', self.cli("status", "--json").stdout)
        self.assertNotIn("pool_b", result.stdout)

    def test_the_next_boot_wipes_cold_and_runs_after_wipe(self):
        self.ok("claim", "pool_a")
        self.ok("release", "pool_a")
        self.assertTrue(self.pending("pool_a"))
        self.ok("claim", "pool_a", "--boot", owner="session-b")
        self.assertEqual(self.log("emulator")[-1].split(),
                         ["-avd", "pool_a", "-gpu", "host", "-no-snapshot", "-wipe-data"])
        self.assertEqual((self.workdir / "provisioned.txt").read_text().strip(), "emulator-5560")
        self.assertFalse(self.pending("pool_a"))

    def test_a_pool_emulator_always_cold_boots(self):
        self.ok("claim", "pool_a", "--boot")
        self.assertEqual(self.log("emulator")[-1].split(),
                         ["-avd", "pool_a", "-gpu", "host", "-no-snapshot"])
        self.assertFalse((self.workdir / "provisioned.txt").exists())

    def test_failed_after_wipe_takes_the_emulator_out_of_rotation(self):
        self.set_policy("exit 3")
        self.ok("claim", "pool_a")
        self.ok("release", "pool_a")
        result = self.cli("claim", "pool_a", "--boot", owner="session-b")
        self.assertEqual(result.returncode, 1)
        self.assertIn("pool_a failed to run its afterWipe step: `exit 3` exited with 3",
                      result.stderr)
        self.assertIn("Tell the user that pool_a failed to run its afterWipe step", result.stderr)
        self.assertIn("is the emulator for", result.stderr)
        self.assertEqual(self.failed("pool_a")["what"], "run its afterWipe step")
        self.assertTrue(Path(self.failed("pool_a")["log"]).exists())
        self.assertNotIn('"pool_a"', self.cli("status", "--json").stdout)
        notified = self.log("notify")
        self.assertEqual(len(notified), 1)
        self.assertIn("pool_a failed to run its afterWipe step", notified[0])

        refused = self.cli("claim", "pool_a", owner="session-c")
        self.assertIn("pool_a is out of rotation", refused.stderr)
        self.assertIn("out of rotation: failed to run its afterWipe step", self.ok("list").stdout)

        self.set_policy("true")
        self.ok("claim", "pool_a", "--boot", owner="me")
        self.assertIsNone(self.failed("pool_a"))
        self.assertFalse(self.pending("pool_a"))

    def test_failed_boot_of_a_pool_emulator_is_marked(self):
        (self.root / "emulator-fails").touch()
        result = self.cli("claim", "pool_a", "--boot")
        self.assertIn("Tell the user that pool_a failed to boot", result.stderr)
        self.assertEqual(self.failed("pool_a")["what"], "boot")

    def test_failed_boot_of_another_emulator_is_not_marked(self):
        (self.root / "emulator-fails").touch()
        self.set_policy("true")
        devices = DEVICES + [{"id": "stopped_own", "kind": "emulator", "serial": None,
                              "state": "stopped", "name": "stopped own", "api": 36}]
        self.devices_file.write_text(json.dumps(devices))
        result = self.cli("claim", "stopped_own", "--boot")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("Tell the user", result.stderr)
        self.assertIsNone(self.failed("stopped_own"))

    def test_wipe_restarts_the_held_pool_emulator(self):
        self.ok("claim", "pool_b")
        self.ok("wipe", booting="pool_b")
        self.assertIn("-s emulator-5554 emu kill", self.log("adb"))
        self.assertEqual(self.log("emulator")[-1].split(),
                         ["-avd", "pool_b", "-gpu", "host", "-no-snapshot", "-wipe-data"])
        self.assertFalse(self.pending("pool_b"))
        self.assertIn('"pool_b"', self.cli("status", "--json").stdout)

    def test_wipe_data_is_only_for_pool_emulators(self):
        self.ok("claim", "own_avd")
        self.assertIn("not a pool emulator", self.cli("wipe", "own_avd").stderr)
        result = self.cli("claim", "own_avd", "--", "-wipe-data")
        self.assertIn("-wipe-data only applies to pool emulators", result.stderr)
        self.ok("claim", "pool_a", "--", "-wipe-data")
        self.assertEqual(self.log("emulator")[-1].split(),
                         ["-avd", "pool_a", "-gpu", "host", "-no-snapshot", "-wipe-data"])

    def test_device_open_boots_a_pool_emulator_that_waits_for_its_wipe(self):
        self.ok("claim", "pool_b")
        self.ok("release", "pool_b")
        (self.root / "booting").write_text("pool_b")
        data = {"session_id": "session-b", "cwd": str(self.workdir),
                "tool_name": "mcp__t3-code__device_open", "tool_input": {"deviceId": "pool_b"}}
        result = subprocess.run([sys.executable, "-m", "devicelease", "hook", "pre-device-open"],
                                input=json.dumps(data), env=self.env, capture_output=True,
                                text=True, timeout=60)
        decision = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(decision["updatedInput"], {"deviceId": "emulator-5560"})
        self.assertEqual(self.log("emulator")[-1].split(),
                         ["-avd", "pool_b", "-gpu", "host", "-no-snapshot", "-no-window",
                          "-no-audio", "-wipe-data"])
        self.assertFalse(self.pending("pool_b"))

    def test_cleanup_keeps_the_log_of_a_failed_emulator(self):
        (self.root / "emulator-fails").touch()
        self.cli("claim", "pool_a", "--boot")
        kept = Path(self.failed("pool_a")["log"])
        os.utime(kept, (0, 0))
        (self.state / ".cleanup").unlink(missing_ok=True)
        self.ok("gc")
        self.assertTrue(kept.exists())


if __name__ == "__main__":
    unittest.main()
