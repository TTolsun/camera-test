from contextlib import redirect_stdout
from io import StringIO
import json
import sys
import unittest
from unittest.mock import patch

import camera_test
from camera_runner import adb
from tests.test_camera_scenarios import GALAXY_PACKAGE, GALAXY_SCENARIO, OWNER_APP, MockCameraAdb


FIRST_COMMAND = 'python camera-test.pyz mode "<모드명>"'
MDNS_HEADER = "List of discovered mdns services\n"


class DoctorTestCase(unittest.TestCase):
    def setUp(self):
        self.device = MockCameraAdb()

    def doctor(self, *arguments, adb_path="C:/platform-tools/adb.exe", text=False):
        output = StringIO()
        path = patch.object(adb, "adb_path", side_effect=adb_path) if isinstance(adb_path, Exception) \
            else patch.object(adb, "adb_path", return_value=adb_path)
        with patch.object(sys, "argv", ["camera-test", "doctor", *(() if text else ("--json",)), *arguments]), \
                redirect_stdout(output), patch.object(adb, "adb", side_effect=self.device), path:
            code = camera_test.main()
        return code, output.getvalue() if text else json.loads(output.getvalue())

    def statuses(self, result):
        return {item["name"]: item["status"] for item in result["checks"]}


class DoctorCheckTests(DoctorTestCase):
    def test_everything_ready(self):
        code, result = self.doctor()
        self.assertEqual(code, 0)
        self.assertEqual([item["name"] for item in result["checks"]],
                         ["python", "adb", "devices", "device", "camera_app", "phone_state"])
        self.assertEqual(set(self.statuses(result).values()), {"ok"})
        self.assertTrue(result["ready"])
        self.assertIn(FIRST_COMMAND, result["next_action"])
        by_name = {item["name"]: item for item in result["checks"]}
        self.assertIn("Android Debug Bridge version 1.0.41", by_name["adb"]["detail"])
        self.assertIn("C:/platform-tools/adb.exe", by_name["adb"]["detail"])
        self.assertIn("Mock Galaxy", by_name["device"]["detail"])
        self.assertIn("exynos", by_name["device"]["detail"])
        self.assertIn("17.0.00.43", by_name["camera_app"]["detail"])
        self.assertTrue(all("next_action" not in item for item in result["checks"]))

    def test_doctor_never_taps_launches_or_captures(self):
        self.doctor()
        self.doctor("--device", "mock-phone")
        self.assertEqual(self.device.taps, [])
        forbidden = [command for command in self.device.commands
                     if command[0] in {"input", "am", "screencap", "pidof"} or any("uiautomator" in part for part in command)]
        self.assertEqual(forbidden, [])
        self.assertFalse(any(call[:3] == ("-s", "mock-phone", "exec-out") or "pull" in call for call in self.device.calls))
        self.assertEqual(self.device.deleted, [])

    def test_no_device_suggests_connect_from_mdns(self):
        self.device.listing = []
        self.device.mdns = (MDNS_HEADER + "adb-R3CX\t_adb-tls-connect._tcp.\t192.168.35.18:45483\n"
                            "adb-R3CX\t_adb-tls-pairing._tcp.\t192.168.35.18:40001\n")
        code, result = self.doctor()
        self.assertEqual(code, 1)
        self.assertEqual(self.statuses(result), {"python": "ok", "adb": "ok", "devices": "fail", "device": "skip",
                                                 "camera_app": "skip", "phone_state": "skip"})
        self.assertFalse(result["ready"])
        action = result["next_action"]
        self.assertIn("`adb connect 192.168.35.18:45483`", action)
        self.assertNotIn("adb pair", action)
        self.assertEqual(self.device.commands, [])

    def test_no_device_with_only_a_pairing_service_says_to_pair(self):
        self.device.listing = []
        self.device.mdns = MDNS_HEADER + "adb-R3CX\t_adb-tls-pairing._tcp\t192.168.35.18:40001\n"
        code, result = self.doctor()
        self.assertEqual(code, 1)
        self.assertIn("`adb pair 192.168.35.18:40001`", result["next_action"])
        self.assertIn("6자리", result["next_action"])

    def test_no_device_and_no_mdns_explains_both_connection_types(self):
        self.device.listing = []
        code, result = self.doctor()
        self.assertEqual(code, 1)
        self.assertIn("USB 디버깅", result["next_action"])
        self.assertIn("adb pair", result["next_action"])

    def test_unauthorized_and_offline(self):
        self.device.listing = [("R3CX", "unauthorized")]
        code, result = self.doctor()
        self.assertEqual((code, self.statuses(result)["devices"]), (1, "fail"))
        self.assertIn("허용", result["next_action"])
        self.assertIn("R3CX", result["next_action"])

        self.device.listing = [("192.168.35.18:45483", "offline")]
        code, result = self.doctor()
        self.assertEqual(code, 1)
        self.assertIn("adb disconnect 192.168.35.18:45483", result["next_action"])

    def test_requested_device_that_is_not_ready_fails_with_its_state(self):
        self.device.listing = [("mock-phone", "device"), ("R3CX", "unauthorized")]
        code, result = self.doctor("--device", "R3CX")
        self.assertEqual((code, self.statuses(result)["devices"]), (1, "fail"))
        self.assertIn("허용", result["next_action"])
        code, result = self.doctor("--device", "nowhere")
        self.assertEqual((code, self.statuses(result)["devices"]), (1, "fail"))

    def test_several_different_phones_warn_and_list_device_values(self):
        self.device.listing = [("mock-phone", "device"), ("other-phone", "device")]
        self.device.other_hardware = {"other-phone": "other-hardware"}
        code, result = self.doctor()
        self.assertEqual(code, 1)
        self.assertEqual(self.statuses(result)["devices"], "warn")
        self.assertEqual(self.statuses(result)["device"], "skip")
        self.assertFalse(result["ready"])
        self.assertIn("`--device mock-phone`", result["next_action"])
        self.assertIn("`--device other-phone`", result["next_action"])

    def test_one_phone_listed_twice_is_ok_with_a_note(self):
        self.device.listing = [("mock-phone", "device"), ("adb-mock._adb-tls-connect._tcp", "device")]
        self.device.other_hardware = {"adb-mock._adb-tls-connect._tcp": "mock-hardware"}
        code, result = self.doctor()
        self.assertEqual(code, 0)
        item = result["checks"][2]
        self.assertEqual(item["status"], "ok")
        self.assertIn("mock-phone", item["detail"])
        self.assertIn("2개", item["detail"])
        self.assertTrue(result["ready"])

    def test_adb_missing_skips_everything_that_needs_it(self):
        code, result = self.doctor(adb_path=adb.RunError("ADB_NOT_FOUND", "Install Android SDK Platform-Tools or set CAMERA_TEST_ADB."))
        self.assertEqual(code, 1)
        self.assertEqual(self.statuses(result), {"python": "ok", "adb": "fail", "devices": "skip", "device": "skip",
                                                 "camera_app": "skip", "phone_state": "skip"})
        self.assertIn('$env:CAMERA_TEST_ADB = "', result["next_action"])
        self.assertEqual(self.device.calls, [])

    def test_camera_app_missing_and_unverified(self):
        del self.device.versions[GALAXY_PACKAGE]
        code, result = self.doctor()
        self.assertEqual((code, self.statuses(result)["camera_app"], self.statuses(result)["phone_state"]), (1, "fail", "ok"))
        self.assertFalse(result["ready"])

        self.device.versions[GALAXY_PACKAGE] = "18.0.00.1"
        code, result = self.doctor()
        self.assertEqual((code, self.statuses(result)["camera_app"]), (1, "warn"))
        self.assertFalse(result["ready"])  # `run` would stop with UNSUPPORTED_CAMERA_VERSION.
        for expected in ("18.0.00.1", "17.0.00.43", f"verify-build {GALAXY_SCENARIO}"):
            self.assertIn(expected, result["next_action"])

    def test_phone_state_failures_and_busy_warning(self):
        self.device.wakefulness = "Asleep"
        code, result = self.doctor()
        self.assertEqual((code, self.statuses(result)["phone_state"]), (1, "fail"))
        self.assertIn("화면", result["next_action"])

        self.device = MockCameraAdb()
        self.device.keyguard = "true"
        code, result = self.doctor()
        self.assertEqual((code, self.statuses(result)["phone_state"]), (1, "fail"))
        self.assertIn("잠금", result["next_action"])

        self.device = MockCameraAdb()
        self.device.free_kb = 1000
        code, result = self.doctor()
        self.assertEqual((code, self.statuses(result)["phone_state"]), (1, "fail"))
        self.assertIn("500MB", result["next_action"])

        self.device = MockCameraAdb()
        self.device.top = OWNER_APP
        code, result = self.doctor()
        self.assertEqual((code, self.statuses(result)["phone_state"]), (0, "warn"))
        self.assertTrue(result["ready"])
        warning = next(item for item in result["checks"] if item["name"] == "phone_state")
        self.assertIn(OWNER_APP, warning["next_action"])
        self.assertIn(FIRST_COMMAND, result["next_action"])


class DoctorOutputTests(DoctorTestCase):
    def test_json_shape(self):
        code, result = self.doctor()
        self.assertEqual(set(result), {"checks", "ready", "next_action"})
        for item in result["checks"]:
            self.assertLessEqual({"name", "status", "detail"}, set(item))
            self.assertIn(item["status"], {"ok", "warn", "fail", "skip"})

    def test_human_text_has_one_line_per_check_and_the_first_command(self):
        code, text = self.doctor(text=True)
        self.assertEqual(code, 0)
        for marker in ("[OK]", "python:", "adb:", "devices:", "device:", "camera_app:", "phone_state:"):
            self.assertIn(marker, text)
        self.assertIn(FIRST_COMMAND, text)
        self.assertIn("run을 실행할 준비가 되었습니다", text)

        self.device.listing = [("R3CX", "unauthorized")]
        code, text = self.doctor(text=True)
        lines = text.splitlines()
        failing = next(index for index, line in enumerate(lines) if line.startswith("[FAIL] devices:"))
        self.assertTrue(lines[failing + 1].startswith("       -> "))
        self.assertIn("[SKIP] device:", text)
        self.assertIn("run이 동작하지 않을 것", text)

    def test_help_examples_and_doctor_pointer(self):
        for arguments, expected in (([], "doctor"), (["run"], "run galaxy.portrait-video"), (["verify-build"], "verify-build galaxy.portrait-video"),
                                    (["doctor"], "doctor --json"), (["inspect"], "inspect --pretty"),
                                    (["screens", "promote"], "screens promote --run")):
            output = StringIO()
            with patch.object(sys, "argv", ["camera-test", *arguments, "--help"]), redirect_stdout(output):
                with self.assertRaises(SystemExit):
                    camera_test.main()
            self.assertIn("python camera-test.pyz " + expected, output.getvalue(), arguments)


if __name__ == "__main__":
    unittest.main()
