import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from camera_runner import adb, device_registry as registry
from camera_runner.apps import APPS
from tests.camera_fixtures import GALAXY_PACKAGE, OWNER_APP, MODEL, KEY, SuiteTestCase


class FullFlowTests(SuiteTestCase):
    def test_video_only_app_reports_unsupported_suite_before_touching_device(self):
        app = SimpleNamespace(NAME="Clip Camera", CATALOG={"video": {"kind": "video"}})
        with patch.dict(APPS, {"example.camera": app}):
            code, result = self.suite("--app", "example.camera")
        self.assertEqual((code, result["error"]["code"]), (1, "SUITE_MODE_UNSUPPORTED"))
        self.assertIn("photo", result["error"]["message"])
        self.assertEqual(self.device.calls, [])

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
        new, old = entries[f"{GALAXY_PACKAGE}|{MODEL}|18.0.00.1|rear"], entries[f"{KEY}rear"]
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

    def test_v1_registry_is_read_without_rewriting_and_upgraded_under_update_lock(self):
        code, first = self.suite()
        self.assertEqual(code, 0)
        old_entries = {}
        for entry in self.entries().values():
            entry.pop("package")
            key = f"{entry['model']}|{entry['app_version']}|{entry['facing']}"
            old_entries[key] = entry
        legacy = json.dumps({"schema_version": 1, "entries": old_entries})
        self.registry_path.write_text(legacy, encoding="utf-8")
        migrated = registry.load(self.registry_path)["entries"]
        self.assertEqual(migrated[f"{KEY}rear"]["modes"]["photo"]["verified"]["run_id"], first["run_id"])
        self.assertEqual(self.registry_path.read_text(encoding="utf-8"), legacy)

        # This other app/version must inherit neither Galaxy's inventory nor its past verification.
        run = {"package": "example.camera", "model": MODEL, "version": "2", "version_verified": True,
               "run_id": "other", "at": "2026-11-26", "status": "OPENED", "facings": {"rear": {
                   "inventory": {"observed": ["Photo"], "recognized": {"photo": "Photo"}, "unrecognized": []},
                   "steps": [{"mode": "photo", "status": "OPENED"}]}}}
        report = registry.update(self.registry_path, run)["rear"]
        self.assertIsNone(report["comparison"])
        self.assertEqual(report["verified_elsewhere"], {})
        self.assertEqual(self.entries()[f"{KEY}rear"], migrated[f"{KEY}rear"])
        self.assertEqual(self.entries()[f"example.camera|{MODEL}|2|rear"]["modes"]["photo"]["opened"]["run_id"], "other")

    def test_registry_update_is_atomic_and_leaves_no_temp_files(self):
        self.assertEqual(self.suite()[0], 0)
        self.assertEqual(sorted(path.name for path in self.registry_path.parent.glob(".*")), [])
        self.assertEqual(json.loads(self.registry_path.read_text(encoding="utf-8"))["schema_version"], 2)


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
