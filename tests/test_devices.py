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

    def test_boot_uses_the_snapshot(self):
        self.assertEqual(self.boot(), ["/sdk/emulator", "-avd", "Pixel_6a"])

    def test_cold_boot_skips_the_snapshot(self):
        self.assertEqual(self.boot(snapshot=False),
                         ["/sdk/emulator", "-avd", "Pixel_6a", "-no-snapshot"])


if __name__ == "__main__":
    unittest.main()
