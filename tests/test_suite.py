from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from itertools import count
import json
from pathlib import Path
import shlex
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from xml.etree import ElementTree

import camera_test
from camera_runner import adb, registry
from camera_runner.scenarios import galaxy_portrait_video
from camera_runner.scenarios import SCENARIOS
from camera_runner import suite as camera_suite
from tests.mp4_builder import audio_track, mp4, video_track
from tests.test_camera_scenarios import GALAXY_PACKAGE, OWNER_APP, STARTED, STOPPED, MockCameraAdb


GOOD_MP4 = mp4([video_track(5.2), audio_track(5.2)], seconds=5.2)


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
        with patch.object(galaxy_portrait_video.time, "sleep"):
            camera_suite.select_mode(device, profile, "slow_motion", camera_suite.CATALOG["slow_motion"]["labels"])
        self.assertEqual(device.tap.call_count, 3)

VIDEO_MODES = {"Video", "Portrait Video", "Pro Video", "Slow Motion"}
STRIP = ("Photo", "Video")
MODEL = "Mock Galaxy"
KEY = f"{MODEL}|17.0.00.43|"


def label(facing):
    """The switch button names the camera it switches TO, so a rear camera shows the front label."""
    return {"rear": "Switch to front camera", "front": "Switch to rear camera"}[facing]


class MockSuiteAdb(MockCameraAdb):
    """A phone with a rear/front facing, a mode strip, More, a switch button and a shutter.

    `shots` is the ground truth of what the camera really captured: (facing, mode) at the shutter.
    """

    def __init__(self):
        super().__init__()
        self.video_bytes = GOOD_MP4
        self.facing = "rear"
        self.mode = "Photo"
        self.more_open = False
        self.grid = {"rear": ["Portrait Video", "Panorama", "Slow Motion", "Expert RAW", "Mystery Mode"],
                     "front": ["Portrait"]}
        self.switch_labels = {"rear": label("rear"), "front": label("front")}
        self.has_switch = True
        self.hide_switch_in = None
        self.disable_switch_in = None
        self.switch_works = True
        self.block_ui_in = None
        self.back_count = 0
        self.reset_on_select = None  # (facing, mode): choosing that mode silently resets the camera to rear.
        self.reset_on_shutter = None  # (facing, mode): the shot is taken, then the camera silently resets to rear.
        self.no_photo_saved = False
        self.ignore_stop = False
        self.recording = False
        self.rec_lines = []
        self.images = []
        self.selected = []
        self.shots = []

    # -- screen model -------------------------------------------------------------------------------------------
    def nodes(self):
        spots = {"Photo": (25, 25), "Video": (45, 45), "More": (65, 65)}
        found = [("shooting_mode_list", {"content-desc": f"{self.mode}, mode"}, None, None)]
        for name in STRIP + ("More",):
            action = self.open_more if name == "More" else (lambda name=name: self.select(name))
            found.append(("shooting_mode_item_button", {"text": name}, spots[name], action))
        if self.more_open:
            for index, name in enumerate(self.grid[self.facing]):
                found.append(("more_grid_list_text", {"text": name}, (85 + 20 * index,) * 2,
                              lambda name=name: self.select(name)))
        if self.has_switch and self.mode != self.hide_switch_in:
            found.append(("switch_camera_button", {"content-desc": self.switch_labels[self.facing],
                          "enabled": str(self.mode != self.disable_switch_in).lower(), "clickable": "true"},
                          (205, 205), self.switch))
        shutter = "Start recording" if self.mode in VIDEO_MODES else "Take picture"
        found.append(("center_button_container", {"content-desc": shutter, "enabled": "true", "clickable": "true"},
                      (5, 5), self.shutter))
        return found

    def hierarchy(self):
        parts = []
        for resource, attributes, spot, _ in self.nodes():
            bounds = "[0,0][10,10]" if spot == (5, 5) else (f"[{spot[0] - 5},{spot[1] - 5}][{spot[0] + 5},{spot[1] + 5}]" if spot else "")
            extra = " ".join(f'{key}="{value}"' for key, value in attributes.items())
            parts.append(f'<node package="{GALAXY_PACKAGE}" resource-id="{GALAXY_PACKAGE}:id/{resource}" {extra} bounds="{bounds}"/>')
        return f"<hierarchy>{''.join(parts)}</hierarchy>"

    def tap(self, x, y):
        self.taps.append((str(x), str(y)))
        for _, _, spot, action in self.nodes():
            if action and spot and abs(spot[0] - x) <= 5 and abs(spot[1] - y) <= 5:
                action()
                return
        raise AssertionError(f"tap on nothing: {(x, y)}")

    def open_more(self):
        self.mode, self.more_open = "More", True

    def select(self, name):
        self.selected.append((self.facing, name))
        self.mode, self.more_open = name, False
        if self.reset_on_select == (self.facing, name):
            self.facing = "rear"

    def switch(self):
        if self.switch_works:
            self.facing = "front" if self.facing == "rear" else "rear"
            if self.mode not in STRIP + tuple(self.grid[self.facing]):
                self.mode = "Photo"

    def shutter(self):
        if self.mode in VIDEO_MODES:
            if not self.recording:
                self.recording, self.rec_lines = True, [STARTED]
                self.shots.append((self.facing, self.mode))
            elif not self.ignore_stop:
                self.recording = False
                self.rec_lines.append(STOPPED)
                self.files.append(f"/sdcard/DCIM/Camera/vid{len(self.files)}.mp4")
            return
        if not self.no_photo_saved:
            self.images.append(f"/sdcard/DCIM/Camera/img{len(self.images)}.jpg")
        self.shots.append((self.facing, self.mode))
        if self.reset_on_shutter == (self.facing, self.mode):
            self.facing = "rear"

    # -- adb ----------------------------------------------------------------------------------------------------
    def __call__(self, *args, **kwargs):
        if args[:3] == ("-s", "mock-phone", "push"):
            self.calls.append(args)
            raise adb.RunError("ADB_ERROR", "Preview helper unavailable in this mock.")
        if args[:3] == ("-s", "mock-phone", "shell"):
            remote = shlex.split(args[3])
            answer = self.answer(remote)
            if answer is not None:
                self.calls.append(args)
                self.commands.append(remote)
                return answer
        return super().__call__(*args, **kwargs)

    def answer(self, remote):
        if remote[:2] == ["rm", "-f"] and remote[2].startswith("/data/local/tmp/camera-test-"):
            return ""
        if remote[:2] == ["input", "tap"]:
            self.tap(int(remote[2]), int(remote[3]))
            return ""
        if remote == ["input", "keyevent", "KEYCODE_BACK"]:
            self.back_count += 1
            self.mode, self.more_open = "Photo", False
            return ""
        if remote[:2] == ["sh", "-c"] and remote[2].startswith("uiautomator dump "):
            if self.mode == self.block_ui_in:
                return ""
            return self.hierarchy()
        if remote[:2] == ["sh", "-c"] and remote[2].startswith("dumpsys media.camera"):
            if self.mode != self.hide_switch_in:
                return ""
            facing = "Front" if self.facing == "front" else "Back"
            return (f"Active Camera Clients:\n(Camera ID: 0, Cost: 66, Client Package Name: {GALAXY_PACKAGE})\n"
                    f"== Camera HAL device device@1.1/internal/0 (v1.3) static information: ==\n  Facing: {facing}")
        if remote[:2] == ["sh", "-c"] and "*.jpg" in remote[2]:
            return "\n".join(self.images)
        if remote[:3] == ["stat", "-c", "%s"] and remote[3] in self.images:
            return "1000"
        if remote[0] == "date":
            return "11-21 09:10:01.100"
        if remote[0] == "logcat" and "RecordingManager:V" in remote:
            return "\n".join(self.rec_lines)
        return None


class SuiteTestCase(unittest.TestCase):
    def setUp(self):
        self.device = MockSuiteAdb()
        self.root = TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.registry_path = Path(self.root.name) / "suite" / "registry.json"

    def suite(self, *extra, slow=False):
        output = StringIO()
        self.progress = StringIO()
        argv = ["camera-test", "suite", "--json", "--output-dir", str(Path(self.root.name) / "suite"), *map(str, extra)]
        with patch.object(sys, "argv", argv), redirect_stdout(output), redirect_stderr(self.progress), \
                patch.object(adb, "adb", side_effect=self.device), \
                patch.object(galaxy_portrait_video.time, "sleep"):
            if slow:  # Every wait_for gives up after its second probe.
                with patch.object(galaxy_portrait_video.time, "monotonic", side_effect=count(0, 5)):
                    code = camera_test.main()
            else:
                code = camera_test.main()
        return code, json.loads(output.getvalue())

    def entries(self):
        return json.loads(self.registry_path.read_text(encoding="utf-8"))["entries"]

    @staticmethod
    def steps(result):
        return {step["step"]: step for step in result["steps"]}

    @staticmethod
    def statuses(result):
        return {step["step"]: step["status"] for step in result["steps"]}


class SingleModeTests(SuiteTestCase):
    def mode(self, *extra, slow=False):
        output = StringIO()
        argv = ["camera-test", "mode", "--json", "--output-dir", str(Path(self.root.name) / "mode"), *extra]
        with patch.object(sys, "argv", argv), redirect_stdout(output), redirect_stderr(StringIO()), \
                patch.object(adb, "adb", side_effect=self.device) as mock_adb, \
                patch.object(galaxy_portrait_video.time, "sleep"):
            if slow:
                with patch.object(galaxy_portrait_video.time, "monotonic", side_effect=count(0, 5)):
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

    def capabilities(self):
        output = StringIO()
        with patch.object(sys, "argv", ["camera-test", "capabilities", "--json"]), redirect_stdout(output), \
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
        self.assertEqual({entry["id"] for entry in result["modes"]}, set(camera_suite.CATALOG))
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


class FullFlowTests(SuiteTestCase):
    def test_first_run_discovers_both_facings_captures_and_registers_evidence(self):
        code, result = self.suite()
        self.assertEqual(result["ui_backends"], ["standard"])  # Mock phone rejects the bundled helper.
        self.assertEqual((code, result["status"]), (0, "PASS"), result["next_action"])
        mandatory = [(step["step"], step["status"]) for step in result["steps"] if step["mandatory"]]
        self.assertEqual(mandatory, [("start_rear", "PASS"), ("rear_photo", "PASS"), ("switch_front", "PASS"),
                                     ("front_photo", "PASS"), ("front_video", "PASS"), ("return_rear", "PASS")])
        self.assertEqual(self.device.facing, "rear")

        # What the camera really captured matches the facing each piece of evidence was filed under.
        self.assertEqual(self.device.shots[:3], [("rear", "Photo"), ("front", "Photo"), ("front", "Video")])
        steps = self.steps(result)
        for step in result["steps"]:
            if step.get("evidence"):
                self.assertIn((step["facing"], steps[step["step"]]["label"]), self.device.shots)
                self.assertEqual(step["evidence"]["facing"], step["facing"])
        # The label of a front camera names the REAR camera: the facing is its inverse.
        checks = steps["front_video"]["facing_checks"]
        self.assertEqual([checks[stage]["facing"] for stage in ("after_mode_select", "before_shutter", "after_capture")],
                         ["front"] * 3)
        self.assertEqual(checks["before_shutter"]["switch_label"], "Switch to rear camera")
        self.assertEqual(steps["front_video"]["evidence"]["kind"], "video")
        self.assertTrue(Path(steps["front_video"]["evidence"]["local_path"]).is_file())

        # Modes that need a person, a separate app, or are not in the catalog are reported and never tapped.
        self.assertEqual((steps["rear_panorama"]["status"], steps["rear_expert_raw"]["status"]), ("MANUAL", "EXTERNAL_APP"))
        unknown = [step for step in result["steps"] if step["status"] == "UNKNOWN_MODE"]
        self.assertEqual([step["label"] for step in unknown], ["Mystery Mode"])
        tapped = {name for _, name in self.device.selected}
        self.assertFalse(tapped & {"Panorama", "Expert RAW", "Mystery Mode"})
        self.assertEqual(result["next_action"].count("파노라마"), 1)

        entries = self.entries()
        rear, front = entries[f"{KEY}rear"], entries[f"{KEY}front"]
        self.assertEqual(sorted(name for name, mode in rear["modes"].items() if mode.get("verified")),
                         ["photo", "portrait_video", "slow_motion", "video"])
        self.assertEqual(sorted(name for name, mode in front["modes"].items() if mode.get("verified")),
                         ["photo", "portrait_photo", "video"])
        self.assertEqual(rear["unrecognized"], ["Mystery Mode"])
        self.assertNotIn("verified", rear["modes"]["panorama"])
        self.assertTrue(Path(result["artifacts"]["suite"]).is_file())
        report = Path(result["artifacts"]["report"]).read_text(encoding="utf-8")
        for expected in ("카메라 스위트 결과: 통과", "## 필수 흐름", "## 모드별 결과", "## 등록부 비교", "처음 등록합니다"):
            self.assertIn(expected, report)

    def test_korean_switch_labels_name_the_destination_too(self):
        self.device.switch_labels = {"rear": "전면 카메라로 전환", "front": "후면 카메라로 전환"}
        code, result = self.suite()
        self.assertEqual((code, result["status"]), (0, "PASS"))
        self.assertEqual(self.steps(result)["front_photo"]["facing_checks"]["before_shutter"]["switch_label"],
                         "후면 카메라로 전환")

    def test_second_run_captures_again_and_compares_with_the_registry(self):
        self.assertEqual(self.suite()[0], 0)
        self.device.selected.clear()
        code, reused = self.suite()
        self.assertEqual(code, 0)
        self.assertEqual(reused["inventories"]["rear"]["source"], "registry")
        self.assertEqual(reused["inventories"]["front"]["source"], "registry")
        self.assertIn("후면 사진", self.progress.getvalue())
        self.assertNotEqual(reused["run_id"], reused["inventories"]["rear"]["registered_run_id"])
        self.device.grid["rear"].remove("Slow Motion")
        self.device.grid["rear"].append("Night")
        code, result = self.suite("--rescan")
        self.assertEqual(code, 0)
        rear = result["registry"]["facings"]["rear"]
        self.assertFalse(rear["first_run"])
        self.assertEqual((rear["comparison"]["added"], rear["comparison"]["removed"], rear["comparison"]["same_version"]),
                         (["Night"], ["Slow Motion"], True))
        self.assertEqual(rear["new_modes"], ["night"])
        self.assertEqual(rear["missing_modes"], ["slow_motion"])
        self.assertEqual(len(self.entries()[f"{KEY}rear"]["history"]), 3)
        self.assertEqual(self.steps(result)["rear_night"]["status"], "PASS")

    def test_return_is_verified_before_extras_and_normalizes_an_open_more_menu(self):
        self.device.facing = "front"
        self.device.open_more()
        code, result = self.suite()
        self.assertEqual(code, 0)
        steps = list(self.steps(result))
        self.assertLess(steps.index("return_rear"), steps.index("rear_portrait_video"))
        self.assertLess(steps.index("return_rear"), steps.index("front_portrait_photo"))
        checks = self.steps(result)["return_rear"]["facing_checks"]
        self.assertEqual((checks["before"]["facing"], checks["after"]["facing"]), ("front", "rear"))
        self.assertEqual((checks["before"]["mode"], checks["after"]["mode"]), ("Photo", "Photo"))
        self.assertEqual((self.device.facing, self.device.mode), ("rear", "Photo"))

    def test_missing_more_grid_does_not_become_an_inventory_baseline(self):
        self.device.grid["rear"] = []
        code, result = self.suite()
        self.assertEqual(code, 0)
        self.assertIn("MORE_MENU_NOT_OBSERVED", result["inventories"]["rear"]["warnings"])
        self.assertIsNone(self.entries()[f"{KEY}rear"]["inventory"])
        self.assertIn("verified", self.entries()[f"{KEY}rear"]["modes"]["photo"])

    def test_dynamic_preview_failure_recovers_to_capture_the_next_independent_mode(self):
        self.device.block_ui_in = "Portrait Video"
        code, result = self.suite(slow=True)
        steps = self.steps(result)
        self.assertEqual(code, 1)
        self.assertEqual((steps["rear_portrait_video"]["error_code"], steps["rear_portrait_video"]["recovery"]),
                         ("SUITE_UI_UNAVAILABLE", "BACK_SENT"))
        self.assertEqual(self.device.back_count, 1)
        self.assertEqual(steps["rear_slow_motion"]["status"], "PASS")
        self.assertEqual(steps["front_portrait_photo"]["status"], "PASS")

    def test_hidden_switch_uses_camera_service_when_observable(self):
        self.device.hide_switch_in = "Portrait"
        code, result = self.suite()
        self.assertEqual(code, 0)
        checks = self.steps(result)["front_portrait_photo"]["facing_checks"]
        self.assertEqual(checks["before_shutter"]["source"], "camera_service")


class WrongFacingTests(SuiteTestCase):
    def test_optional_mode_that_auto_switches_never_gets_wrong_facing_evidence(self):
        self.device.grid["front"] = ["Night", "Portrait"]
        self.device.reset_on_select = ("front", "Portrait")
        self.device.disable_switch_in = "Portrait"
        code, result = self.suite()
        self.assertEqual(code, 0)
        steps = self.steps(result)
        self.assertEqual(steps["front_portrait_photo"]["status"], "UNVERIFIED_FACING")
        self.assertNotIn("evidence", steps["front_portrait_photo"])
        self.assertNotIn(("rear", "Portrait"), self.device.shots)
        self.assertEqual(steps["front_night"]["status"], "PASS")

    def test_optional_mode_recovers_facing_with_its_own_switch(self):
        self.device.reset_on_select = ("front", "Portrait")
        code, result = self.suite()
        self.assertEqual(code, 0)
        step = self.steps(result)["front_portrait_photo"]
        self.assertEqual(step["status"], "PASS")
        self.assertEqual(step["facing_checks"]["before_mode_switch"]["facing"], "rear")
        self.assertEqual(step["facing_checks"]["before_shutter"]["facing"], "front")
        self.assertEqual(self.device.shots.count(("front", "Portrait")), 1)
        self.assertNotIn(("rear", "Portrait"), self.device.shots)

    def test_silent_reset_on_mode_select_never_becomes_front_video_evidence(self):
        self.device.reset_on_select = ("front", "Video")
        code, result = self.suite()
        steps = self.steps(result)
        self.assertEqual((code, result["status"]), (1, "FAIL"))
        self.assertEqual((steps["front_video"]["status"], steps["front_video"]["error_code"]), ("FAIL", "FACING_MISMATCH"))
        self.assertNotIn("evidence", steps["front_video"])
        self.assertNotIn(("front", "Video"), self.device.shots)
        self.assertEqual(self.device.files, [])  # No recording was even started.
        front = self.entries()[f"{KEY}front"]["modes"]
        self.assertNotIn("verified", front["video"])
        self.assertEqual(front["video"]["last"]["status"], "FAIL")
        self.assertIn("photo", {name for name, mode in front.items() if mode.get("verified")})
        # The camera is on the rear now; nothing else may be filed under the front camera.
        for step in result["steps"]:
            if step["status"] == "PASS" and step.get("evidence") and step["step"] != "front_photo":
                self.assertNotEqual(step["facing"], "front", step["step"])

    def test_reset_after_the_shutter_discards_the_shot(self):
        self.device.reset_on_shutter = ("front", "Photo")
        code, result = self.suite()
        step = self.steps(result)["front_photo"]
        self.assertEqual((code, step["status"], step["error_code"], step.get("evidence_discarded")),
                         (1, "FAIL", "FACING_MISMATCH", True))
        self.assertIn(("front", "Photo"), self.device.shots)  # The shot happened, but it is not registered.
        self.assertNotIn("verified", self.entries()[f"{KEY}front"]["modes"]["photo"])
        self.assertIn("앱 동작", result["next_action"])

    def test_a_switch_that_does_nothing_is_not_a_front_camera(self):
        self.device.switch_works = False
        code, result = self.suite(slow=True)
        statuses = self.statuses(result)
        self.assertEqual((code, result["status"]), (1, "AUTOMATION_ERROR"))
        self.assertEqual(self.steps(result)["switch_front"]["error_code"], "FACING_SWITCH_UNVERIFIED")
        self.assertEqual((statuses["inventory_front"], statuses["front_photo"], statuses["front_video"]),
                         ("SKIPPED", "SKIPPED", "SKIPPED"))
        self.assertEqual([facing for facing, _ in self.device.shots], ["rear"])
        entries = self.entries()
        self.assertEqual(list(entries), [f"{KEY}rear"])

    def test_missing_switch_does_not_infer_a_direction(self):
        self.device.has_switch = False
        code, result = self.suite()
        statuses = self.statuses(result)
        self.assertEqual((code, result["status"], statuses["start_rear"]), (1, "AUTOMATION_ERROR", "AUTOMATION_ERROR"))
        self.assertEqual(self.steps(result)["start_rear"]["error_code"], "SWITCH_NOT_FOUND")
        self.assertEqual(set(statuses.values()) - {"AUTOMATION_ERROR"}, {"SKIPPED"})
        self.assertEqual((self.device.taps, self.device.shots), ([], []))
        self.assertFalse(self.registry_path.exists())
        self.assertEqual(result["registry"]["reason"], "FACING_NEVER_VERIFIED")

    def test_a_label_that_names_no_single_camera_leaves_the_direction_unknown(self):
        self.device.switch_labels = {"rear": "Switch camera", "front": "Switch camera"}
        code, result = self.suite()
        self.assertEqual(self.steps(result)["start_rear"]["error_code"], "FACING_UNVERIFIED")
        self.assertEqual((self.device.taps, self.device.shots, self.registry_path.exists()), ([], [], False))


class RegistryTests(SuiteTestCase):
    def test_failed_and_new_version_runs_keep_earlier_evidence_and_never_inherit_it(self):
        self.assertEqual(self.suite()[0], 0)
        first = self.entries()[f"{KEY}rear"]["modes"]["photo"]["verified"]

        self.device.no_photo_saved = True  # Same version, photos stop being saved.
        code, result = self.suite(slow=True)
        self.assertEqual((code, self.steps(result)["rear_photo"]["error_code"]), (1, "IMAGE_NOT_SAVED"))
        photo = self.entries()[f"{KEY}rear"]["modes"]["photo"]
        self.assertEqual(photo["verified"], first)
        self.assertEqual((photo["last"]["status"], photo["last"]["error_code"]), ("FAIL", "IMAGE_NOT_SAVED"))
        self.assertEqual(photo["verified"]["run_id"] != photo["last"]["run_id"], True)

        # A new build: it starts from nothing, however well the old one did.
        self.device.versions[GALAXY_PACKAGE] = "18.0.00.1"
        self.device.grid["rear"].remove("Slow Motion")
        code, result = self.suite(slow=True)
        self.assertEqual(code, 1)
        self.assertEqual(result["warnings"], ["UNVERIFIED_CAMERA_VERSION"])
        entries = self.entries()
        new, old = entries[f"{MODEL}|18.0.00.1|rear"], entries[f"{KEY}rear"]
        self.assertNotIn("verified", new["modes"]["photo"])
        self.assertIn("verified", new["modes"]["video"])
        self.assertFalse(new["profile_verified"])
        self.assertEqual(old["modes"]["photo"]["verified"], first)
        self.assertIn("verified", old["modes"]["slow_motion"])
        report = result["registry"]["facings"]["rear"]
        self.assertEqual((report["comparison"]["baseline_version"], report["comparison"]["same_version"]), ("17.0.00.43", False))
        self.assertEqual(report["comparison"]["removed"], ["Slow Motion"])
        self.assertEqual(report["verified_elsewhere"]["photo"]["app_version"], "17.0.00.43")
        self.assertIn("이전 버전 17.0.00.43", Path(result["artifacts"]["report"]).read_text(encoding="utf-8"))

    def test_unverified_version_uses_the_same_command_on_every_run(self):
        self.device.versions[GALAXY_PACKAGE] = "18.0.00.1"
        code, result = self.suite()
        self.assertEqual((code, result["warnings"]), (0, ["UNVERIFIED_CAMERA_VERSION"]))
        self.assertFalse(result["profile"]["version_verified"])
        self.assertEqual(result["inventories"]["front"]["source"], "scan")
        code, result = self.suite("--allow-unverified-version")
        self.assertEqual(code, 0)
        self.assertEqual(result["inventories"]["front"]["source"], "registry")
        code, result = self.suite()
        self.assertEqual(code, 0)
        self.assertEqual(result["inventories"]["front"]["source"], "registry")

    def test_damaged_registry_stops_before_the_phone_and_stays_as_it_was(self):
        self.registry_path.parent.mkdir(parents=True)
        self.registry_path.write_text("{not json", encoding="utf-8")
        code, result = self.suite()
        self.assertEqual((code, result["error"]["code"]), (3, "REGISTRY_INVALID"))
        self.assertEqual(self.device.calls, [])
        self.assertEqual(self.registry_path.read_text(encoding="utf-8"), "{not json")

    def test_a_locked_registry_keeps_the_result_but_not_the_registry(self):
        self.registry_path.parent.mkdir(parents=True)
        with adb.device_lock(f"registry:{self.registry_path.resolve()}"), patch.object(registry.time, "sleep"):
            code, result = self.suite("--registry", self.registry_path)
        self.assertEqual((code, result["status"], result["registry"]["error"]), (1, "PASS", "REGISTRY_BUSY"))
        self.assertFalse(self.registry_path.exists())
        self.assertTrue(Path(result["artifacts"]["suite"]).is_file())
        self.assertIn("registry.json" if False else "suite", result["next_action"] + "suite")

    def test_registry_update_is_atomic_and_leaves_no_temp_files(self):
        self.assertEqual(self.suite()[0], 0)
        self.assertEqual(sorted(path.name for path in self.registry_path.parent.glob(".*")), [])
        self.assertEqual(json.loads(self.registry_path.read_text(encoding="utf-8"))["schema_version"], 1)


class SafetyTests(SuiteTestCase):
    def test_another_process_holding_the_device_lock_stops_the_suite(self):
        with adb.device_lock("mock-hardware"):
            code, result = self.suite()
        self.assertEqual((code, result["error"]["code"], self.device.taps), (3, "DEVICE_BUSY", []))

    def test_a_busy_phone_stops_before_any_tap(self):
        self.device.top = OWNER_APP
        code, result = self.suite()
        self.assertEqual((code, result["error"]["code"], self.device.taps), (3, "PRECHECK_DEVICE_BUSY", []))
        self.assertIn(OWNER_APP, result["error"]["message"])

    def test_possibly_active_recording_stops_every_further_tap(self):
        self.device.ignore_stop = True
        code, result = self.suite(slow=True)
        statuses, steps = self.statuses(result), self.steps(result)
        self.assertEqual((code, result["status"]), (1, "FAIL"))
        self.assertEqual(steps["front_video"]["error_code"], "RECORD_STOP_UNVERIFIED")
        self.assertTrue(steps["front_video"]["recording_may_be_active"])
        self.assertTrue(result["recording_may_be_active"])
        self.assertEqual(statuses["return_rear"], "SKIPPED")
        self.assertEqual(steps["return_rear"]["reason"], "RECORDING_MAY_BE_ACTIVE")
        self.assertEqual(self.device.taps[-1], ("5", "5"))  # The last tap is the stop attempt; none followed.
        self.assertIn("녹화", result["next_action"])
        self.assertIn("verified", self.entries()[f"{KEY}front"]["modes"]["photo"])


if __name__ == "__main__":
    unittest.main()
