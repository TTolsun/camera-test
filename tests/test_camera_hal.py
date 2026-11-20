from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import shlex
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import camera_test


OFF = """--------- beginning of main
11-21 09:10:12.000 1111 1112 I ExampleSession: session_started
11-21 09:10:12.100 1113 1114 I ExampleExynosTag: example_applied=0
11-21 09:10:12.200 1113 1114 I ExampleExynosTagExtra: example_applied=1
"""
ON = """--------- beginning of main
11-21 09:11:13.000 1121 1122 I ExampleSession: session_started
11-21 09:11:13.100 1123 1124 I ExampleExynosTag: example_applied=1
11-21 09:11:13.200 1123 1124 I ExampleExynosTag: example_applied=1
"""


class HalCommandTests(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.off = self.root / "off.log"
        self.on = self.root / "on.log"
        self.off.write_text(OFF, encoding="utf-8-sig")
        self.on.write_text(ON, encoding="utf-8")

    def cli(self, *arguments):
        stdout = StringIO()
        with patch.object(sys, "argv", ["camera-test", "hal", *map(str, arguments)]), redirect_stdout(stdout):
            code = camera_test.main()
        return code, json.loads(stdout.getvalue())

    def compare(self, *arguments):
        with patch.object(camera_test, "adb", side_effect=AssertionError("compare tried to use ADB")):
            return self.cli("compare", "--off", self.off, "--on", self.on, *arguments)

    def test_compare_summarizes_tags_and_messages(self):
        code, result = self.compare()
        self.assertEqual(code, 0)
        self.assertIn({"tag": "ExampleExynosTag", "off": 1, "on": 2}, result["tag_changes"])
        code, result = self.compare("--tag", "ExampleExynosTag")
        self.assertEqual(code, 0)
        self.assertEqual(result["message_changes"], [
            {"message": "example_applied=1", "off": 0, "on": 2},
            {"message": "example_applied=0", "off": 1, "on": 0},
        ])

    def test_compare_checks_exact_tag_and_requires_requested_control(self):
        args = ("--tag", "ExampleExynosTag", "--expect-log", r"\bexample_applied=1\b",
                "--control-tag", "ExampleSession", "--control-log", "^session_started$")
        code, result = self.compare(*args)
        self.assertEqual((code, result["status"], result["matches"]), (0, "ON_ONLY", {"off": 0, "on": 2}))

        self.off.write_text(OFF.replace("session_started", "unrelated"), encoding="utf-8")
        code, result = self.compare(*args)
        self.assertEqual((code, result["status"]), (2, "CONTROL_MISSING"))

    def test_compare_distinguishes_no_match_and_matches_in_both(self):
        code, result = self.compare("--tag", "ExampleExynosTag", "--expect-log", "example_applied=9")
        self.assertEqual((code, result["status"]), (2, "NO_MATCH"))
        code, result = self.compare("--tag", "ExampleExynosTag", "--expect-log", "example_applied=")
        self.assertEqual((code, result["status"]), (2, "BOTH_MATCH"))

    def test_compare_reports_empty_or_invalid_inputs(self):
        code, result = self.compare("--tag", "ExampleExynosTag", "--expect-log", "[")
        self.assertEqual((code, result["error"]["code"]), (1, "INVALID_PATTERN"))
        self.off.write_text("--------- beginning of main\n", encoding="utf-8")
        code, result = self.compare()
        self.assertEqual((code, result["error"]["code"]), (1, "EMPTY_LOG"))

    def test_begin_collect_and_compare_two_trials(self):
        stamp_off = "11-21 09:10:11.123"
        stamp_on = "11-21 09:11:12.456"
        timestamps = iter((stamp_off, stamp_on))
        queries = []

        def mock_adb(*args, **kwargs):
            if args == ("devices",):
                return "List of devices attached\nmock-exynos\tdevice\nemulator-5554\tdevice\n"
            self.assertEqual(args[:3], ("-s", "mock-exynos", "shell"))
            remote = shlex.split(args[3])
            if remote[0] == "date":
                return next(timestamps) + "\n"
            if remote == ["getprop", "ro.serialno"]:
                return "mock-exynos-hardware\n"
            self.assertEqual(remote[:3], ["logcat", "-d", "-T"])
            self.assertEqual(remote[4:12], ["-v", "threadtime", "-b", "main", "-b", "system", "-b", "crash"])
            queries.append(remote)
            return {stamp_off: OFF, stamp_on: ON}[remote[3]]

        with patch.object(camera_test, "adb", side_effect=mock_adb):
            code, off = self.cli("begin", "--state", "off", "--output-dir", self.root)
            self.assertEqual((code, off["since"]), (0, stamp_off))
            code, off_log = self.cli("collect", "--trial", off["trial"])
            self.assertEqual((code, off_log["entries"]), (0, 3))
            code, on = self.cli("begin", "--state", "on", "--output-dir", self.root,
                                "--tag", "ExampleSession", "--tag", "ExampleExynosTag")
            self.assertEqual((code, on["since"]), (0, stamp_on))
            code, on_log = self.cli("collect", "--trial", on["trial"])
            self.assertEqual((code, on_log["entries"]), (0, 3))
        self.assertEqual(queries[0][12:], [])
        self.assertEqual(queries[1][12:], ["ExampleSession:V", "ExampleExynosTag:V", "*:S"])

        with patch.object(camera_test, "adb", side_effect=AssertionError("collected trial queried ADB")):
            self.assertEqual(self.cli("collect", "--trial", on["trial"]), (0, on_log))
            code, compared = self.cli("compare", "--off", off_log["logcat"], "--on", on_log["logcat"],
                                      "--tag", "ExampleExynosTag", "--expect-log", "example_applied=1")
            self.assertEqual((code, compared["status"]), (0, "ON_ONLY"))


if __name__ == "__main__":
    unittest.main()
