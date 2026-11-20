from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import shlex
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import camera_test
from camera_runner import adb
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
        return f"<hierarchy>{nodes}</hierarchy>"

    def __call__(self, *args, **kwargs):
        if args == ("devices",):
            return "List of devices attached\nmock-phone\tdevice\n"
        assert args[:2] == ("-s", "mock-phone"), args
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
        self.assertEqual(result["scenario"], {"name": GALAXY_SCENARIO, "status": "runnable", "reason": "MODE_OBSERVED"})
        self.assertNotIn("scenarios", result)
        self.assertEqual(result["device"]["camera_version"], "17.0.00.43")
        self.assertEqual(result["camera_ui"]["observed_modes"], ["Photo", "Portrait Video"])
        self.assertEqual((self.device.taps, self.device.mode), ([("15", "15"), ("25", "25")], "photo"))

    def test_unverified_build_keeps_existing_error_and_does_not_start_capture(self):
        self.device.versions[GALAXY_PACKAGE] = "0.0"
        code, result = self.cli("inspect")
        self.assertEqual((code, result["scenario"]["reason"]), (0, "UNSUPPORTED_CAMERA_VERSION"))
        self.assertIn("Portrait Video", result["camera_ui"]["observed_modes"])

        with TemporaryDirectory() as output:
            code, result = self.cli("run", GALAXY_SCENARIO, "--output-dir", output)
        self.assertEqual((code, result["error"]["code"]), (1, "UNSUPPORTED_CAMERA_VERSION"))
        self.assertEqual(result["error"]["message"], "This profile requires Galaxy Camera 17.0.00.43.")
        self.assertEqual(self.device.record_taps, 0)

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
        scenario = Scenario("example.camera", "Example Camera", "2.0", (), "example.mp4", scanner, runner)

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
