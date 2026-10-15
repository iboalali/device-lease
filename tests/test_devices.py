import tempfile
import unittest
from pathlib import Path
from unittest import mock

from devicelease import devices


class BootEmulatorTest(unittest.TestCase):
    def boot(self, **kwargs):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(devices.paths, "emulator_path", return_value="/sdk/emulator"), \
                mock.patch.object(devices.subprocess, "Popen") as popen, \
                mock.patch.object(devices, "_adb_devices",
                                  return_value=[("emulator-5554", "device", {})]), \
                mock.patch.object(devices, "_emulator_avd_name", return_value="Pixel_6a"), \
                mock.patch.object(devices, "_adb", return_value="1\n"):
            popen.return_value.poll.return_value = None
            serial = devices.boot_emulator("Pixel_6a", Path(tmp) / "Pixel_6a.log", **kwargs)
        self.assertEqual(serial, "emulator-5554")
        return popen.call_args.args[0]

    def test_boot_uses_the_snapshot_and_the_host_gpu(self):
        self.assertEqual(self.boot(), ["/sdk/emulator", "-avd", "Pixel_6a", "-gpu", "host"])

    def test_cold_boot_skips_the_snapshot(self):
        self.assertEqual(self.boot(snapshot=False),
                         ["/sdk/emulator", "-avd", "Pixel_6a", "-gpu", "host", "-no-snapshot"])

    def test_extra_args_follow_the_defaults(self):
        self.assertEqual(self.boot(extra_args=["-no-audio"]),
                         ["/sdk/emulator", "-avd", "Pixel_6a", "-gpu", "host", "-no-audio"])

    def test_own_gpu_replaces_the_host_gpu(self):
        self.assertEqual(self.boot(extra_args=["-gpu", "auto"]),
                         ["/sdk/emulator", "-avd", "Pixel_6a", "-gpu", "auto"])

    def exit_early(self, returncode, **kwargs):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(devices.paths, "emulator_path", return_value="/sdk/emulator"), \
                mock.patch.object(devices.subprocess, "Popen") as popen, \
                mock.patch.object(devices.time, "sleep") as sleep:
            popen.return_value.poll.return_value = returncode
            popen.return_value.returncode = returncode
            with self.assertRaises(devices.DiscoveryError) as raised:
                devices.boot_emulator("Pixel_6a", Path(tmp) / "Pixel_6a.log", **kwargs)
        sleep.assert_not_called()
        return str(raised.exception)

    def test_exit_before_boot_fails_right_away(self):
        message = self.exit_early(0)
        self.assertIn("exited with code 0 before it finished booting", message)
        self.assertNotIn("-gpu auto", message)

    def test_failed_host_gpu_boot_suggests_another_gpu(self):
        self.assertIn("-- -gpu auto", self.exit_early(1))

    def test_failed_boot_with_own_gpu_has_no_gpu_hint(self):
        own_gpu = ["-gpu", "swiftshader_indirect"]
        self.assertNotIn("-gpu auto", self.exit_early(1, extra_args=own_gpu))


if __name__ == "__main__":
    unittest.main()
