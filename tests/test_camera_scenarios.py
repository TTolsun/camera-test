from contextlib import redirect_stdout
from io import StringIO
import json
from itertools import count
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import Mock, patch

import camera_test
from camera_runner import adb
from camera_runner.profiles import parse_profile
from camera_runner.scenarios import SCENARIOS, Scenario
from tests.mp4_builder import audio_track, mp4, video_track
from tests.camera_fixtures import GALAXY_SCENARIO, GALAXY_PACKAGE, OWNER_APP, VIDEO_PATH, STARTED, node, MockCameraAdb


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

        with TemporaryDirectory() as output, patch.object(time, "sleep"):
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
        with TemporaryDirectory() as output, patch.object(time, "sleep"), \
                patch.object(time, "monotonic", side_effect=count(0, 5)):
            code, result = self.cli("run", GALAXY_SCENARIO, "--output-dir", output, "--allow-unverified-version")
            self.assertEqual((code, result["error"]["code"]), (1, "MODE_NOT_READY"))
            self.assertTrue(Path(result["artifacts"]["failure_ui"]).is_file())
        diagnosis = result["diagnosis"]
        self.assertEqual(diagnosis["ui"]["absent"]["mode_ready"]["similar"][0], "shooting_mode_portrait_video_main_v2")
        self.assertIn("record_button", diagnosis["ui"]["present"])
        self.assertEqual(diagnosis["record_state_log"], {"tag": "RecordingManager", "lines": 1, "matched": 1})

        with TemporaryDirectory() as output, patch.object(time, "sleep"):
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
        with TemporaryDirectory() as output, patch.object(time, "sleep"):
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


class SelectDeviceTests(unittest.TestCase):
    def select(self, listing, serials):
        def fake(*args, **kwargs):
            if args == ("devices",):
                return "List of devices attached\n" + "".join(f"{name}\t{state}\n" for name, state in listing)
            assert args[0] == "-s" and args[2:] == ("shell", "getprop ro.serialno"), args
            return serials[args[1]]
        with patch.object(adb, "adb", side_effect=fake) as mocked:
            try:
                return adb.select_device(None), mocked.call_count
            except adb.RunError as exc:
                return exc.code, mocked.call_count

    def test_one_phone_listed_twice_is_selected_once(self):
        mdns = "adb-R3CY10TZLJB-0nUgkc._adb-tls-connect._tcp"
        same = {mdns: "R3CY10TZLJB\n", "192.168.35.18:45483": "R3CY10TZLJB\n"}
        listing = [(mdns, "device"), ("192.168.35.18:45483", "device")]
        self.assertEqual(self.select(listing, same), ("192.168.35.18:45483", 3))
        self.assertEqual(self.select(listing[::-1], same)[0], "192.168.35.18:45483")
        self.assertEqual(self.select([("a", "device"), ("b", "device")], {"a": "X", "b": "X"})[0], "a")

    def test_different_phones_or_unreadable_serials_still_require_a_choice(self):
        listing = [("a", "device"), ("b", "device")]
        self.assertEqual(self.select(listing, {"a": "X", "b": "Y"})[0], "DEVICE_REQUIRED")
        self.assertEqual(self.select(listing, {"a": "", "b": ""})[0], "DEVICE_REQUIRED")

    def test_single_device_needs_no_extra_adb_call(self):
        self.assertEqual(self.select([("a", "device"), ("emulator-5554", "device")], {}), ("a", 1))


class EfficiencyCliTests(unittest.TestCase):
    def setUp(self):
        self.device = MockCameraAdb()

    def cli(self, *arguments):
        output = StringIO()
        with patch.object(sys, "argv", ["camera-test", *map(str, arguments)]), redirect_stdout(output):
            with patch.object(adb, "adb", side_effect=self.device):
                code = camera_test.main()
        return code, json.loads(output.getvalue())

    def run_scenario(self, *extra, slow=False):
        with TemporaryDirectory() as output, patch.object(time, "sleep"):
            if slow:  # Every wait_for gives up after its first probe.
                with patch.object(time, "monotonic", side_effect=count(0, 5)):
                    return self.cli("run", GALAXY_SCENARIO, "--output-dir", output, *extra)
            return self.cli("run", GALAXY_SCENARIO, "--output-dir", output, *extra)

    def test_preflight_records_measurements_on_success(self):
        code, result = self.run_scenario()
        self.assertEqual(code, 0)
        self.assertEqual(result["preflight"], {"screen": "awake", "lock": "unlocked",
                                               "free_bytes": 10_000_000 * 1024, "min_free_bytes": 500 * 1024 * 1024})
        self.assertNotIn("failure_class", result)

    def test_preflight_failures_stop_before_any_tap(self):
        for knob, value, expected in (("wakefulness", "Asleep", "PRECHECK_SCREEN_OFF"),
                                      ("wakefulness", "Dozing", "PRECHECK_SCREEN_OFF"),
                                      ("keyguard", "true", "PRECHECK_LOCKED"),
                                      ("free_kb", 100_000, "PRECHECK_LOW_STORAGE")):
            self.device = MockCameraAdb()
            setattr(self.device, knob, value)
            code, result = self.run_scenario()
            self.assertEqual((code, result["error"]["code"], result["failure_class"]), (1, expected, "environment"))
            self.assertEqual(self.device.taps, [])

    def test_unknown_lock_and_power_signals_do_not_fail(self):
        self.device.wakefulness = self.device.keyguard = None
        code, result = self.run_scenario()
        self.assertEqual(code, 0)
        self.assertEqual((result["preflight"]["screen"], result["preflight"]["lock"]), ("unknown", "unknown"))

    def test_inspect_reports_a_locked_phone_instead_of_scanning(self):
        self.device.keyguard = "true"
        code, result = self.cli("inspect")
        self.assertEqual((code, self.device.taps), (0, []))
        self.assertEqual(result["preflight"]["failed"], "PRECHECK_LOCKED")
        self.assertEqual(result["scenario"]["reason"], "PRECHECK_LOCKED")
        self.assertNotIn("camera_ui", result)
        self.assertNotIn("preflight", self.cli("inspect", "--no-ui")[1])

    def test_failure_class_for_each_class(self):
        self.device.launch_status = "Status: error"
        code, result = self.run_scenario()
        self.assertEqual((result["error"]["code"], result["failure_class"]), ("CAMERA_LAUNCH_FAILED", "product"))
        self.assertEqual(result["diagnosis"]["phase"], "launch")

        self.device = MockCameraAdb()
        self.device.renamed_ids = {"shooting_mode_portrait_video_main": "renamed"}
        code, result = self.run_scenario(slow=True)
        self.assertEqual((result["error"]["code"], result["failure_class"]), ("MODE_NOT_READY", "automation"))

        # The start log is there but its wording changed: automation. No log at all: product.
        self.device = MockCameraAdb()
        self.device.record_log = "11-21 09:10:12.100 1234 1234 I RecordingManager: state is now recording"
        code, result = self.run_scenario(slow=True)
        self.assertEqual((result["error"]["code"], result["failure_class"]), ("RECORD_START_UNVERIFIED", "automation"))
        self.device = MockCameraAdb()
        self.device.record_log = ""
        code, result = self.run_scenario(slow=True)
        self.assertEqual((result["error"]["code"], result["failure_class"]), ("RECORD_START_UNVERIFIED", "product"))
        self.assertEqual(result["diagnosis"]["phase"], "recording")

        code, result = self.cli("run", GALAXY_SCENARIO, "--profile-file", "missing.json")
        self.assertEqual((result["error"]["code"], result["failure_class"]), ("INVALID_PROFILE_FILE", "automation"))

    def test_diagnosis_lists_only_selectors_of_the_failing_phase(self):
        self.device.renamed_ids = {"shooting_mode_portrait_video_main": "renamed"}
        code, result = self.run_scenario(slow=True)
        diagnosis = result["diagnosis"]
        self.assertEqual(diagnosis["phase"], "mode_ready")
        # The Portrait Video screen has no mode strip, but the mode_ready phase does not need one.
        self.assertEqual(list(diagnosis["ui"]["absent"]), ["mode_ready"])
        self.assertIn("record_button", diagnosis["ui"]["present"])
        self.assertTrue(diagnosis["ui"]["resource_ids"])

    def test_capture_promote_compare_round_trip(self):
        with TemporaryDirectory() as root, patch.object(time, "sleep"):
            code, result = self.cli("run", GALAXY_SCENARIO, "--output-dir", root, "--capture-screens")
            self.assertEqual((code, result["screens"]), (0, ["initial", "more_opened", "mode_ready"]))
            run_dir = Path(result["artifacts"]["result"]).parent
            snapshot = json.loads((run_dir / "screens" / "mode_ready.json").read_text(encoding="utf-8"))
            self.assertEqual(snapshot["nodes"][0], {"resource-id": "shooting_mode_portrait_video_main", "text": "",
                                                    "content-desc": "", "class": ""})
            self.assertNotIn("bounds", json.dumps(snapshot))

            golden = Path(root) / "golden"
            code, promoted = self.cli("screens", "promote", "--run", run_dir, "--output", golden)
            self.assertEqual((code, promoted["scenario"], promoted["app_version"], promoted["profile_source"]),
                             (0, GALAXY_SCENARIO, "17.0.00.43", "built-in"))
            self.assertEqual(sorted(path.name for path in golden.iterdir()),
                             ["golden.json", "initial.json", "mode_ready.json", "more_opened.json"])
            self.assertEqual(self.cli("screens", "promote", "--run", run_dir, "--output", golden)[1]["error"]["code"],
                             "GOLDEN_EXISTS")

            code, report = self.cli("screens", "compare", "--golden", golden, "--run", run_dir)
            self.assertEqual((code, report["severity"]), (0, "none"))

            def drifted(name, edit):
                copy = Path(root) / name
                (copy / "screens").mkdir(parents=True)
                for path in (run_dir / "screens").glob("*.json"):
                    data = json.loads(path.read_text(encoding="utf-8"))
                    edit(path.stem, data["nodes"])
                    (copy / "screens" / path.name).write_text(json.dumps(data), encoding="utf-8")
                return self.cli("screens", "compare", "--golden", golden, "--run", copy)[1]

            def rename(checkpoint, nodes):
                for item in nodes:
                    if item["resource-id"] == "shooting_mode_portrait_video_main":
                        item["resource-id"] = "shooting_mode_portrait_video_v2"
            report = drifted("renamed", rename)
            self.assertEqual(report["severity"], "breaking")
            ready = report["checkpoints"]["mode_ready"]
            self.assertEqual((ready["missing_ids"], ready["new_ids"]),
                             (["shooting_mode_portrait_video_main"], ["shooting_mode_portrait_video_v2"]))
            self.assertEqual(ready["profile_selectors_missing"],
                             [{"key": "mode_ready", "selector": "shooting_mode_portrait_video_main"}])

            def extra(checkpoint, nodes):
                nodes.append({"resource-id": "promo_banner", "text": "New", "content-desc": "", "class": ""})
            report = drifted("extra", extra)
            self.assertEqual(report["severity"], "cosmetic")
            self.assertEqual(report["checkpoints"]["initial"]["new_ids"], ["promo_banner"])
            self.assertEqual(report["checkpoints"]["initial"]["profile_selectors_missing"], [])

            def relabel(checkpoint, nodes):
                for item in nodes:
                    if item["resource-id"] == "center_button_container":
                        item["content-desc"] = "Capture"
                    if item["resource-id"] == "shooting_mode_list":
                        item["content-desc"] = "Picture, mode"
            report = drifted("relabeled", relabel)
            self.assertEqual(report["severity"], "breaking")
            self.assertEqual(report["checkpoints"]["mode_ready"]["changed_labels"],
                             [{"id": "center_button_container", "golden": ["Start recording"], "now": ["Capture"]}])
            # The mode strip label is not a profile label, so that checkpoint is only cosmetic.
            self.assertEqual(report["checkpoints"]["initial"]["severity"], "cosmetic")

    def test_promote_refuses_failed_runs_and_runs_without_screens(self):
        with TemporaryDirectory() as root, patch.object(time, "sleep"):
            code, result = self.cli("run", GALAXY_SCENARIO, "--output-dir", root)
            run_dir = Path(result["artifacts"]["result"]).parent
            code, result = self.cli("screens", "promote", "--run", run_dir, "--output", Path(root) / "g")
            self.assertEqual((code, result["error"]["code"]), (1, "SCREENS_NOT_CAPTURED"))
            data = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
            data["scenario_status"] = "FAIL"
            (run_dir / "result.json").write_text(json.dumps(data), encoding="utf-8")
            code, result = self.cli("screens", "promote", "--run", run_dir, "--output", Path(root) / "g")
            self.assertEqual(result["error"]["code"], "RUN_NOT_PASSED")

    def test_run_with_golden_attaches_drift_without_changing_the_verdict(self):
        with TemporaryDirectory() as root, patch.object(time, "sleep"):
            code, result = self.cli("run", GALAXY_SCENARIO, "--output-dir", root, "--capture-screens")
            golden = Path(root) / "golden"
            self.cli("screens", "promote", "--run", Path(result["artifacts"]["result"]).parent, "--output", golden)

            self.device = MockCameraAdb()
            code, result = self.cli("run", GALAXY_SCENARIO, "--output-dir", root, "--golden", golden)
            self.assertEqual((code, result["scenario_status"], result["drift"]["severity"]), (0, "PASS", "none"))
            self.assertNotIn("warnings", result)

            # The mode strip is not used by the run, so the run passes while the screen drifts.
            self.device = MockCameraAdb()
            self.device.renamed_ids = {"shooting_mode_list": "shooting_mode_list_v2"}
            code, result = self.cli("run", GALAXY_SCENARIO, "--output-dir", root, "--golden", golden)
            self.assertEqual((code, result["scenario_status"], result["drift"]["severity"]), (0, "PASS", "breaking"))
            self.assertEqual(result["warnings"], ["UI_DRIFT_BREAKING"])
            self.assertEqual(result["drift"]["checkpoints"]["initial"]["profile_selectors_missing"],
                             [{"key": "mode_strip", "selector": "shooting_mode_list"}])
            self.assertTrue((Path(result["artifacts"]["screens"]) / "mode_ready.json").is_file())

            self.device = MockCameraAdb()
            self.device.versions[GALAXY_PACKAGE] = "18.0.00.1"
            self.device.renamed_ids = {"shooting_mode_list": "shooting_mode_list_v2"}
            code, result = self.cli("run", GALAXY_SCENARIO, "--output-dir", root, "--golden", golden,
                                    "--allow-unverified-version")
            self.assertEqual(result["warnings"], ["UNVERIFIED_CAMERA_VERSION", "UI_DRIFT_BREAKING"])

            code, result = self.cli("run", GALAXY_SCENARIO, "--output-dir", root, "--golden", Path(root) / "nope")
            self.assertEqual((code, result["error"]["code"], result["failure_class"]), (1, "INVALID_GOLDEN", "automation"))

    def golden_from_photo_run(self, root):
        code, result = self.cli("run", GALAXY_SCENARIO, "--output-dir", root, "--capture-screens")
        golden = Path(root) / "golden"
        self.cli("screens", "promote", "--run", Path(result["artifacts"]["result"]).parent, "--output", golden)
        return golden

    def captured_portrait_run(self, root, *extra):
        self.device = MockCameraAdb()
        self.device.mode = "portrait"
        return self.cli("run", GALAXY_SCENARIO, "--output-dir", root, *extra)

    def test_volatile_id_is_ignored_by_drift_comparison(self):
        with TemporaryDirectory() as root, patch.object(time, "sleep"):
            golden = self.golden_from_photo_run(root)
            self.assertIn("guide_text", SCENARIOS[GALAXY_SCENARIO].profile.volatile_ids)
            self.device = MockCameraAdb()
            self.device.mode = "portrait"
            original = MockCameraAdb.hierarchy
            hint = node("guide_text", 'text="Ready"')
            with patch.object(MockCameraAdb, "hierarchy",
                              lambda device: original(device).replace("</hierarchy>", hint + "</hierarchy>")):
                code, result = self.cli("run", GALAXY_SCENARIO, "--output-dir", root, "--golden", golden)
            self.assertEqual((code, result["scenario_status"], result["drift"]["severity"]), (0, "PASS", "none"))
            self.assertEqual(result["drift"]["checkpoints"]["mode_ready"]["new_ids"], [])
            self.assertNotIn("warnings", result)

    def test_cosmetic_drift_adds_no_warning_but_breaking_does(self):
        with TemporaryDirectory() as root, patch.object(time, "sleep"):
            golden = self.golden_from_photo_run(root)
            self.device = MockCameraAdb()
            self.device.mode = "portrait"
            original = MockCameraAdb.hierarchy
            banner = node("promo_banner", 'text="New"')
            with patch.object(MockCameraAdb, "hierarchy",
                              lambda device: original(device).replace("</hierarchy>", banner + "</hierarchy>")):
                code, result = self.cli("run", GALAXY_SCENARIO, "--output-dir", root, "--golden", golden)
            self.assertEqual((code, result["drift"]["severity"]), (0, "cosmetic"))
            self.assertEqual(result["drift"]["checkpoints"]["mode_ready"]["new_ids"], ["promo_banner"])
            self.assertNotIn("warnings", result)

            self.device = MockCameraAdb()
            self.device.renamed_ids = {"shooting_mode_list": "shooting_mode_list_v2"}
            code, result = self.cli("run", GALAXY_SCENARIO, "--output-dir", root, "--golden", golden)
            self.assertEqual(result["warnings"], ["UI_DRIFT_BREAKING"])

    def test_profile_rejects_volatile_ids_that_overlap_selectors(self):
        with TemporaryDirectory() as output:
            profile = Path(output) / "profile.json"
            data = SCENARIOS[GALAXY_SCENARIO].profile.to_dict()
            self.assertIn("guide_text", data["volatile_ids"])
            data["volatile_ids"] = ["guide_text", data["selectors"]["record_button"]]
            profile.write_text(json.dumps(data), encoding="utf-8")
            code, result = self.cli("profile", "validate", profile)
            self.assertEqual((code, result["valid"]), (1, False))
            self.assertIn("center_button_container", result["error"]["message"])
            data["volatile_ids"] = "guide_text"
            profile.write_text(json.dumps(data), encoding="utf-8")
            self.assertFalse(self.cli("profile", "validate", profile)[1]["valid"])
            del data["volatile_ids"]  # Optional in profile files.
            profile.write_text(json.dumps(data), encoding="utf-8")
            self.assertTrue(self.cli("profile", "validate", profile)[1]["valid"])

    def test_promote_merge_adds_checkpoints_of_the_other_start_state(self):
        with TemporaryDirectory() as root, patch.object(time, "sleep"):
            golden = self.golden_from_photo_run(root)
            before = (golden / "mode_ready.json").read_text(encoding="utf-8")
            code, result = self.captured_portrait_run(root, "--capture-screens")
            run_dir = Path(result["artifacts"]["result"]).parent
            self.assertEqual(result["screens"], ["initial_in_mode", "mode_ready"])

            args = ("screens", "promote", "--run", run_dir, "--output", golden)
            self.assertEqual(self.cli(*args)[1]["error"]["code"], "GOLDEN_EXISTS")
            code, merged = self.cli(*args, "--merge")
            self.assertEqual(code, 0, merged)
            self.assertEqual(merged["checkpoints"], ["initial", "initial_in_mode", "mode_ready", "more_opened"])
            self.assertEqual(json.loads((golden / "golden.json").read_text(encoding="utf-8"))["checkpoints"],
                             merged["checkpoints"])
            self.assertTrue((golden / "initial_in_mode.json").is_file())
            self.assertEqual((golden / "mode_ready.json").read_text(encoding="utf-8"), before)

            self.assertEqual(self.cli(*args, "--merge", "--force")[1]["error"]["code"], "INVALID_ARGUMENTS")

            data = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
            data["device"]["camera_version"] = "18.0.00.1"
            (run_dir / "result.json").write_text(json.dumps(data), encoding="utf-8")
            self.assertEqual(self.cli(*args, "--merge")[1]["error"]["code"], "GOLDEN_MISMATCH")

    def test_failed_preflight_keeps_the_measurement(self):
        self.device.top = OWNER_APP
        code, result = self.run_scenario("--require-idle")
        self.assertEqual(result["error"]["code"], "PRECHECK_DEVICE_BUSY")
        self.assertEqual(result["preflight"]["foreground"]["package"], OWNER_APP)
        self.assertEqual(result["preflight"]["foreground"]["state"], "busy")
        self.device = MockCameraAdb()
        self.device.keyguard = "true"
        code, result = self.run_scenario()
        self.assertEqual(result["preflight"]["lock"], "locked")

    def test_golden_from_photo_start_does_not_flag_a_run_that_starts_in_portrait_video(self):
        with TemporaryDirectory() as root, patch.object(time, "sleep"):
            code, result = self.cli("run", GALAXY_SCENARIO, "--output-dir", root, "--capture-screens")
            golden = Path(root) / "golden"
            self.cli("screens", "promote", "--run", Path(result["artifacts"]["result"]).parent, "--output", golden)

            # The app remembers Portrait Video, so the next run starts already in the mode.
            self.device = MockCameraAdb()
            self.device.mode = "portrait"
            code, result = self.cli("run", GALAXY_SCENARIO, "--output-dir", root, "--golden", golden)
            self.assertEqual((code, result["scenario_status"], result["screens"]), (0, "PASS", ["initial_in_mode", "mode_ready"]))
            self.assertEqual(result["drift"]["severity"], "none")
            self.assertNotIn("warnings", result)
            checkpoints = result["drift"]["checkpoints"]
            self.assertEqual((checkpoints["initial"], checkpoints["more_opened"]),
                             ({"severity": "none", "not_reached": True},) * 2)
            self.assertEqual(result["drift"]["extra_checkpoints"], ["initial_in_mode"])

    def test_transient_ui_dump_failure_while_waiting_for_the_mode_is_tolerated(self):
        self.device.ui_failures = 1
        code, result = self.run_scenario()
        self.assertEqual((code, result["scenario_status"], result["ui_retries"]), (0, "PASS", 1))

        self.device = MockCameraAdb()
        code, result = self.run_scenario()
        self.assertNotIn("ui_retries", result)

        self.device = MockCameraAdb()
        self.device.ui_failures = 10 ** 6
        code, result = self.run_scenario(slow=True)
        self.assertEqual((code, result["error"]["code"], result["failure_class"]), (1, "UI_UNAVAILABLE", "environment"))
        self.assertGreaterEqual(result["ui_retries"], 1)

    def test_cleanup_deletes_only_the_video_this_run_created(self):
        code, result = self.run_scenario("--pull-video", "--cleanup-video")
        self.assertEqual(code, 0)
        self.assertEqual(self.device.deleted, [VIDEO_PATH])
        self.assertEqual(result["video"], {"device_path": VIDEO_PATH, "bytes": 4, "deleted": True})

        self.device = MockCameraAdb()
        code, result = self.run_scenario()
        self.assertEqual((self.device.deleted, "deleted" in result["video"]), ([], False))

    def test_failed_cleanup_only_warns(self):
        self.device.rm_fails = True
        code, result = self.run_scenario("--cleanup-video")
        self.assertEqual((code, result["scenario_status"]), (0, "PASS"))
        self.assertEqual((result["video"]["deleted"], result["warnings"]), (False, ["VIDEO_CLEANUP_FAILED"]))


    def test_verify_media_passes_and_removes_the_local_copy_without_pull_video(self):
        self.device.video_bytes = mp4([video_track(5.2), audio_track(5.2)], seconds=5.2)
        with TemporaryDirectory() as output, patch.object(time, "sleep"):
            code, result = self.cli("run", GALAXY_SCENARIO, "--output-dir", output, "--verify-media")
            self.assertEqual((code, result["scenario_status"]), (0, "PASS"))
            self.assertEqual(result["media"]["checks"], {"video_track": "PASS", "frames": "PASS", "duration": "PASS", "audio": "PASS"})
            self.assertEqual((result["media"]["brand"], result["media"]["duration_s"]), ("mp42", 5.2))
            self.assertEqual(result["media"]["tracks"][0]["sample_count"], 156)
            self.assertNotIn("video", result["artifacts"])
            self.assertFalse((Path(output) / result["run_id"] / "portrait-video.mp4").exists())
            self.assertNotIn("warnings", result)

            self.device = MockCameraAdb()
            self.device.video_bytes = mp4([video_track(5.2), audio_track(5.2)], seconds=5.2)
            code, result = self.cli("run", GALAXY_SCENARIO, "--output-dir", output, "--verify-media", "--pull-video")
            self.assertEqual(code, 0)
            self.assertTrue(Path(result["artifacts"]["video"]).is_file())

    def test_verify_media_warns_about_missing_audio_without_failing(self):
        self.device.video_bytes = mp4([video_track(5.2)], seconds=5.2)
        code, result = self.run_scenario("--verify-media")
        self.assertEqual((code, result["scenario_status"], result["warnings"]), (0, "PASS", ["MEDIA_NO_AUDIO"]))
        self.assertEqual(result["media"]["checks"]["audio"], "WARN")

    def test_wrong_duration_fails_as_a_product_problem_and_keeps_the_evidence(self):
        self.device.video_bytes = mp4([video_track(9.0), audio_track(9.0)], seconds=9.0)
        code, result = self.run_scenario("--verify-media", "--cleanup-video")
        self.assertEqual((code, result["scenario_status"]), (1, "FAIL"))
        self.assertEqual((result["error"]["code"], result["failure_class"]), ("MEDIA_DURATION", "product"))
        self.assertEqual(result["media"]["checks"]["duration"], "FAIL")
        self.assertNotIn("failure_screen", result["artifacts"])
        self.assertTrue(result["artifacts"]["video"].endswith("portrait-video.mp4"))
        self.assertEqual(self.device.deleted, [])  # The device file stays as evidence too.

    def test_broken_files_fail_with_their_own_codes(self):
        for data, code in ((mp4([], moov=False), "MEDIA_NO_MOOV"), (mp4([audio_track(5.2)], seconds=5.2), "MEDIA_NO_VIDEO"),
                           (b"\x00\x00\x10\x00free", "MEDIA_MALFORMED")):
            self.device = MockCameraAdb()
            self.device.video_bytes = data
            exit_code, result = self.run_scenario("--verify-media")
            self.assertEqual((exit_code, result["error"]["code"], result["failure_class"]), (1, code, "product"))

    def test_device_file_is_deleted_only_after_the_pull(self):
        self.device.video_bytes = mp4([video_track(5.2), audio_track(5.2)], seconds=5.2)
        code, result = self.run_scenario("--verify-media", "--cleanup-video")
        self.assertEqual((code, self.device.events), (0, ["pull", "rm"]))
        self.assertTrue(result["video"]["deleted"])

    def test_latency_metrics_come_from_log_timestamps(self):
        code, result = self.run_scenario()
        # Start tap at 09:10:02.100 (device clock), RECORDING logged at 02.350; stop at 03.100, IDLE at 03.900.
        self.assertEqual(code, 0)
        self.assertEqual({key: result["metrics"][key] for key in ("record_start_latency_ms", "record_stop_latency_ms")},
                         {"record_start_latency_ms": 250, "record_stop_latency_ms": 800})
        self.assertIsInstance(result["metrics"]["video_ready_ms"], int)
        self.assertGreaterEqual(result["metrics"]["video_ready_ms"], 0)

    def test_metric_that_cannot_be_computed_is_omitted(self):
        # A log stamped before the device clock (for example across New Year) cannot give a latency.
        stamped_early = STARTED.replace("09:10:02.350", "09:09:00.000")
        with patch("tests.camera_fixtures.STARTED", stamped_early):
            code, result = self.run_scenario()
        self.assertEqual(code, 0)
        self.assertNotIn("record_start_latency_ms", result["metrics"])
        self.assertEqual(result["metrics"]["record_stop_latency_ms"], 800)


if __name__ == "__main__":
    unittest.main()
