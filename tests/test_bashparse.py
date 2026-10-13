import unittest

from devicelease.bashparse import DYNAMIC, EXPLICIT, IMPLICIT, UNPARSEABLE, device_targets

# (command, [(tool, mode, device)])
CASES = [
    # Commands that never touch a device.
    ("ls -la", []),
    ("adb devices", []),
    ("adb devices -l | grep emulator", []),
    ("adb version && adb kill-server", []),
    ("adb -s 0A1B2C3D4E devices", []),
    ("adb connect 192.168.1.5:5555", []),
    ("./gradlew assembleDebug lint", []),
    ("./gradlew :app:compileDebugKotlin --offline", []),
    ("emulator -list-avds", []),
    ("android docs search 'adb shell'", []),
    ("android emulator list", []),
    ("android sdk list", []),
    ("android screen resolve --screenshot shot.png --string 'tap #3'", []),
    ("devicelease list && devicelease claim --any", []),
    ("devicelease exec -- ./gradlew connectedAndroidTest", []),
    ("git commit -m 'run adb -s X shell'", []),
    ('grep -rn "adb -s" docs/', []),
    ("echo adb shell", []),
    ("cat <<'EOF' > notes.md\nadb -s 0A1B shell\n./gradlew installDebug\nEOF", []),
    ("cat <<-EOF\n\tadb shell ls\n\tEOF\necho done", []),
    ("# adb shell ls", []),
    ("agent-device snapshot -i --config c.json --session t3-abc", []),

    # Explicit targets.
    ("adb -s 0A1B2C3D4E shell ls", [("adb", EXPLICIT, "0A1B2C3D4E")]),
    ("adb -s0A1B2C3D4E logcat", [("adb", EXPLICIT, "0A1B2C3D4E")]),
    ("/home/u/Android/Sdk/platform-tools/adb -s emulator-5554 install a.apk",
     [("adb", EXPLICIT, "emulator-5554")]),
    ("ANDROID_SERIAL=emulator-5554 adb shell ls", [("adb", EXPLICIT, "emulator-5554")]),
    ("export ANDROID_SERIAL=0A1B; adb shell ls", [("adb", EXPLICIT, "0A1B")]),
    ("ANDROID_SERIAL=0A1B\nadb shell ls", [("adb", EXPLICIT, "0A1B")]),
    ("cd app && ANDROID_SERIAL=0A1B ./gradlew :app:installDebug",
     [("gradle", EXPLICIT, "0A1B")]),
    ("sudo -u me timeout -s KILL 30 adb -s A reboot", [("adb", EXPLICIT, "A")]),
    ("env ANDROID_SERIAL=B adb shell", [("adb", EXPLICIT, "B")]),
    ("nohup adb -s C logcat > log.txt 2>&1 &", [("adb", EXPLICIT, "C")]),
    ("adb -s A shell ls | head && adb -s B shell ls",
     [("adb", EXPLICIT, "A"), ("adb", EXPLICIT, "B")]),
    ("bash -c 'adb -s Z reboot'", [("adb", EXPLICIT, "Z")]),
    ("sh -lc \"cd x && adb -s Z shell\"", [("adb", EXPLICIT, "Z")]),
    ("x=$(adb -s Q shell getprop ro.product.model)", [("adb", EXPLICIT, "Q")]),
    ("echo `adb -s B shell id`", [("adb", EXPLICIT, "B")]),
    ("(cd app; adb -s D install x.apk)", [("adb", EXPLICIT, "D")]),
    ("emulator -avd Pixel_6a -no-snapshot", [("emulator", EXPLICIT, "Pixel_6a")]),
    ("emulator @Pixel_6a", [("emulator", EXPLICIT, "Pixel_6a")]),
    ("android emulator start Pixel_6a", [("android", EXPLICIT, "Pixel_6a")]),
    ("android layout --device=0A1B --pretty", [("android", EXPLICIT, "0A1B")]),
    ("android screen capture --device 0A1B -o s.png", [("android", EXPLICIT, "0A1B")]),
    ("android run --apks=a.apk --device=emulator-5554", [("android", EXPLICIT, "emulator-5554")]),
    ("scrcpy --serial=0A1B", [("scrcpy", EXPLICIT, "0A1B")]),
    ("agent-device open app --serial 0A1B", [("agent-device", EXPLICIT, "0A1B")]),
    ("fastboot -s 0A1B reboot", [("fastboot", EXPLICIT, "0A1B")]),

    # No device named.
    ("adb shell ls", [("adb", IMPLICIT, None)]),
    ("adb install -r app.apk", [("adb", IMPLICIT, None)]),
    ("adb wait-for-device", [("adb", IMPLICIT, None)]),
    ("cd app && ./gradlew connectedDebugAndroidTest", [("gradle", IMPLICIT, None)]),
    ("./gradlew -p app :app:installDebug", [("gradle", IMPLICIT, None)]),
    ("gradle uninstallAll", [("gradle", IMPLICIT, None)]),
    ("android install --apks=a.apk", [("android", IMPLICIT, None)]),
    ("android screen capture -o s.png", [("android", IMPLICIT, None)]),
    ("scrcpy", [("scrcpy", IMPLICIT, None)]),
    ("ANDROID_SERIAL=X true; adb shell", [("adb", IMPLICIT, None)]),
    ("export ANDROID_SERIAL=X; unset ANDROID_SERIAL; adb shell", [("adb", IMPLICIT, None)]),

    # Device named through the shell.
    ('adb -s "$(devicelease serial)" shell ls', [("adb", DYNAMIC, "$(devicelease serial)")]),
    ("adb -s $SERIAL shell ls", [("adb", DYNAMIC, "$SERIAL")]),
    ("adb -t 3 shell", [("adb", DYNAMIC, "transport id")]),
]


class BashParseTest(unittest.TestCase):
    def test_cases(self):
        for command, expected in CASES:
            with self.subTest(command=command):
                found = [(t.tool, t.mode, t.device) for t in device_targets(command)]
                self.assertEqual(found, expected)

    def test_adb_kind_flags(self):
        self.assertEqual([t.kind for t in device_targets("adb -e shell")], ["emulator"])
        self.assertEqual([t.kind for t in device_targets("adb -d shell")], ["physical"])

    def test_inherited_android_serial(self):
        targets = device_targets("adb shell ls", {"ANDROID_SERIAL": "0A1B"})
        self.assertEqual([(t.mode, t.device) for t in targets], [(EXPLICIT, "0A1B")])

    def test_unbalanced_quotes(self):
        self.assertEqual([t.mode for t in device_targets("adb shell 'ls")], [UNPARSEABLE])
        self.assertEqual(device_targets("echo 'it''s adb"), [])


if __name__ == "__main__":
    unittest.main()
