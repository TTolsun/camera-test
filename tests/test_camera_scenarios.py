from contextlib import redirect_stdout
from io import StringIO
import json
from itertools import count
from pathlib import Path
import shlex
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import camera_test
from camera_runner import adb
from camera_runner.profiles import parse_profile
from camera_runner.scenarios import SCENARIOS, Scenario, galaxy_portrait_video


GALAXY_SCENARIO = "galaxy.portrait-video"
GALAXY_PACKAGE = "com.sec.android.app.camera"
VIDEO_PATH = "/sdcard/DCIM/Camera/mock.mp4"
STARTED = "11-21 09:10:12.100 1234 1234 I RecordingManager: changeRecordingState : STARTING -> RECORDING"
STOPPED = "11-21 09:10:13.100 1234 1234 I RecordingManager: changeRecordingState : STOPPING -> IDLE"


def node(resource, attributes=""):
    return f'<node package="{GALAXY_PACKAGE}" resource-id="{GALAXY_PACKAGE}:id/{resource}" {attributes}/>'


class MockCameraAdb:
    def __init__(self):
        self.versions = {GALAXY_PACKAGE: "17.0.00.43"}
        self.mode = "photo"
        self.taps = []
        self.clocks = 0
        self.record_taps = 0
        self.renamed_ids = {}

    def hierarchy(self):
        if self.mode == "portrait":
            nodes = node("shooting_mode_portrait_video_main") + node(
                "center_button_container", 'content-desc="Start recording" enabled="true" clickable="true" bounds="[0,0][10,10]"'
            )
        else:
            nodes = node("shooting_mode_list", f'content-desc="{self.mode.title()}, mode"')
            nodes += node("shooting_mode_item_button", 'text="More" bounds="[10,10][20,20]"')
            nodes += node("shooting_mode_item_button", 'text="Photo" bounds="[20,20][30,30]"')
            if self.mode == "more":
                nodes += node("more_grid_list_text", 'text="Portrait Video" bounds="[30,30][40,40]"')
        for old, new in self.renamed_ids.items():
            nodes = nodes.replace(f'id/{old}"', f'id/{new}"')
        return f"<hierarchy>{nodes}</hierarchy>"

    def __call__(self, *args, **kwargs):
        if args == ("devices",):
            return "List of devices attached\nmock-phone\tdevice\n"
        assert args[:2] == ("-s", "mock-phone"), args
        if args[2] == "exec-out":
            return b""
        if args[2] == "pull":
            assert args[3] == VIDEO_PATH and kwargs["timeout"] == 60
            Path(args[4]).write_bytes(b"mock")
            return ""
        assert args[2] == "shell", args
        remote = shlex.split(args[3])
        if remote == ["getprop", "ro.serialno"]:
            return "mock-hardware"
        if remote == ["getprop"]:
            return "[ro.product.model]: [Mock Galaxy]\n[ro.soc.manufacturer]: [Exynos]"
        if remote[:2] == ["dumpsys", "package"]:
            version = self.versions.get(remote[2])
            return f"    versionName={version}" if version else ""
        if remote == ["dumpsys", "activity", "activities"]:
            return f"topResumedActivity=ActivityRecord{{1 u0 {GALAXY_PACKAGE}/.Camera}}"
        if remote[:2] == ["am", "start"]:
            assert remote[-1] == f"{GALAXY_PACKAGE}/.Camera"
            return "Status: ok"
        if remote[:2] == ["input", "tap"]:
            point = tuple(remote[2:])
            self.taps.append(point)
            if point == ("5", "5"):
                self.record_taps += 1
            else:
                self.mode = {("15", "15"): "more", ("25", "25"): "photo", ("35", "35"): "portrait"}[point]
            return ""
        if remote[0] == "date":
            self.clocks += 1
            return f"11-21 09:10:{self.clocks:02d}.100"
        if remote[0] == "pidof":
            return "1234"
        if remote[0] == "logcat":
            if "ExampleHal:V" in remote and "RecordingManager:V" not in remote:
                return "11-21 09:10:12.000 2222 2222 I ExampleHal: feature_applied=true"
            return STOPPED if remote[3] == "11-21 09:10:03.100" else STARTED
        if remote == ["stat", "-c", "%s", VIDEO_PATH]:
            return "4"
        if remote[:2] == ["sh", "-c"]:
            if remote[2].startswith("uiautomator dump "):
                return self.hierarchy()
            if remote[2].startswith("dumpsys media.camera | "):
                return "Number of camera devices: 1"
            if "find /sdcard/DCIM/Camera" in remote[2]:
                return VIDEO_PATH if self.record_taps == 2 else ""
        raise AssertionError(remote)


class ScenarioCliTests(unittest.TestCase):
    def setUp(self):
        self.device = MockCameraAdb()

    def cli(self, *arguments):
        output = StringIO()
        with patch.object(sys, "argv", ["camera-test", *map(str, arguments)]), redirect_stdout(output):
            with patch.object(adb, "adb", side_effect=self.device):
                code = camera_test.main()
        return code, json.loads(output.getvalue())

    def test_default_inspection_restores_mode_and_preserves_output(self):
        self.assertEqual(self.cli("list"), (0, [GALAXY_SCENARIO]))
        code, result = self.cli("inspect", "--no-ui")
        self.assertEqual((code, result["scenario"]["reason"], self.device.taps), (0, "UI_SCAN_SKIPPED", []))
        self.assertNotIn("camera_ui", result)

        code, result = self.cli("inspect")
        self.assertEqual(code, 0)
        self.assertEqual(result["scenario"], {"name": GALAXY_SCENARIO, "status": "runnable", "reason": "MODE_OBSERVED",
                                              "profile": {"source": "built-in", "verified_versions": ["17.0.00.43"],
                                                          "version_verified": True}})
        self.assertNotIn("scenarios", result)
        self.assertEqual(result["device"]["camera_version"], "17.0.00.43")
        self.assertEqual(result["camera_ui"]["observed_modes"], ["Photo", "Portrait Video"])
        self.assertEqual((self.device.taps, self.device.mode), ([("15", "15"), ("25", "25")], "photo"))

    def test_unverified_build_stops_unless_a_verification_run_is_requested(self):
        self.device.versions[GALAXY_PACKAGE] = "18.0.00.1"
        code, result = self.cli("inspect")
        self.assertEqual((code, result["scenario"]["status"], result["scenario"]["reason"]),
                         (0, "unverified", "UNSUPPORTED_CAMERA_VERSION"))
        self.assertFalse(result["scenario"]["profile"]["version_verified"])
        self.assertIn("Portrait Video", result["camera_ui"]["observed_modes"])

        with TemporaryDirectory() as output:
            code, result = self.cli("run", GALAXY_SCENARIO, "--output-dir", output)
        self.assertEqual((code, result["error"]["code"]), (1, "UNSUPPORTED_CAMERA_VERSION"))
        self.assertEqual(result["error"]["message"],
                         "Galaxy Camera 18.0.00.1 is not a verified version of this profile (17.0.00.43). "
                         "Rerun with --allow-unverified-version to verify it.")
        self.assertEqual(self.device.record_taps, 0)

        with TemporaryDirectory() as output, patch.object(galaxy_portrait_video.time, "sleep"):
            code, result = self.cli("run", GALAXY_SCENARIO, "--output-dir", output, "--allow-unverified-version")
        self.assertEqual((code, result["scenario_status"], result["warnings"]), (0, "PASS", ["UNVERIFIED_CAMERA_VERSION"]))
        self.assertFalse(result["profile"]["version_verified"])
        self.assertNotIn("diagnosis", result)

    def test_missing_camera_app_is_reported_before_any_operation(self):
        del self.device.versions[GALAXY_PACKAGE]
        with TemporaryDirectory() as output:
            code, result = self.cli("run", GALAXY_SCENARIO, "--output-dir", output, "--allow-unverified-version")
        self.assertEqual((code, result["error"]["code"], self.device.taps), (1, "CAMERA_APP_NOT_FOUND", []))

    def test_selector_drift_is_diagnosed_and_fixed_with_a_profile_file(self):
        # A new app build renames the Portrait Video marker; nothing else changes.
        self.device.versions[GALAXY_PACKAGE] = "18.0.00.1"
        self.device.renamed_ids = {"shooting_mode_portrait_video_main": "shooting_mode_portrait_video_main_v2"}
        with TemporaryDirectory() as output, patch.object(galaxy_portrait_video.time, "sleep"), \
                patch.object(galaxy_portrait_video.time, "monotonic", side_effect=count(0, 5)):
            code, result = self.cli("run", GALAXY_SCENARIO, "--output-dir", output, "--allow-unverified-version")
            self.assertEqual((code, result["error"]["code"]), (1, "MODE_NOT_READY"))
            self.assertTrue(Path(result["artifacts"]["failure_ui"]).is_file())
        diagnosis = result["diagnosis"]
        self.assertEqual(diagnosis["ui"]["absent"]["mode_ready"]["similar"][0], "shooting_mode_portrait_video_main_v2")
        self.assertIn("record_button", diagnosis["ui"]["present"])
        self.assertEqual(diagnosis["record_state_log"], {"tag": "RecordingManager", "lines": 1, "matched": 1})

        with TemporaryDirectory() as output, patch.object(galaxy_portrait_video.time, "sleep"):
            profile = Path(output) / "galaxy-18.json"
            self.assertEqual(self.cli("profile", "show", GALAXY_SCENARIO, "--output", profile)[0], 0)
            data = json.loads(profile.read_text(encoding="utf-8"))
            data["verified_versions"].append("18.0.00.1")
            data["selectors"]["mode_ready"] = "shooting_mode_portrait_video_main_v2"
            # Windows PowerShell 5.1 redirection writes UTF-16; the loader must accept it.
            profile.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-16")
            self.assertEqual(self.cli("profile", "validate", profile),
                             (0, {"valid": True, "scenario": GALAXY_SCENARIO, "verified_versions": ["17.0.00.43", "18.0.00.1"]}))

            # Same renamed build, fresh phone state: the mock's log timeline starts over.
            self.device = MockCameraAdb()
            self.device.versions[GALAXY_PACKAGE] = "18.0.00.1"
            self.device.renamed_ids = {"shooting_mode_portrait_video_main": "shooting_mode_portrait_video_main_v2"}
            code, result = self.cli("run", GALAXY_SCENARIO, "--output-dir", output, "--profile-file", profile)
        self.assertEqual((code, result["scenario_status"]), (0, "PASS"), result.get("error"))
        self.assertEqual(result["profile"], {"source": str(profile), "verified_versions": ["17.0.00.43", "18.0.00.1"],
                                             "version_verified": True})
        self.assertNotIn("warnings", result)

    def test_profile_validation_rejects_misspelled_or_foreign_keys(self):
        with TemporaryDirectory() as output:
            profile = Path(output) / "profile.json"
            data = SCENARIOS[GALAXY_SCENARIO].profile.to_dict()
            data["selectors"]["record_buton"] = data["selectors"].pop("record_button")
            profile.write_text(json.dumps(data), encoding="utf-8")
            code, result = self.cli("profile", "validate", profile)
            self.assertEqual((code, result["valid"], result["error"]["code"]), (1, False, "INVALID_PROFILE_FILE"))
            self.assertIn("record_buton", result["error"]["message"])

            data = SCENARIOS[GALAXY_SCENARIO].profile.to_dict()
            data["log"]["record_state"]["pattern"] = r"changeRecordingState (\w+)"
            profile.write_text(json.dumps(data), encoding="utf-8")
            self.assertIn("two groups", self.cli("profile", "validate", profile)[1]["error"]["message"])

            data = dict(SCENARIOS[GALAXY_SCENARIO].profile.to_dict(), scenario="other.scenario")
            profile.write_text(json.dumps(data), encoding="utf-8")
            with TemporaryDirectory() as runs:
                code, result = self.cli("run", GALAXY_SCENARIO, "--output-dir", runs, "--profile-file", profile)
            self.assertEqual((code, result["error"]["code"], self.device.taps), (1, "INVALID_PROFILE_FILE", []))

    def test_galaxy_run_records_video_and_checks_hal_logs(self):
        with TemporaryDirectory() as output, patch.object(galaxy_portrait_video.time, "sleep"):
            checks = Path(output) / "checks.json"
            checks.write_text(json.dumps({"schema_version": 1, "checks": [{
                "name": "mock-feature", "source": "hal", "vendor": "exynos", "tag": "ExampleHal", "expect_log": "feature_applied=true",
            }]}), encoding="utf-8")
            code, result = self.cli("run", GALAXY_SCENARIO, "--output-dir", output, "--pull-video",
                                    "--check", "mock-feature", "--checks-file", checks)

            self.assertEqual((code, result["scenario_status"], result["feature"]["status"]), (0, "PASS", "PASS"))
            self.assertEqual(result["steps"], ["more_opened", "portrait_video_ready", "recording_started", "recording_stopped", "video_saved"])
            self.assertEqual(result["video"], {"device_path": VIDEO_PATH, "bytes": 4})
            self.assertEqual(Path(result["artifacts"]["video"]).read_bytes(), b"mock")
            self.assertEqual(Path(result["artifacts"]["video"]).name, "portrait-video.mp4")
            self.assertTrue(Path(result["artifacts"]["logcat"]).is_file())

    def test_new_registration_drives_list_inspect_and_run(self):
        name = "example.capture"
        self.device.versions["example.camera"] = "2.0"
        scanner = Mock(return_value=({"observed_modes": ["Example"]}, True))
        runner = Mock(return_value="11-21 09:10:01.100")
        profile = parse_profile({
            "schema_version": 1, "scenario": name, "package": "example.camera", "activity": ".Main",
            "verified_versions": ["2.0"], "selectors": {}, "labels": {},
            "log": {"tags": ["ExampleCamera"], "record_state": {"tag": "ExampleCamera", "pattern": r"(\w+) -> (\w+)"}},
        }, "built-in")
        scenario = Scenario("Example Camera", profile, "example.mp4", scanner, runner)

        with patch.dict(SCENARIOS, {name: scenario}), TemporaryDirectory() as output:
            self.assertEqual(self.cli("list"), (0, [GALAXY_SCENARIO, name]))
            code, result = self.cli("inspect")
            self.assertEqual(code, 0)
            self.assertEqual([item["name"] for item in result["scenarios"]], [GALAXY_SCENARIO, name])
            self.assertEqual(result["scenarios"][1]["status"], "runnable")
            scanner.assert_called_once()
            code, result = self.cli("run", name, "--output-dir", output)
            self.assertEqual((code, result["scenario_status"], result["device"]["camera_version"]), (0, "PASS", "2.0"))
            runner.assert_called_once()


if __name__ == "__main__":
    unittest.main()
