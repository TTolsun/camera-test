from contextlib import redirect_stdout
from io import StringIO
from itertools import count
import json
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

import camera_test
from camera_runner import adb, guidance
from camera_runner.failures import CLASS_BY_CODE
from tests.camera_fixtures import GALAXY_PACKAGE, GALAXY_SCENARIO, OWNER_APP, MockCameraAdb


PACKAGE_DIR = Path(__file__).resolve().parents[1] / "camera_runner"


class GuidanceTableTests(unittest.TestCase):
    def test_every_classified_and_known_code_has_its_own_entry(self):
        for code in (*CLASS_BY_CODE, *guidance.OTHER_KNOWN_CODES):
            self.assertIn(code, guidance.GUIDANCE, code)
            text = guidance.next_action(code)
            self.assertTrue(re.search("[가-힣]", text), code)
            self.assertNotEqual(text, guidance.FALLBACK, code)

    def test_every_code_raised_in_the_source_has_an_entry(self):
        raised = set()
        for path in PACKAGE_DIR.rglob("*.py"):
            raised |= set(re.findall(r'(?:RunError|MediaError|HalError)\(\s*"([A-Z_]+)"', path.read_text(encoding="utf-8")))
            raised |= set(re.findall(r'"(MEDIA_[A-Z_]+)"', path.read_text(encoding="utf-8")))
        raised.discard("MEDIA_NO_AUDIO")  # A warning, never an error.
        self.assertTrue(raised)
        self.assertEqual(sorted(code for code in raised if code not in guidance.GUIDANCE), [])

    def test_unknown_code_falls_back_by_prefix_then_generic(self):
        self.assertIn("doctor", guidance.next_action("ADB_SOMETHING_NEW"))
        self.assertIn("doctor", guidance.next_action("DEVICE_SOMETHING_NEW"))
        self.assertEqual(guidance.next_action("TOTALLY_NEW"), guidance.FALLBACK)

    def test_context_names_busy_package_versions_and_evidence(self):
        busy = guidance.next_action("PRECHECK_DEVICE_BUSY", {"preflight": {"foreground": {"package": OWNER_APP}}})
        self.assertIn(OWNER_APP, busy)
        version = guidance.next_action("UNSUPPORTED_CAMERA_VERSION", {
            "scenario": GALAXY_SCENARIO, "device": {"camera_version": "18.0.00.1"},
            "profile": {"verified_versions": ["17.0.00.43"]}})
        for expected in ("18.0.00.1", "17.0.00.43", f"python camera-test.pyz verify-build {GALAXY_SCENARIO}"):
            self.assertIn(expected, version)
        product = guidance.next_action("VIDEO_NOT_SAVED", {"artifacts": {"logcat": "runs/x/logcat.txt", "failure_screen": "runs/x/failure.png"}})
        self.assertIn("runs/x/logcat.txt", product)
        self.assertIn("runs/x/failure.png", product)
        self.assertIn("logcat.txt", guidance.next_action("VIDEO_NOT_SAVED"))
        self.assertIn("runs/x/video.mp4", guidance.next_action("MEDIA_DURATION", {"artifacts": {"video": "runs/x/video.mp4"}}))
        absent = {"diagnosis": {"ui": {"absent": {"mode_ready": {"expected": "a_main", "similar": ["a_main_v2"]}}}}}
        automation = guidance.next_action("MODE_NOT_READY", absent)
        for expected in ("a_main_v2", "profile show", "--output"):
            self.assertIn(expected, automation)

    def test_recording_may_be_active_adds_a_phone_check(self):
        plain = guidance.next_action("RECORD_STOP_UNVERIFIED", {})
        flagged = guidance.next_action("RECORD_STOP_UNVERIFIED", {"recording_may_be_active": True})
        self.assertTrue(flagged.startswith(plain))
        self.assertIn("녹화", flagged[len(plain):])


class CliGuidanceTests(unittest.TestCase):
    def setUp(self):
        self.device = MockCameraAdb()

    def cli(self, *arguments, slow=False):
        output = StringIO()
        with patch.object(sys, "argv", ["camera-test", *map(str, arguments)]), redirect_stdout(output), \
                patch.object(adb, "adb", side_effect=self.device), patch.object(time, "sleep"):
            if slow:
                with patch.object(time, "monotonic", side_effect=count(0, 5)):
                    code = camera_test.main()
            else:
                code = camera_test.main()
        return code, json.loads(output.getvalue())

    def test_run_error_has_next_action_with_context_and_is_saved(self):
        self.device.top = OWNER_APP
        with TemporaryDirectory() as output:
            code, result = self.cli("run", GALAXY_SCENARIO, "--output-dir", output, "--require-idle")
            saved = json.loads(Path(result["artifacts"]["result"]).read_text(encoding="utf-8"))
        self.assertEqual(result["error"]["code"], "PRECHECK_DEVICE_BUSY")
        self.assertIn(OWNER_APP, result["next_action"])
        self.assertEqual(saved["next_action"], result["next_action"])
        self.assertEqual(set(result["error"]), {"code", "message"})  # English message stays as it was.

        self.device = MockCameraAdb()
        self.device.versions[GALAXY_PACKAGE] = "18.0.00.1"
        with TemporaryDirectory() as output:
            code, result = self.cli("run", GALAXY_SCENARIO, "--output-dir", output)
        self.assertEqual(result["error"]["code"], "UNSUPPORTED_CAMERA_VERSION")
        for expected in ("18.0.00.1", "17.0.00.43", "verify-build"):
            self.assertIn(expected, result["next_action"])

    def test_run_automation_and_product_failures_point_at_the_right_artifacts(self):
        self.device.renamed_ids = {"shooting_mode_portrait_video_main": "shooting_mode_portrait_video_main_v2"}
        with TemporaryDirectory() as output:
            code, result = self.cli("run", GALAXY_SCENARIO, "--output-dir", output, slow=True)
        self.assertEqual((result["error"]["code"], result["failure_class"]), ("MODE_NOT_READY", "automation"))
        self.assertIn("shooting_mode_portrait_video_main_v2", result["next_action"])
        self.assertIn("profile show", result["next_action"])

        self.device = MockCameraAdb()
        self.device.record_log = ""
        with TemporaryDirectory() as output:
            code, result = self.cli("run", GALAXY_SCENARIO, "--output-dir", output, slow=True)
        self.assertEqual((result["error"]["code"], result["failure_class"]), ("RECORD_START_UNVERIFIED", "product"))
        self.assertIn("logcat.txt", result["next_action"])

    def test_successful_run_has_no_next_action(self):
        with TemporaryDirectory() as output:
            code, result = self.cli("run", GALAXY_SCENARIO, "--output-dir", output)
        self.assertEqual(code, 0)
        self.assertNotIn("next_action", result)

    def test_inspect_adds_next_action_only_when_not_runnable(self):
        code, result = self.cli("inspect")
        self.assertNotIn("next_action", result["scenario"])

        self.device.versions[GALAXY_PACKAGE] = "18.0.00.1"
        code, result = self.cli("inspect")
        self.assertEqual(result["scenario"]["status"], "unverified")
        self.assertIn(f"verify-build {GALAXY_SCENARIO}", result["scenario"]["next_action"])

        self.device = MockCameraAdb()
        code, result = self.cli("inspect", "--no-ui")
        self.assertIn("--no-ui", result["scenario"]["next_action"])

        self.device = MockCameraAdb()
        self.device.wakefulness = "Asleep"
        code, result = self.cli("inspect")
        self.assertEqual(result["scenario"]["reason"], "PRECHECK_SCREEN_OFF")
        self.assertIn("화면", result["scenario"]["next_action"])

        self.device = MockCameraAdb()
        del self.device.versions[GALAXY_PACKAGE]
        code, result = self.cli("inspect")
        self.assertEqual(result["scenario"]["reason"], "CAMERA_APP_NOT_FOUND")
        self.assertIn("next_action", result["scenario"])

    def test_error_objects_of_every_command_carry_next_action(self):
        self.device.listing = []
        code, result = self.cli("inspect")
        self.assertEqual((code, result["error"]["code"]), (1, "NO_DEVICE"))
        self.assertIn("doctor", result["error"]["next_action"])
        self.assertEqual(self.cli("hal", "begin", "--state", "off")[1]["error"]["code"], "NO_DEVICE")
        self.assertIn("next_action", self.cli("hal", "begin", "--state", "off")[1]["error"])
        self.assertIn("next_action", self.cli("hal", "compare", "--off", "missing-a", "--on", "missing-b")[1]["error"])
        self.assertIn("next_action", self.cli("profile", "validate", "missing.json")[1]["error"])
        self.assertIn("next_action", self.cli("media", "inspect", "missing.mp4")[1]["error"])
        code, result = self.cli("screens", "compare", "--golden", "missing-golden", "--run", "missing-run")
        self.assertIn("next_action", result["error"])
        code, result = self.cli("screens", "promote", "--run", "r", "--output", "o", "--merge", "--force")
        self.assertEqual(result["error"]["code"], "INVALID_ARGUMENTS")
        self.assertIn("next_action", result["error"])
        self.assertIn("next_action", self.cli("verify-build", GALAXY_SCENARIO, "--baseline", "missing.json")[1]["error"])


if __name__ == "__main__":
    unittest.main()
