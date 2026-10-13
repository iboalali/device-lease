import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from devicelease.t3 import agent_device_session
from fake_t3 import (
    add_agent_device_session, add_thread, create_db, install_agent_device, set_thread,
)

SRC = str(Path(__file__).resolve().parents[1] / "src")

DEVICES = [
    {"id": "0A1B2C3D4E", "kind": "physical", "serial": "0A1B2C3D4E", "state": "online",
     "name": "Pixel 8", "api": 36},
    {"id": "uitest_api37", "kind": "emulator", "serial": "emulator-5554", "state": "online",
     "name": "uitest api37", "api": 37},
    {"id": "Pixel_6a", "kind": "emulator", "serial": None, "state": "stopped",
     "name": "Pixel 6a", "api": 34},
]


class HookTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.devices_file = root / "devices.json"
        self.set_devices(DEVICES)
        self.workdir = root / "work"
        self.workdir.mkdir()
        self.t3_db = root / "t3.sqlite"
        self.env = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("CLAUDE", "DEVICELEASE_", "XDG_", "ANDROID_SERIAL"))
        }
        self.env.update(
            PYTHONPATH=SRC,
            DEVICELEASE_FAKE_DEVICES=str(self.devices_file),
            DEVICELEASE_STATE_DIR=str(root / "state"),
            DEVICELEASE_POLICY=str(root / "policy.json"),
            DEVICELEASE_T3_DB=str(self.t3_db),
        )

    def set_devices(self, devices):
        self.devices_file.write_text(json.dumps(devices))

    def cli(self, *args, session="session-a"):
        env = dict(self.env, CLAUDE_CODE_SESSION_ID=session)
        return subprocess.run([sys.executable, "-m", "devicelease", *args], cwd=self.workdir,
                              env=env, capture_output=True, text=True, timeout=30)

    def hook(self, name, session="session-a", **payload):
        data = {"session_id": session, "cwd": str(self.workdir), **payload}
        result = subprocess.run([sys.executable, "-m", "devicelease", "hook", name],
                                input=json.dumps(data), env=self.env, capture_output=True,
                                text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return json.loads(result.stdout) if result.stdout else None

    def bash(self, command, session="session-a"):
        """Returns the deny reason, or None when the command may run."""
        output = self.hook("pre-bash", session, tool_name="Bash", tool_input={"command": command})
        if output is None:
            return None
        decision = output["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        return decision["permissionDecisionReason"]

    def holders(self):
        result = self.cli("status", "--json")
        return {c["device"]: c["owner"] for c in json.loads(result.stdout)}

    def test_unrelated_commands_pass(self):
        self.assertIsNone(self.bash("ls -la && git status"))
        self.assertIsNone(self.bash("adb devices"))
        self.assertIsNone(self.bash("./gradlew assembleDebug"))

    def test_unclaimed_device_is_denied(self):
        reason = self.bash("adb -s 0A1B2C3D4E shell ls")
        self.assertIn("you have not claimed 0A1B2C3D4E", reason)
        self.assertIn("devicelease claim", reason)

    def test_claim_in_the_same_command_is_explained(self):
        reason = self.bash("devicelease claim 0A1B2C3D4E && adb -s 0A1B2C3D4E shell ls")
        self.assertIn("Run `devicelease claim` on its own first", reason)

    def test_claimed_device_passes(self):
        self.cli("claim", "uitest_api37")
        self.assertIsNone(self.bash("adb -s emulator-5554 shell ls"))
        self.assertIsNone(self.bash("ANDROID_SERIAL=emulator-5554 ./gradlew installDebug"))
        self.assertIsNone(self.bash('adb -s "$(devicelease serial)" shell ls'))

    def test_other_sessions_device_is_denied(self):
        self.cli("claim", "uitest_api37", session="session-b")
        reason = self.bash("adb -s emulator-5554 shell ls")
        self.assertIn("taken by session-b", reason)

    def test_implicit_target_needs_a_named_device(self):
        self.cli("claim", "uitest_api37")
        reason = self.bash("adb shell ls")
        self.assertIn("does not name a device", reason)
        self.assertIn("adb -s emulator-5554", reason)
        reason = self.bash("cd app && ./gradlew connectedAndroidTest")
        self.assertIn("devicelease exec --", reason)

    def test_implicit_target_passes_when_only_own_device_is_online(self):
        self.set_devices(DEVICES[1:])
        self.cli("claim", "uitest_api37")
        self.assertIsNone(self.bash("adb shell ls"))
        self.assertIsNone(self.bash("adb -e shell ls"))

    def test_dynamic_target_needs_exactly_one_claim(self):
        self.assertIn("you hold no device", self.bash("adb -s $SERIAL shell"))
        self.cli("claim", "uitest_api37")
        self.assertIsNone(self.bash("adb -s $SERIAL shell"))

    def test_booting_an_emulator_needs_a_claim(self):
        self.assertIn("you have not claimed Pixel_6a", self.bash("emulator -avd Pixel_6a"))
        self.cli("claim", "Pixel_6a")
        self.assertIsNone(self.bash("emulator -avd Pixel_6a"))

    def test_device_open_claims_and_device_close_releases(self):
        self.assertIsNone(self.hook("pre-device-open", tool_name="mcp__t3-code__device_open",
                                    tool_input={"deviceId": "emulator-5554"}))
        self.assertEqual(self.holders(), {"uitest_api37": "session-a"})

        output = self.hook("pre-device-open", "session-b", tool_name="mcp__t3-code__device_open",
                           tool_input={"deviceId": "uitest_api37"})
        self.assertIn("taken by session-a", output["hookSpecificOutput"]["permissionDecisionReason"])

        self.hook("post-device-close", tool_name="mcp__t3-code__device_close",
                  tool_input={"deviceId": "emulator-5554"})
        self.assertEqual(self.holders(), {})

    def test_device_close_keeps_claims_made_with_the_cli(self):
        self.cli("claim", "uitest_api37")
        self.hook("post-device-close", tool_name="mcp__t3-code__device_close",
                  tool_input={"deviceId": "emulator-5554"})
        self.assertEqual(self.holders(), {"uitest_api37": "session-a"})

    def test_device_open_without_device_id_is_denied(self):
        self.cli("claim", "uitest_api37")
        output = self.hook("pre-device-open", tool_name="mcp__t3-code__device_open",
                           tool_input={"platform": "android"})
        self.assertIn('deviceId "uitest_api37"', output["hookSpecificOutput"]["permissionDecisionReason"])

    def test_ios_and_remote_hosts_are_ignored(self):
        for tool_input in ({"platform": "ios"}, {"deviceId": "x", "hostId": "mac-mini"}):
            self.assertIsNone(self.hook("pre-device-open", tool_name="mcp__t3-code__device_open",
                                        tool_input=tool_input))

    def test_session_end_releases_claims(self):
        self.cli("claim", "uitest_api37")
        self.cli("claim", "0A1B2C3D4E", session="session-b")
        self.hook("session-end", reason="prompt_input_exit")
        self.assertEqual(self.holders(), {"0A1B2C3D4E": "session-b"})

    def test_session_end_keeps_t3_thread_claims(self):
        create_db(self.t3_db)
        add_thread(self.t3_db, "thread-1", "session-a")
        self.cli("claim", "uitest_api37")
        self.hook("session-end", reason="other")
        self.assertEqual(self.holders(), {"uitest_api37": "t3:thread-1"})

    def test_t3_release_ends_the_agent_device_session(self):
        create_db(self.t3_db)
        add_thread(self.t3_db, "thread-1", "session-a")
        calls = install_agent_device(self.t3_db)
        add_agent_device_session(self.t3_db, agent_device_session("thread-1", "emulator-5554"))
        self.hook("pre-device-open", tool_name="mcp__t3-code__device_open",
                  tool_input={"deviceId": "emulator-5554"})
        self.assertEqual(self.holders(), {"uitest_api37": "t3:thread-1"})
        self.hook("post-device-close", tool_name="mcp__t3-code__device_close",
                  tool_input={"deviceId": "emulator-5554"})
        self.assertEqual(self.holders(), {})
        sessions = [line.split("--session ")[1] for line in calls.read_text().splitlines()]
        self.assertIn(agent_device_session("thread-1", "emulator-5554"), sessions)

    def test_settled_thread_ends_the_agent_device_session(self):
        create_db(self.t3_db)
        add_thread(self.t3_db, "thread-1", "session-a")
        calls = install_agent_device(self.t3_db)
        add_agent_device_session(self.t3_db, agent_device_session("thread-1", "uitest_api37"))
        self.hook("pre-device-open", tool_name="mcp__t3-code__device_open",
                  tool_input={"deviceId": "uitest_api37"})
        set_thread(self.t3_db, "thread-1", "settled_override", "settled")
        self.cli("gc", session="session-b")
        self.assertEqual(self.holders(), {})
        self.assertIn("close --config", calls.read_text())

    def test_taking_over_a_settled_thread_ends_its_agent_device_session(self):
        create_db(self.t3_db)
        add_thread(self.t3_db, "thread-1", "session-a")
        add_thread(self.t3_db, "thread-2", "session-b")
        calls = install_agent_device(self.t3_db)
        add_agent_device_session(self.t3_db, agent_device_session("thread-1", "emulator-5554"))
        self.hook("pre-device-open", tool_name="mcp__t3-code__device_open",
                  tool_input={"deviceId": "emulator-5554"})
        set_thread(self.t3_db, "thread-1", "settled_override", "settled")
        self.assertEqual(self.cli("claim", "uitest_api37", session="session-b").returncode, 0)
        self.assertEqual(self.holders(), {"uitest_api37": "t3:thread-2"})
        self.assertIn(agent_device_session("thread-1", "emulator-5554"), calls.read_text())

    def test_t3_project_policy(self):
        create_db(self.t3_db)
        add_thread(self.t3_db, "thread-1", "session-a", project="project-9")
        Path(self.env["DEVICELEASE_POLICY"]).write_text(json.dumps(
            {"projects": {"t3:project-9": {"allow": ["0A1B*"]}}}))
        output = self.hook("pre-device-open", tool_name="mcp__t3-code__device_open",
                           tool_input={"deviceId": "uitest_api37"})
        self.assertIn("not in project t3:project-9 allow list",
                      output["hookSpecificOutput"]["permissionDecisionReason"])
        whoami = json.loads(self.cli("whoami", "--json").stdout)
        self.assertEqual((whoami["t3Project"], whoami["allowed"]), ("project-9", ["0A1B2C3D4E"]))

    def test_heartbeat_refreshes_own_claims(self):
        self.cli("claim", "uitest_api37")
        claim_file = Path(self.env["DEVICELEASE_STATE_DIR"]) / "claims" / "uitest_api37.json"
        data = json.loads(claim_file.read_text())
        data["heartbeatAt"] = "2000-01-01T00:00:00Z"
        claim_file.write_text(json.dumps(data))
        self.hook("heartbeat", tool_name="Bash")
        self.assertNotEqual(json.loads(claim_file.read_text())["heartbeatAt"], "2000-01-01T00:00:00Z")

    def test_broken_input_fails_open(self):
        result = subprocess.run([sys.executable, "-m", "devicelease", "hook", "pre-bash"],
                                input="not json", env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("allowing the tool call", result.stderr)


if __name__ == "__main__":
    unittest.main()
