from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from itertools import count
import json
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import Mock, patch
from xml.etree import ElementTree

import camera_test
from camera_runner import adb, device_registry as registry
from camera_runner.apps import APPS, galaxy_camera
from camera_runner.preview import PreviewDevice
from camera_runner.profiles import parse_profile
from camera_runner.scenarios import SCENARIOS
from tests.mp4_builder import audio_track, mp4, video_track
from tests.camera_fixtures import GALAXY_PACKAGE, OWNER_APP, SuiteTestCase


class ModeMenuTransitionTests(unittest.TestCase):
    def test_fast_snapshot_can_still_show_previous_screen_after_back_or_more(self):
        profile = SCENARIOS["galaxy.portrait-video"].profile

        def snapshot(*controls):
            root = ElementTree.Element("hierarchy")
            for selector, label in controls:
                ElementTree.SubElement(root, "node", {"resource-id": profile.resource_id(selector),
                    "package": profile.package, "text": label, "bounds": "[0,0][20,20]"})
            return root

        pro = snapshot(("instant_mode_title", "프로 동영상"), ("instant_mode_back", ""))
        strip = snapshot(("mode_button", "더보기"))
        more = snapshot(("more_grid_item", "슬로우 모션"))
        device = Mock()
        device.ui.side_effect = [pro, pro, strip, strip, more]
        with patch.object(time, "sleep"):
            galaxy_camera.select_mode(device, profile, "slow_motion")
        self.assertEqual(device.tap.call_count, 3)

class SingleModeTests(SuiteTestCase):
    def mode(self, *extra, slow=False):
        output = StringIO()
        argv = ["camera-test", "mode", "--json", "--output-dir", str(Path(self.root.name) / "mode"), *extra]
        with patch.object(sys, "argv", argv), redirect_stdout(output), redirect_stderr(StringIO()), \
                patch.object(adb, "adb", side_effect=self.device) as mock_adb, \
                patch.object(time, "sleep"):
            if slow:
                with patch.object(time, "monotonic", side_effect=count(0, 5)):
                    code = camera_test.main()
            else:
                code = camera_test.main()
        return code, json.loads(output.getvalue()), mock_adb.call_count

    def check_pro_video(self, name, facing):
        self.device.grid[facing].append("Pro Video")
        code, result, _ = self.mode(name, "--facing", facing, "--duration", "4")
        self.assertEqual((code, result["status"], result["mode"], result["facing"]), (0, "PASS", "pro_video", facing))
        self.assertEqual(self.device.shots, [(facing, "Pro Video")])
        self.assertEqual((self.device.facing, self.device.mode), (facing, "Pro Video"))
        self.assertNotIn((facing, "Portrait Video"), self.device.selected)
        self.assertEqual(result["requested_seconds"], 4)
        self.assertEqual(result["step"]["evidence"]["kind"], "video")
        self.assertTrue(Path(result["step"]["evidence"]["local_path"]).is_file())
        self.assertTrue(Path(result["artifacts"]["mode"]).is_file())
        self.assertFalse(self.registry_path.exists())

    def test_pro_video_english_rear(self):
        self.check_pro_video("Pro Video", "rear")

    def test_pro_video_korean_front(self):
        self.check_pro_video("프로 동영상", "front")

    def capabilities(self, *extra):
        output = StringIO()
        with patch.object(sys, "argv", ["camera-test", "capabilities", "--json", *extra]), redirect_stdout(output), \
                patch.object(adb, "adb", side_effect=self.device):
            self.assertEqual(camera_test.main(), 0)
        return json.loads(output.getvalue())

    def test_device_registration_from_suite_to_single_request(self):
        with patch.object(registry, "DEVICE_DIRECTORY", Path(self.root.name) / "devices"):
            code, batch = self.suite("--register-device")
            self.assertEqual(code, 0)
            self.device.grid["rear"].append("Pro Video")
            code, capture, _ = self.mode("Pro Video", "--register-device")
            self.assertEqual(code, 0)
            self.assertEqual(batch["registry"]["path"], capture["registry"]["path"])
            self.device.mode = "Photo"
            code, attempt, _ = self.mode("Pro Video", "--register-device", "--facing", "front")
            self.assertEqual((code, attempt["status"]), (1, "NOT_OBSERVED"))
            self.assertIsNone(self.capabilities()["facings"]["front"]["modes"]["pro_video"]["capture_run"])
            self.device.grid["front"].append("Pro Video")
            code, opened, _ = self.mode("Pro Video", "--facing", "front", "--open-only", "--register-device")
            self.assertEqual(code, 0)
            shots = list(self.device.shots)
            facings = self.capabilities()["facings"]
            self.assertEqual(facings["rear"]["modes"]["pro_video"]["capture_run"], capture["run_id"])
            front = facings["front"]["modes"]
            self.assertEqual(front["photo"]["capture_run"], batch["run_id"])
            self.assertEqual(front["pro_video"]["opened_run"], opened["run_id"])
            self.assertIsNone(front["pro_video"]["capture_run"])
            self.assertEqual(self.device.shots, shots)
            self.device.video_bytes = mp4([video_track(3.2), audio_track(3.2)], seconds=3.2)
            code, replay, _ = self.mode("프로 동영상", "--duration", "3", "--facing", "front", "--register-device")
            self.assertEqual((code, self.device.shots[-1]), (0, ("front", "Pro Video")))
            self.assertEqual(self.capabilities()["facings"]["front"]["modes"]["pro_video"]["capture_run"],
                             replay["run_id"])
            self.device.versions[GALAXY_PACKAGE] = "new-version"
            self.assertIsNone(self.capabilities()["facings"]["rear"]["modes"]["pro_video"]["capture_run"])
            self.assertNotEqual(registry.device_path("other-hardware"), Path(capture["registry"]["path"]))

    def test_second_app_records_through_cli_without_galaxy_controls_or_recording_logs(self):
        package = "example.camera"
        profile = parse_profile({
            "schema_version": 1, "scenario": "example.clip", "package": package, "activity": ".Capture",
            "verified_versions": ["17.0.00.43"], "selectors": {}, "labels": {}, "log": {"tags": ["Example"]},
        }, "test")
        self.assertNotIn("record_state", profile.to_dict()["log"])
        # Only the adapter understands this app's UI, recording signal and media location.
        app = Mock(spec_set=["NAME", "profile", "CATALOG", "MODE_NAMES", "TIME_SHIFTED", "select_mode",
                             "current_mode", "mode_key", "read_state", "switch_facing", "shutter_control",
                             "shutter_point", "record", "require_foreground"])
        app.NAME, app.profile = "Example Camera", profile
        app.CATALOG = {"video": {"kind": "video", "labels": {"clip"}}}
        app.MODE_NAMES, app.TIME_SHIFTED = {"video": "Clip"}, set()
        screen = {"tab": "Welcome", "lens": "front"}
        def select_clip(device, profile, key, *, scenario_result):
            self.assertEqual(key, "video")
            screen["tab"] = "Clip"

        def switch_lens(device, state, *, require_enabled):
            self.assertTrue(require_enabled)
            self.assertEqual(state["facing"], "front")
            screen["lens"] = "rear"
            return True

        app.select_mode.side_effect = select_clip
        app.switch_facing.side_effect = switch_lens
        app.current_mode.side_effect = lambda root, _: root["tab"]
        app.mode_key.side_effect = lambda label: "video" if label == "Clip" else None
        app.read_state.side_effect = lambda root, *_: {
            "facing": root["lens"], "source": "lens_indicator", "switch": "flip", "label": None}
        app.shutter_control.return_value = "capture-button"
        app.shutter_point.return_value = (42, 50)

        def save(device, profile, point, seconds, result):
            self.assertEqual((profile.package, point), (package, (42, 50)))
            path = "/sdcard/Movies/clip.mp4"
            self.device.files.append(path)
            result["video"] = {"device_path": path, "bytes": len(self.device.video_bytes)}
            return "11-21 09:10:01.100"

        app.record.side_effect = save
        self.device.versions[package] = self.device.versions[GALAXY_PACKAGE]
        with patch.dict(APPS, {package: app}), patch.object(registry, "DEVICE_DIRECTORY", Path(self.root.name)):
            code, galaxy, _ = self.mode("video", "--register-device")
            self.assertEqual(code, 0)
            self.assertIsNone(self.capabilities("--app", package)["facings"]["rear"]["modes"]["video"]["capture_run"])
            self.device.top = package
            with patch.object(PreviewDevice, "ui", return_value=screen):
                code, other, _ = self.mode("clip", "--app", package, "--register-device")
            self.assertEqual((code, other["step"]["evidence"]["device_path"]), (0, "/sdcard/Movies/clip.mp4"))
            self.assertEqual(galaxy["registry"]["path"], other["registry"]["path"])
            self.assertEqual(self.capabilities()["facings"]["rear"]["modes"]["video"]["capture_run"], galaxy["run_id"])
            self.assertEqual(self.capabilities("--app", package)["facings"]["rear"]["modes"]["video"]["capture_run"],
                             other["run_id"])
            app.select_mode.assert_called_once()
            app.switch_facing.assert_called_once()
            app.record.assert_called_once()

    def test_two_handsets_of_same_model_keep_independent_capabilities(self):
        with patch.object(registry, "DEVICE_DIRECTORY", Path(self.root.name)):
            code, first, _ = self.mode("photo", "--register-device")
            self.assertEqual(code, 0)
            original = self.device.answer

            def second_handset(command):
                return "second-hardware" if command == ["getprop", "ro.serialno"] else original(command)

            with patch.object(self.device, "answer", side_effect=second_handset):
                self.assertIsNone(self.capabilities()["facings"]["rear"]["modes"]["photo"]["capture_run"])
                code, second, _ = self.mode("photo", "--register-device")
                self.assertEqual(code, 0)
                self.assertEqual(self.capabilities()["facings"]["rear"]["modes"]["photo"]["capture_run"], second["run_id"])
            self.assertNotEqual(first["registry"]["path"], second["registry"]["path"])
            self.assertEqual(self.capabilities()["facings"]["rear"]["modes"]["photo"]["capture_run"], first["run_id"])

    def test_later_failed_capture_preserves_prior_proof_and_last_attempt(self):
        with patch.object(registry, "DEVICE_DIRECTORY", Path(self.root.name)):
            self.device.grid["rear"].append("Pro Video")
            _, captured, _ = self.mode("Pro Video", "--register-device")
            self.device.ignore_stop = True
            code, failed, _ = self.mode("Pro Video", "--register-device", slow=True)
            record = self.capabilities()["facings"]["rear"]["modes"]["pro_video"]
            self.assertEqual((code, record["last"]["status"], record["last"]["run_id"]), (1, "FAIL", failed["run_id"]))
            self.assertEqual(record["capture_run"], captured["run_id"])
            self.assertEqual(record["opened_run"], failed["run_id"])

    def test_already_selected_mode_does_not_navigate(self):
        self.device.mode = "Pro Video"
        self.check_pro_video("pro-video", "rear")
        self.assertEqual(self.device.selected, [])

    def test_another_mode_runs_only_that_mode(self):
        code, result, _ = self.mode("portrait-photo", "--facing", "front")
        self.assertEqual((code, result["status"], result["mode"]), (0, "PASS", "portrait_photo"))
        self.assertEqual(self.device.shots, [("front", "Portrait")])
        self.assertEqual(result["step"]["evidence"]["kind"], "image")

    def test_open_only_selects_and_verifies_mode_without_touching_shutter(self):
        self.device.grid["front"].append("Pro Video")
        code, result, _ = self.mode("Pro Video", "--facing", "front", "--open-only")
        self.assertEqual((code, result["status"], result["step"]["status"]), (0, "OPENED", "OPENED"))
        self.assertEqual(result["step"]["facing_checks"]["after_mode_select"]["mode"], "Pro Video")
        self.assertEqual(result["step"]["facing_checks"]["after_mode_select"]["facing"], "front")
        self.assertEqual((self.device.mode, self.device.facing), ("Pro Video", "front"))
        self.assertEqual(self.device.shots, [])
        self.assertNotIn(("5", "5"), self.device.taps)
        self.assertIsNone(result["requested_seconds"])
        self.assertTrue(Path(result["artifacts"]["mode"]).is_file())

    def test_unavailable_on_requested_facing_does_not_capture(self):
        self.device.grid["rear"].append("Pro Video")
        code, result, _ = self.mode("Pro Video", "--facing", "front")
        self.assertEqual((code, result["status"], result["step"]["error_code"]), (1, "NOT_OBSERVED", "MODE_NOT_FOUND"))
        self.assertEqual(self.device.shots, [])
        self.assertNotIn(("rear", "Pro Video"), self.device.selected)

    def test_list_and_non_automatable_requests_work_offline(self):
        code, result, calls = self.mode("--list")
        self.assertEqual((code, result["status"], calls), (0, "LISTED", 0))
        self.assertEqual({entry["id"] for entry in result["modes"]}, set(galaxy_camera.CATALOG))
        for name, status in [("Mystery Mode", "UNKNOWN_MODE"), ("파노라마", "MANUAL"), ("Expert RAW", "EXTERNAL_APP")]:
            with self.subTest(name=name):
                code, result, calls = self.mode(name)
                self.assertEqual((code, result["status"], calls), (1, status, 0))
        self.assertEqual(self.device.shots, [])

    def test_busy_phone_returns_environment_status_before_any_tap(self):
        self.device.top = OWNER_APP
        code, result, _ = self.mode("Pro Video")
        self.assertEqual((code, result["status"], result["error"]["code"]), (3, "ENVIRONMENT_BLOCKED", "PRECHECK_DEVICE_BUSY"))
        self.assertEqual(self.device.taps, [])
        self.assertEqual(self.device.shots, [])

    def test_possibly_active_recording_reports_stop_failure_and_leaves_it_alone(self):
        self.device.grid["rear"].append("Pro Video")
        self.device.ignore_stop = True
        code, result, _ = self.mode("Pro Video", slow=True)
        self.assertEqual((code, result["status"], result["step"]["error_code"]), (1, "FAIL", "RECORD_STOP_UNVERIFIED"))
        self.assertTrue(result["recording_may_be_active"])
        self.assertEqual(self.device.shots, [("rear", "Pro Video")])
        self.assertEqual(self.device.taps[-1], ("5", "5"))
        self.assertEqual(self.device.back_count, 0)
        self.assertIn("녹화", result["next_action"])
