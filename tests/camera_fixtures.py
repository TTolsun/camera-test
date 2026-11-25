"""In-memory ADB handsets and the CLI harness shared by camera tests."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from itertools import count
import json
from pathlib import Path
import shlex
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

import camera_test
from camera_runner import adb
from tests.mp4_builder import audio_track, mp4, video_track


GALAXY_SCENARIO = "galaxy.portrait-video"
GALAXY_PACKAGE = "com.sec.android.app.camera"
LAUNCHER_PACKAGE = "com.sec.android.app.launcher"
OWNER_APP = "com.sds.mchat"
VIDEO_PATH = "/sdcard/DCIM/Camera/mock.mp4"
STARTED = "11-21 09:10:02.350 1234 1234 I RecordingManager: changeRecordingState : STARTING -> RECORDING"
STOPPED = "11-21 09:10:03.900 1234 1234 I RecordingManager: changeRecordingState : STOPPING -> IDLE"


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
        self.wakefulness = "Awake"  # None omits the line, as an Android version without it would.
        self.keyguard = "false"
        self.free_kb = 10_000_000
        self.launch_status = "Status: ok"
        self.record_log = None  # Replaces the RecordingManager log lines when set.
        self.deleted = []
        self.rm_fails = False
        self.ui_failures = 0  # Portrait Video dumps that fail before one succeeds.
        self.video_bytes = b"mock"  # Content of the pulled MP4; its length is also the device file size.
        self.events = []  # Order of device-file operations that matter to --cleanup-video.
        self.top = LAUNCHER_PACKAGE  # Top resumed activity package; launching the camera replaces it.
        self.launcher_output = f"priority=0 preferredOrder=0 match=0x108000\n{LAUNCHER_PACKAGE}/.LauncherActivity"
        self.video_plan = []  # MP4 bytes for the next recordings, consumed in order as each recording stops.
        self.files = []  # MP4s on the device: the first recording is VIDEO_PATH, later ones get their own names.
        self.recordings = 0
        self.commands = []  # Every shell command, to prove what a short-circuited command did not touch.
        self.calls = []  # Every adb invocation (args tuple), including exec-out and pull.
        self.listing = [("mock-phone", "device")]  # (serial, state) rows of `adb devices`.
        self.other_hardware = {}  # Hardware serial answered for listed serials other than mock-phone.
        self.mdns = "List of discovered mdns services\n"
        self.adb_version = "Android Debug Bridge version 1.0.41\nVersion 35.0.2-12147458\nInstalled as C:\\adb.exe"

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
        self.calls.append(args)
        if args == ("version",):
            return self.adb_version
        if args == ("devices",):
            return "List of devices attached\n" + "".join(f"{serial}\t{state}\n" for serial, state in self.listing)
        if args == ("devices", "-l"):
            return "List of devices attached\n" + "".join(
                f"{serial:<22} {state} product:mock model:Mock_Galaxy device:mock transport_id:{index}\n"
                for index, (serial, state) in enumerate(self.listing, 1))
        if args == ("mdns", "services"):
            return self.mdns
        if args[0] == "-s" and args[1] in self.other_hardware:
            assert args[2:] == ("shell", "getprop ro.serialno"), args
            return self.other_hardware[args[1]]
        assert args[:2] == ("-s", "mock-phone"), args
        if args[2] == "exec-out":
            return b""
        if args[2] == "pull":
            assert args[3] in self.files and kwargs["timeout"] == 60
            Path(args[4]).write_bytes(self.video_bytes)
            self.events.append("pull")
            return ""
        assert args[2] == "shell", args
        remote = shlex.split(args[3])
        self.commands.append(remote)
        if remote == ["getprop", "ro.serialno"]:
            return "mock-hardware"
        if remote == ["getprop"]:
            return "[ro.product.model]: [Mock Galaxy]\n[ro.soc.manufacturer]: [Exynos]"
        if remote[:2] == ["dumpsys", "package"]:
            version = self.versions.get(remote[2])
            return f"    versionName={version}" if version else ""
        if remote == ["dumpsys", "activity", "activities"]:
            return f"    topResumedActivity=ActivityRecord{{1 u0 {self.top}/.Main t7}}"
        if remote[:3] == ["cmd", "package", "resolve-activity"]:
            assert remote[3:] == ["--brief", "-a", "android.intent.action.MAIN", "-c", "android.intent.category.HOME"]
            return self.launcher_output
        if remote[:2] == ["am", "start"]:
            assert remote[-1] == f"{GALAXY_PACKAGE}/.Camera"
            self.clocks = 1  # Each run reads its first clock before it launches the camera.
            if self.launch_status == "Status: ok":
                self.top = GALAXY_PACKAGE
            return self.launch_status
        if remote[:2] == ["input", "tap"]:
            point = tuple(remote[2:])
            self.taps.append(point)
            if point == ("5", "5"):
                self.record_taps += 1
                if self.record_taps % 2 == 0:
                    self.files.append(VIDEO_PATH if not self.recordings else f"/sdcard/DCIM/Camera/mock{self.recordings}.mp4")
                    self.recordings += 1
                    if self.video_plan:
                        self.video_bytes = self.video_plan.pop(0)
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
            if self.record_log is not None:
                return self.record_log
            return STOPPED if remote[3] == "11-21 09:10:03.100" else STARTED
        if remote[:3] == ["stat", "-c", "%s"] and remote[3] in self.files:
            return str(len(self.video_bytes))
        if remote[0] == "df":
            assert remote[1:] == ["-k", "/sdcard"]
            return ("Filesystem     1K-blocks    Used Available Use% Mounted on\n"
                    f"/dev/fuse      481226752 1000 {self.free_kb} 46% /storage/emulated")
        if remote[:2] == ["sh", "-c"]:
            if remote[2].startswith("dumpsys power | "):
                return f"  mWakefulness={self.wakefulness}" if self.wakefulness else ""
            if remote[2].startswith("dumpsys window | "):
                return f"    isKeyguardShowing={self.keyguard}" if self.keyguard else ""
            if remote[2].startswith("rm "):
                if self.rm_fails:
                    raise adb.RunError("ADB_ERROR", "rm failed")
                self.deleted.append(shlex.split(remote[2])[1])
                self.files.remove(self.deleted[-1])
                self.events.append("rm")
                return "deleted"
            if remote[2].startswith("uiautomator dump "):
                if self.mode == "portrait" and self.ui_failures:
                    self.ui_failures -= 1
                    return "ERROR: could not get idle state."
                return self.hierarchy()
            if remote[2].startswith("dumpsys media.camera | "):
                return "Number of camera devices: 1"
            if "find /sdcard/DCIM/Camera" in remote[2]:
                return "\n".join(self.files)
        raise AssertionError(remote)


GOOD_MP4 = mp4([video_track(5.2), audio_track(5.2)], seconds=5.2)


VIDEO_MODES = {"Video", "Portrait Video", "Pro Video", "Slow Motion"}
STRIP = ("Photo", "Video")
MODEL = "Mock Galaxy"
KEY = f"{GALAXY_PACKAGE}|{MODEL}|17.0.00.43|"


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
                patch.object(time, "sleep"):
            if slow:  # Every wait_for gives up after its second probe.
                with patch.object(time, "monotonic", side_effect=count(0, 5)):
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
