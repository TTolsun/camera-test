from contextlib import redirect_stdout
from io import StringIO
from itertools import count
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import camera_test
from camera_runner import adb, failures, guidance
from camera_runner.scenarios import galaxy_portrait_video
from tests.mp4_builder import audio_track, mp4, video_track
from tests.test_camera_scenarios import GALAXY_PACKAGE, GALAXY_SCENARIO, LAUNCHER_PACKAGE, OWNER_APP, MockCameraAdb


NEW_VERSION = "18.0.00.1"
GOOD_MP4 = mp4([video_track(5.2), audio_track(5.2)], seconds=5.2)
LONG_MP4 = mp4([video_track(9.0), audio_track(9.0)], seconds=9.0)
UI_COMMANDS = {"am", "input", "pidof"}


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self.device = MockCameraAdb()
        self.device.video_bytes = GOOD_MP4

    def cli(self, *arguments, slow=False):
        output = StringIO()
        with patch.object(sys, "argv", ["camera-test", *map(str, arguments)]), redirect_stdout(output), \
                patch.object(adb, "adb", side_effect=self.device), patch.object(galaxy_portrait_video.time, "sleep"):
            if slow:  # Every wait_for gives up after its first probe.
                with patch.object(galaxy_portrait_video.time, "monotonic", side_effect=count(0, 5)):
                    code = camera_test.main()
            else:
                code = camera_test.main()
        return code, json.loads(output.getvalue())

    def ui_commands(self):
        return [command for command in self.device.commands if command[0] in UI_COMMANDS or "uiautomator" in command[-1]]


class RequireIdleTests(CliTestCase):
    def test_plain_run_ignores_a_busy_phone_and_keeps_its_output(self):
        self.device.top = OWNER_APP
        with TemporaryDirectory() as output:
            code, result = self.cli("run", GALAXY_SCENARIO, "--output-dir", output)
        self.assertEqual((code, result["scenario_status"]), (0, "PASS"))
        self.assertNotIn("foreground", result["preflight"])
        self.assertFalse(any(command[:3] == ["cmd", "package", "resolve-activity"] for command in self.device.commands))

    def test_require_idle_stops_on_another_app_before_any_tap(self):
        self.device.top = OWNER_APP
        with TemporaryDirectory() as output:
            code, result = self.cli("run", GALAXY_SCENARIO, "--output-dir", output, "--require-idle")
        self.assertEqual((code, result["error"]["code"], result["failure_class"]), (1, "PRECHECK_DEVICE_BUSY", "environment"))
        self.assertIn(OWNER_APP, result["error"]["message"])
        self.assertEqual((self.device.taps, self.ui_commands()), ([], []))

    def test_require_idle_accepts_launcher_and_camera_and_tolerates_unknown_signals(self):
        for top, launcher_output, state in ((LAUNCHER_PACKAGE, None, "idle"), (GALAXY_PACKAGE, None, "idle"),
                                            (OWNER_APP, "", "unknown"), (OWNER_APP, "android/com.android.internal.app.ResolverActivity", "unknown")):
            self.device = MockCameraAdb()
            self.device.top = top
            if launcher_output is not None:
                self.device.launcher_output = launcher_output
            with TemporaryDirectory() as output:
                code, result = self.cli("run", GALAXY_SCENARIO, "--output-dir", output, "--require-idle")
            self.assertEqual((code, result["preflight"]["foreground"]["state"]), (0, state), (top, launcher_output))
        self.assertEqual(result["preflight"]["foreground"], {"package": OWNER_APP, "launcher": None, "state": "unknown"})


class TopPackageTests(unittest.TestCase):
    def top(self, text):
        with patch.object(adb, "adb", return_value=text):
            return adb.Device("mock-phone", "id").top_package()

    def test_falls_back_to_resumed_activity_when_the_screen_is_dozing(self):
        self.assertEqual(self.top("  ResumedActivity: ActivityRecord{50477531 u10 com.sds.mchat/.MainActivity t2641}"), OWNER_APP)
        both = ("  ResumedActivity: ActivityRecord{1 u0 a.b/.X t1}\n"
                "    topResumedActivity=ActivityRecord{2 u0 c.d/.Y t2}")
        self.assertEqual(self.top(both), "c.d")
        self.assertIsNone(self.top("nothing here"))


class VerifyBuildTests(CliTestCase):
    def setUp(self):
        super().setUp()
        self.device.versions[GALAXY_PACKAGE] = NEW_VERSION
        self.root = TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.state = Path(self.root.name) / "state" / "verify-state.json"

    def verify(self, *extra, **options):
        return self.cli("verify-build", GALAXY_SCENARIO, "--output-dir", Path(self.root.name) / "verify", *extra, **options)

    def report(self, summary):
        return json.loads(Path(summary["report"]).read_text(encoding="utf-8"))

    def state_entry(self):
        return json.loads(self.state.read_text(encoding="utf-8"))["devices"]["mock-hardware"][GALAXY_SCENARIO]

    def test_unchanged_build_short_circuits_without_touching_the_ui(self):
        self.state.parent.mkdir()
        self.state.write_text(json.dumps({"devices": {"mock-hardware": {GALAXY_SCENARIO: {
            "app_version": NEW_VERSION, "decision": "promote", "report": "old/report.json"}}}}), encoding="utf-8")
        code, summary = self.verify("--if-changed", "--state-file", self.state)
        self.assertEqual((code, summary["status"], summary["previous_decision"]), (0, "unchanged", "promote"))
        self.assertEqual((self.ui_commands(), self.device.taps), ([], []))
        self.assertFalse((Path(self.root.name) / "verify").exists())

        # Another phone, another scenario entry, or a new version is not unchanged.
        self.device.versions[GALAXY_PACKAGE] = "19.0.00.1"
        code, summary = self.verify("--repeat", 1, "--if-changed", "--state-file", self.state)
        self.assertEqual((code, summary["decision"]), (0, "promote"))
        self.assertEqual(self.state_entry()["app_version"], "19.0.00.1")

    def test_promote_writes_a_valid_proposal_and_never_touches_the_builtin_profile(self):
        code, summary = self.verify("--repeat", 3, "--state-file", self.state)
        self.assertEqual((code, summary["decision"], summary["pass_rate"]), (0, "promote", 1.0))
        report = self.report(summary)
        self.assertEqual((report["app_version"], report["verified"]), (NEW_VERSION, False))
        self.assertEqual({key: report["iterations"][key] for key in ("requested", "run", "passed", "pass_rate")},
                         {"requested": 3, "run": 3, "passed": 3, "pass_rate": 1.0})
        self.assertEqual(report["metrics"]["record_start_latency_ms"],
                         {"min": 250, "median": 250, "max": 250, "samples": 3})
        self.assertIn("video_ready_ms", report["metrics"])
        self.assertEqual(report["media"]["video_codecs"], ["hvc1"])
        self.assertEqual((report["media"]["resolutions"], report["media"]["fps"], report["media"]["audio"]),
                         (["1920x1080"], [30.0], [True]))
        self.assertEqual(report["media"]["duration_s"], {"min": 5.2, "max": 5.2})
        self.assertEqual((report["drift"]["worst"], report["failure_classes"], report["stopped_early"]), ("none", {}, None))
        self.assertEqual([run["status"] for run in report["runs"]], ["PASS"] * 3)
        self.assertTrue(all(Path(run["result"]).is_file() for run in report["runs"]))
        # Every run folder lives inside the batch folder, and the device files were cleaned up.
        batch = Path(summary["report"]).parent
        self.assertTrue(all(Path(run["result"]).parent.parent == batch for run in report["runs"]))
        self.assertEqual(len(self.device.deleted), 3)

        proposal = report["proposal"]
        code, validated = self.cli("profile", "validate", proposal["profile"])
        self.assertEqual((code, validated["verified_versions"]), (0, ["17.0.00.43", NEW_VERSION]))
        self.assertEqual(proposal["golden_from"], report["runs"][0]["run_id"])
        golden = Path(proposal["golden"])
        # The first iteration started in Photo mode and later ones in Portrait Video; the golden covers both.
        self.assertEqual(sorted(path.name for path in golden.iterdir()),
                         ["golden.json", "initial.json", "initial_in_mode.json", "mode_ready.json", "more_opened.json"])
        self.assertEqual(json.loads((golden / "golden.json").read_text(encoding="utf-8"))["checkpoints"],
                         ["initial", "initial_in_mode", "mode_ready", "more_opened"])
        self.assertEqual(self.cli("screens", "compare", "--golden", golden, "--run", Path(report["runs"][0]["result"]).parent)[1]["severity"], "none")
        text = (batch / "report.md").read_text(encoding="utf-8")
        self.assertIn("promote", text.splitlines()[2])
        self.assertIn(proposal["profile"], text)
        self.assertIn("build.py camera-test.pyz", text)
        self.assertEqual(self.state_entry()["decision"], "promote")
        self.assertEqual([path.name for path in self.state.parent.iterdir()], ["verify-state.json"])
        self.assertEqual(self.cli("profile", "show", GALAXY_SCENARIO, "--output", Path(self.root.name) / "p.json")[0], 0)
        self.assertEqual(json.loads((Path(self.root.name) / "p.json").read_text(encoding="utf-8"))["verified_versions"], ["17.0.00.43"])

    def test_verified_version_with_all_passing_is_a_baseline(self):
        self.device.versions[GALAXY_PACKAGE] = "17.0.00.43"
        code, summary = self.verify("--repeat", 2)
        report = self.report(summary)
        self.assertEqual((code, summary["decision"], report["proposal"], report["verified"]), (0, "baseline_ok", None, True))
        self.assertFalse((Path(summary["report"]).parent / "proposal").exists())

    def test_two_consecutive_automation_failures_stop_the_batch(self):
        self.device.renamed_ids = {"shooting_mode_portrait_video_main": "renamed"}
        code, summary = self.verify("--repeat", 5, "--state-file", self.state, slow=True)
        report = self.report(summary)
        self.assertEqual((code, summary["decision"]), (1, "needs_profile_fix"))
        self.assertEqual((report["iterations"]["run"], report["iterations"]["passed"], report["stopped_early"]), (2, 0, "automation"))
        self.assertEqual((report["failure_classes"], report["error_codes"]), ({"automation": 2}, {"MODE_NOT_READY": 1, "MODE_NOT_FOUND": 1}))
        # The app remembers Portrait Video, so the second run starts in the mode and cannot find the renamed marker.
        self.assertIsNone(report["proposal"])
        self.assertIn("profile show", (Path(summary["report"]).parent / "report.md").read_text(encoding="utf-8"))
        # The broken build is remembered, so an hourly --if-changed task does not retry it.
        self.assertEqual(self.state_entry()["decision"], "needs_profile_fix")
        code, summary = self.verify("--if-changed", "--state-file", self.state)
        self.assertEqual((code, summary["status"]), (0, "unchanged"))

    def test_breaking_drift_needs_a_profile_fix_even_when_every_run_passes(self):
        self.device.versions[GALAXY_PACKAGE] = "17.0.00.43"
        with TemporaryDirectory() as root:
            code, result = self.cli("run", GALAXY_SCENARIO, "--output-dir", root, "--capture-screens")
            golden = Path(root) / "golden"
            self.cli("screens", "promote", "--run", Path(result["artifacts"]["result"]).parent, "--output", golden)
            self.device = MockCameraAdb()
            self.device.video_bytes = GOOD_MP4
            self.device.renamed_ids = {"shooting_mode_list": "shooting_mode_list_v2"}
            code, summary = self.verify("--repeat", 2, "--golden", golden)
        report = self.report(summary)
        self.assertEqual((code, summary["decision"], summary["pass_rate"]), (1, "needs_profile_fix", 1.0))
        self.assertEqual(report["drift"]["worst"], "breaking")
        self.assertEqual(report["drift"]["profile_selectors_missing"], [{"key": "mode_strip", "selector": "shooting_mode_list"}])
        self.assertIn("UI_DRIFT_BREAKING", report["warnings"])

    def test_busy_phone_stops_at_the_first_iteration_with_exit_3(self):
        self.device.top = OWNER_APP
        code, summary = self.verify("--repeat", 4, "--state-file", self.state)
        report = self.report(summary)
        self.assertEqual((code, summary["decision"]), (3, "environment_blocked"))
        self.assertEqual((report["iterations"]["run"], report["stopped_early"], report["error_codes"]),
                         (1, "environment", {"PRECHECK_DEVICE_BUSY": 1}))
        self.assertEqual((self.device.taps, self.ui_commands()), ([], []))
        self.assertEqual(self.state_entry()["decision"], "environment_blocked")

        # Nothing was learned about the build, so the next scheduled call tries again.
        self.device.top = GALAXY_PACKAGE
        code, summary = self.verify("--repeat", 1, "--if-changed", "--state-file", self.state)
        self.assertEqual((code, summary["decision"]), (0, "promote"))

    def test_product_failures_without_automation_failures(self):
        self.device.video_bytes = LONG_MP4
        code, summary = self.verify("--repeat", 2, "--state-file", self.state)
        report = self.report(summary)
        self.assertEqual((code, summary["decision"]), (1, "product_failure"))
        self.assertEqual((report["failure_classes"], report["error_codes"], report["iterations"]["run"]),
                         ({"product": 2}, {"MEDIA_DURATION": 2}, 2))
        self.assertEqual(report["media"]["duration_s"], {"min": 9.0, "max": 9.0})
        self.assertEqual(self.device.deleted, [])  # Failed recordings stay on the phone as evidence.
        self.assertEqual(self.state_entry()["decision"], "product_failure")

    def test_mixed_results_that_fit_no_class_are_flaky(self):
        self.device.video_plan = [LONG_MP4, GOOD_MP4]
        with patch.dict(failures.CLASS_BY_CODE):
            del failures.CLASS_BY_CODE["MEDIA_DURATION"]
            code, summary = self.verify("--repeat", 2)
        report = self.report(summary)
        self.assertEqual((code, summary["decision"], summary["pass_rate"]), (1, "flaky", 0.5))
        self.assertEqual((report["failure_classes"], report["iterations"]["intermittent"]), ({"unknown": 1}, True))

    def test_baseline_flags_only_large_metric_growth(self):
        baseline = Path(self.root.name) / "baseline.json"
        # Start latency 100 -> 250 ms is +150% and +150 ms: regression. Stop 760 -> 800 is +40 ms: not.
        baseline.write_text(json.dumps({"app_version": "17.0.00.43", "metrics": {
            "record_start_latency_ms": {"median": 100}, "record_stop_latency_ms": {"median": 760}}}), encoding="utf-8")
        code, summary = self.verify("--repeat", 1, "--baseline", baseline)
        report = self.report(summary)
        self.assertEqual((code, summary["decision"]), (0, "promote"))
        self.assertEqual(report["baseline"]["regressions"], ["record_start_latency_ms"])
        self.assertEqual(report["baseline"]["metrics"]["record_stop_latency_ms"],
                         {"previous_median": 760, "median": 800, "change_ms": 40, "regression": False})
        self.assertIn("METRIC_REGRESSION", report["warnings"])

        # +90 ms is below the absolute floor even though it is +56%.
        self.device = MockCameraAdb()
        self.device.video_bytes = GOOD_MP4
        self.device.versions[GALAXY_PACKAGE] = NEW_VERSION
        baseline.write_text(json.dumps({"metrics": {"record_start_latency_ms": {"median": 160}}}), encoding="utf-8")
        code, summary = self.verify("--repeat", 1, "--baseline", baseline)
        report = self.report(summary)
        self.assertEqual((report["baseline"]["regressions"], "METRIC_REGRESSION" in report["warnings"]), ([], False))

    def test_unreadable_baseline_or_missing_app_stops_before_any_operation(self):
        code, result = self.verify("--baseline", Path(self.root.name) / "missing.json")
        self.assertEqual((code, result["error"]["code"], self.ui_commands()), (1, "INVALID_BASELINE", []))
        del self.device.versions[GALAXY_PACKAGE]
        code, result = self.verify()
        self.assertEqual((code, result["error"]["code"]), (1, "CAMERA_APP_NOT_FOUND"))
        self.assertEqual(self.ui_commands(), [])

    def test_summary_and_report_carry_a_next_action_for_each_decision(self):
        code, summary = self.verify("--repeat", 1)
        report = self.report(summary)
        self.assertEqual(summary["decision"], "promote")
        self.assertIn(report["proposal"]["profile"], summary["next_action"])
        self.assertIn(NEW_VERSION, summary["next_action"])
        self.assertEqual(report["next_action"], summary["next_action"])
        self.assertIn(summary["next_action"], Path(report["artifacts"]["markdown"]).read_text(encoding="utf-8"))

        self.device = MockCameraAdb()
        self.device.video_bytes = GOOD_MP4
        self.device.top = OWNER_APP
        code, summary = self.verify("--repeat", 1)
        self.assertEqual(summary["decision"], "environment_blocked")
        self.assertIn(OWNER_APP, summary["next_action"])

        self.device = MockCameraAdb()
        self.device.video_bytes = LONG_MP4
        self.device.versions[GALAXY_PACKAGE] = NEW_VERSION
        code, summary = self.verify("--repeat", 1)
        self.assertEqual(summary["decision"], "product_failure")
        self.assertIn("logcat.txt", summary["next_action"])

        self.device = MockCameraAdb()
        self.device.video_bytes = GOOD_MP4
        code, summary = self.verify("--repeat", 1)  # 17.0.00.43 is verified: baseline_ok.
        self.assertEqual(summary["decision"], "baseline_ok")
        self.assertIn("--baseline", summary["next_action"])

    def test_profile_fix_flaky_and_unchanged_next_actions(self):
        report = {"scenario": GALAXY_SCENARIO, "app_version": NEW_VERSION, "proposal": None,
                  "runs": [{"status": "FAIL", "result": "runs/a/result.json", "failure_class": "automation", "error_code": "MODE_NOT_READY"}]}
        fix = guidance.decision_next_action("needs_profile_fix", report)
        self.assertIn("runs/a/result.json", fix)
        self.assertIn(f"profile show {GALAXY_SCENARIO} --output", fix)
        self.assertIn("--repeat", guidance.decision_next_action("flaky", report))

        self.state.parent.mkdir()
        self.state.write_text(json.dumps({"devices": {"mock-hardware": {GALAXY_SCENARIO: {
            "app_version": NEW_VERSION, "decision": "promote", "report": "old/report.json"}}}}), encoding="utf-8")
        code, summary = self.verify("--if-changed", "--state-file", self.state)
        self.assertIn("promote", summary["next_action"])


if __name__ == "__main__":
    unittest.main()
