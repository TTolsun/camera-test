#!/usr/bin/env python3
import argparse
from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from uuid import uuid4
from xml.etree import ElementTree

from camera_checks import load_checks, resolve_check


PACKAGE = "com.sec.android.app.camera"
ACTIVITY = f"{PACKAGE}/.Camera"
SCENARIO = "galaxy.portrait-video"
VERSION = "1.0.0"
# The stop gesture and RecordingManager messages were confirmed on this app build.
CAMERA_VERSION = "17.0.00.43"
MODE_TEXT = {"인물 동영상", "portrait video"}
MORE_TEXT = {"더보기", "more"}
START_TEXT = {"녹화 시작", "start recording", "record video"}
LOG_TAGS = ("RecordingManager", "CameraService", "Camera3-Device", "MediaRecorder", "MPEG4Writer")
LOG_LINE = re.compile(r"^\S+\s+\S+\s+(\d+)\s+\d+\s+[VDIWEFAS]\s+([^:]+?)\s*:\s*(.*)$")
RECORD_STATE = re.compile(r"changeRecordingState\s*:\s*(\w+)\s*->\s*(\w+)")


class RunError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def adb_path():
    if os.environ.get("CAMERA_TEST_ADB"):
        return os.environ["CAMERA_TEST_ADB"]
    if on_path := shutil.which("adb"):
        return on_path
    sdk_roots = [os.environ.get("ANDROID_HOME"), os.environ.get("ANDROID_SDK_ROOT")]
    if local_app_data := os.environ.get("LOCALAPPDATA"):
        sdk_roots.append(Path(local_app_data) / "Android" / "Sdk")
    if sys.platform == "darwin":
        sdk_roots.append(Path.home() / "Library" / "Android" / "sdk")
    sdk_roots.append(Path.home() / "Android" / "Sdk")
    for root in filter(None, sdk_roots):
        executable = "adb.exe" if os.name == "nt" else "adb"
        candidate = Path(root) / "platform-tools" / executable
        if candidate.is_file():
            return str(candidate)
    raise RunError("ADB_NOT_FOUND", "Install Android SDK Platform-Tools or set CAMERA_TEST_ADB.")


def adb(*args, timeout=12, binary=False):
    try:
        result = subprocess.run(
            [adb_path(), *args], capture_output=True, timeout=timeout, check=True
        )
    except FileNotFoundError as exc:
        raise RunError("ADB_NOT_FOUND", "Check the CAMERA_TEST_ADB path.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RunError("ADB_TIMEOUT", f"ADB timed out while running {args[0]}.") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", errors="replace").strip()
        raise RunError("ADB_ERROR", detail[:240] or "ADB command failed.") from exc
    return result.stdout if binary else result.stdout.decode("utf-8", errors="replace").replace("\r\n", "\n")


def select_device(serial):
    devices = {}
    for line in adb("devices").splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 2:
            devices[fields[0]] = fields[1]
    if serial:
        if devices.get(serial) == "device":
            return serial
        raise RunError("DEVICE_UNAVAILABLE", f"Requested device state: {devices.get(serial, 'not listed')}.")
    physical = [key for key, state in devices.items() if state == "device" and not key.startswith("emulator-")]
    if len(physical) == 1:
        return physical[0]
    if physical:
        raise RunError("DEVICE_REQUIRED", "More than one device is ready. Specify --device.")
    raise RunError("NO_DEVICE", "Connect and authorize a Galaxy phone with ADB.")


@contextmanager
def device_lock(serial):
    key = sha256(serial.encode()).hexdigest()[:24]
    with (Path(tempfile.gettempdir()) / f"camera-test-{key}.lock").open("ab+") as lock:
        try:
            lock.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RunError("DEVICE_BUSY", "Another camera-test process has locked this device.") from exc
        yield


class Device:
    def __init__(self, serial, run_id):
        self.serial = serial
        self.dump_path = f"/data/local/tmp/camera-test-{run_id}.xml"

    def shell(self, *args, timeout=12):
        return adb("-s", self.serial, "shell", shlex.join(args), timeout=timeout).strip()

    def ui(self):
        path = shlex.quote(self.dump_path)
        script = f"uiautomator dump {path} >/dev/null && cat {path}; rm -f {path}"
        data = self.shell("sh", "-c", script, timeout=14)
        try:
            return ElementTree.fromstring(data)
        except ElementTree.ParseError as exc:
            raise RunError("UI_UNAVAILABLE", "Camera UI hierarchy is unavailable. Check unlock state or active recording.") from exc

    def tap(self, point):
        x, y = point
        self.shell("input", "tap", str(x), str(y))

    def camera_foreground(self):
        activities = self.shell("dumpsys", "activity", "activities")
        return bool(re.search(rf"topResumedActivity=[^\n]*\s{re.escape(PACKAGE)}/", activities))

    def require_foreground(self):
        if not self.camera_foreground():
            raise RunError("CAMERA_NOT_FOREGROUND", "Unlock the phone and bring the Galaxy Camera app to the foreground.")

    def clock(self):
        stamp = self.shell("date", "+%m-%d %H:%M:%S.%3N")
        if not re.fullmatch(r"\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}", stamp):
            raise RunError("DEVICE_CLOCK", "Could not read a logcat timestamp from the device.")
        return stamp

    def logs(self, since, tags):
        return self.shell(
            "logcat", "-d", "-T", since, "-v", "threadtime", "-b", "main", "-b", "system",
            "-b", "crash", *(f"{tag}:V" for tag in tags), "*:S"
        )

    def videos(self):
        # An empty DCIM directory is normal on a device that has never used the camera.
        listing = self.shell(
            "sh", "-c", "if [ -d /sdcard/DCIM/Camera ]; then "
            "find /sdcard/DCIM/Camera -maxdepth 1 -type f -iname '*.mp4' ! -name '.*'; fi"
        )
        return {line for line in listing.splitlines() if line.startswith("/sdcard/DCIM/Camera/")}

    def metadata(self):
        properties = self.shell("getprop")
        props = dict(re.findall(r"^\[([^\]]+)\]: \[(.*)\]$", properties, re.M))
        version = re.search(r"^\s*versionName=(\S+)", self.shell("dumpsys", "package", PACKAGE), re.M)
        return {
            "model": props.get("ro.product.model"),
            "manufacturer": props.get("ro.product.manufacturer"),
            "android": props.get("ro.build.version.release"),
            "board_platform": props.get("ro.board.platform"),
            "soc_model": props.get("ro.soc.model"),
            "soc_manufacturer": props.get("ro.soc.manufacturer"),
            "soc_vendor": soc_vendor(props),
            "camera_version": version[1] if version else None,
        }


def soc_vendor(props):
    manufacturer = props.get("ro.soc.manufacturer", "").casefold()
    if manufacturer in {"qti", "qualcomm", "qualcomm technologies, inc.", "qcom"}:
        return "qualcomm"
    if manufacturer in {"exynos", "samsung exynos", "samsung lsi", "samsung system lsi"}:
        return "exynos"
    hardware = {props.get(key, "").casefold() for key in (
        "ro.hardware", "ro.boot.hardware", "ro.board.platform", "ro.soc.model"
    )}
    if "qcom" in hardware:
        return "qualcomm"
    if any(re.fullmatch(r"exynos(?:[ _-]?\d+)?|s5e\d{4}", name) for name in hardware):
        return "exynos"
    return "unknown"


def wait_for(probe, timeout, code, message, interval=0.25):
    deadline = time.monotonic() + timeout
    while True:
        if value := probe():
            return value
        if time.monotonic() >= deadline:
            raise RunError(code, message)
        time.sleep(interval)


def find_node(root, resource_id, labels=None):
    for node in root.iter("node"):
        if node.get("package") != PACKAGE or node.get("resource-id") != f"{PACKAGE}:id/{resource_id}":
            continue
        values = {node.get(key, "").strip().casefold() for key in ("text", "content-desc")}
        if labels is None or labels & values:
            return node
    return None


def center(node):
    bounds = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
    if bounds:
        left, top, right, bottom = map(int, bounds.groups())
        if left < right and top < bottom:
            return (left + right) // 2, (top + bottom) // 2
    raise RunError("INVALID_UI_BOUNDS", "Camera control is not visible on screen.")


def portrait_ready(root):
    return find_node(root, "shooting_mode_portrait_video_main") is not None


def launch_camera(device):
    launch = device.shell(
        "am", "start", "-W", "-a", "android.intent.action.MAIN", "-c",
        "android.intent.category.LAUNCHER", "-n", ACTIVITY
    )
    if "Status: ok" not in launch:
        raise RunError("CAMERA_LAUNCH_FAILED", "Galaxy Camera could not be launched.")
    device.require_foreground()


def enter_portrait_video(device, steps):
    launch_camera(device)

    root = device.ui()
    if not portrait_ready(root):
        if (back := find_node(root, "instant_mode_back_button")) is not None:
            device.tap(center(back))
            root = device.ui()
        tile = find_node(root, "more_grid_list_text", MODE_TEXT)
        if tile is None:
            pinned = find_node(root, "shooting_mode_item_button", MODE_TEXT)
            if pinned is not None:
                tile = pinned
            elif (more := find_node(root, "shooting_mode_item_button", MORE_TEXT)) is not None:
                device.tap(center(more))
                tile = find_node(device.ui(), "more_grid_list_text", MODE_TEXT)
                if tile is not None:
                    steps.append("more_opened")
        if tile is None:
            raise RunError("MODE_NOT_FOUND", "Portrait Video was not found on the Camera mode strip or in More.")
        device.tap(center(tile))

    def ready_button():
        state = device.ui()
        if portrait_ready(state):
            node = find_node(state, "center_button_container", START_TEXT)
            if node is not None and node.get("enabled") == "true" and node.get("clickable") == "true":
                return center(node)
        return None

    return wait_for(ready_button, 8, "MODE_NOT_READY", "Portrait Video and its Record button were not ready.")


def mode_labels(root, resource_id):
    exact_id = f"{PACKAGE}:id/{resource_id}"
    return {
        text for node in root.iter("node")
        if node.get("package") == PACKAGE and node.get("resource-id") == exact_id
        and (text := node.get("text", "").strip()) and text.casefold() not in MORE_TEXT
    }


def scan_modes(device):
    launch_camera(device)
    initial = device.ui()
    root = initial
    warnings = []
    title = find_node(initial, "instant_mode_title")
    strip = find_node(initial, "shooting_mode_list")
    initial_mode = title.get("text") if title is not None else None
    if initial_mode is None and strip is not None:
        initial_mode = strip.get("content-desc", "").split(",", 1)[0].strip() or None

    if (back := find_node(initial, "instant_mode_back_button")) is not None:
        device.tap(center(back))
        root = device.ui()
    strip_modes = mode_labels(root, "shooting_mode_item_button")
    more_modes = mode_labels(root, "more_grid_list_text")
    opened_more = bool(more_modes)
    if not opened_more:
        more = find_node(root, "shooting_mode_item_button", MORE_TEXT)
        if more is not None:
            device.tap(center(more))
            root = device.ui()
            strip_modes.update(mode_labels(root, "shooting_mode_item_button"))
            more_modes = mode_labels(root, "more_grid_list_text")
            opened_more = bool(more_modes)
    if not opened_more:
        warnings.append("MORE_MENU_NOT_OBSERVED")

    if initial_mode and initial_mode.casefold() not in MORE_TEXT:
        labels = {initial_mode.casefold()}
        restore = find_node(root, "more_grid_list_text", labels) if title is not None else None
        if restore is None:
            restore = find_node(root, "shooting_mode_item_button", labels)
        if restore is not None:
            device.tap(center(restore))
            restored = device.ui()
            actual_title = find_node(restored, "instant_mode_title", labels)
            actual_strip = find_node(restored, "shooting_mode_list")
            actual_mode = actual_strip.get("content-desc", "").split(",", 1)[0].strip().casefold() if actual_strip is not None else None
            if actual_title is None and actual_mode not in labels:
                warnings.append("INITIAL_MODE_RESTORE_UNVERIFIED")
        else:
            warnings.append("INITIAL_MODE_RESTORE_UNAVAILABLE")

    observed = strip_modes | more_modes
    if title is not None and initial_mode:
        observed.add(initial_mode)
    portrait_seen = portrait_ready(initial) or any(mode.casefold() in MODE_TEXT for mode in observed)
    return {"observed_modes": sorted(observed), "mode_strip": sorted(strip_modes),
            "more_menu": sorted(more_modes), "warnings": warnings}, portrait_seen


def inspect_device(args, checks):
    serial = select_device(args.device)
    device = Device(serial, uuid4().hex[:12])
    with device_lock(device.shell("getprop", "ro.serialno") or serial):
        info = device.metadata()
        vendor = info["soc_vendor"]
        result = {
            "tool_version": VERSION,
            "device": info,
            "scenario": {"name": SCENARIO, "status": "unknown", "reason": "UI_SCAN_SKIPPED"},
            "checks": {
                "configured": args.checks_file is not None,
                "registered_for_device": [
                    {"name": item.name, "source": item.source, **({"vendor": item.vendor} if item.vendor else {})}
                    for item in checks if item.vendor is None or item.vendor == vendor
                ],
            },
        }
        try:
            service = device.shell("sh", "-c", "dumpsys media.camera | "
                                   "grep -E '^Number of camera devices:|^== Camera Provider HAL ' || true")
            number = re.search(r"^Number of camera devices: (\d+)", service, re.M)
            result["camera_service"] = {
                "camera_count": int(number[1]) if number else None,
                "providers": re.findall(r"^== Camera Provider HAL ([^\s]+)", service, re.M),
            }
        except RunError as exc:
            result["camera_service_error"] = exc.code
        if info["camera_version"] is None:
            result["scenario"].update(status="unavailable", reason="CAMERA_APP_NOT_FOUND")
            return result
        if not args.no_ui:
            try:
                result["camera_ui"], seen = scan_modes(device)
                result["scenario"].update(status="runnable" if seen else "unavailable",
                                          reason="MODE_OBSERVED" if seen else "MODE_NOT_OBSERVED")
            except RunError as exc:
                result["camera_ui_error"] = exc.code
                result["scenario"].update(reason=exc.code)
        if info["camera_version"] != CAMERA_VERSION:
            result["scenario"].update(status="unavailable", reason="UNSUPPORTED_CAMERA_VERSION")
        return result


def record_transitions(device, since, pid):
    for line in device.logs(since, ("RecordingManager",)).splitlines():
        entry = LOG_LINE.match(line)
        if entry and entry[1] == pid and (transition := RECORD_STATE.search(entry[3])):
            yield transition.groups()


def latest_record_state(device, since, pid):
    transitions = list(record_transitions(device, since, pid))
    return transitions[-1][1] if transitions else None


def recording_finished(device, since, pid):
    return ("STOPPING", "IDLE") in record_transitions(device, since, pid)


def verify_video(device, before):
    sizes = {}

    def saved():
        new = device.videos() - before
        if len(new) > 1:
            raise RunError("AMBIGUOUS_VIDEO", "Multiple new MP4s appeared in the Camera folder.")
        if not new:
            return None
        path = next(iter(new))
        length = device.shell("stat", "-c", "%s", path)
        if length.isdecimal():
            size = int(length)
            if size > 0 and sizes.get(path) == size:
                return {"device_path": path, "bytes": size}
            sizes[path] = size
        return None

    return wait_for(saved, 10, "VIDEO_NOT_SAVED", "A new nonempty Camera MP4 was not found.")


def record(device, point, seconds, result):
    device.require_foreground()
    pid = device.shell("pidof", "-s", PACKAGE)
    if not pid.isdecimal():
        raise RunError("CAMERA_PID_NOT_FOUND", "Could not identify the Galaxy Camera process.")
    baseline = device.videos()
    start_clock = device.clock()
    stop_attempted = False
    ended = False
    armed = False
    try:
        armed = True
        device.tap(point)
        wait_for(lambda: latest_record_state(device, start_clock, pid) == "RECORDING", 5,
                 "RECORD_START_UNVERIFIED", "Camera log did not confirm the recording start.")
        result["steps"].append("recording_started")
        time.sleep(seconds)
        device.require_foreground()
        if latest_record_state(device, start_clock, pid) != "RECORDING":
            raise RunError("RECORDING_ENDED_EARLY", "The camera stopped recording before the planned stop.")
        stop_clock = device.clock()
        stop_attempted = True
        device.tap(point)
        wait_for(lambda: recording_finished(device, stop_clock, pid),
                 6, "RECORD_STOP_UNVERIFIED", "Camera log did not confirm that recording ended.")
        ended = True
        result["steps"].append("recording_stopped")
    finally:
        if armed and not ended:
            result["recording_may_be_active"] = True
            try:
                state = latest_record_state(device, start_clock, pid)
                if state == "RECORDING" and not stop_attempted:
                    device.require_foreground()
                    cleanup_clock = device.clock()
                    device.tap(point)
                    wait_for(lambda: recording_finished(device, cleanup_clock, pid),
                             6, "CLEANUP_UNVERIFIED", "Recording cleanup was not confirmed.")
                    result["recording_may_be_active"] = False
                elif state in {"STOPPING", "IDLE", "PREPARING", "PREPARED"}:
                    result["recording_may_be_active"] = False
            except RunError:
                pass
    result["video"] = verify_video(device, baseline)
    result["steps"].append("video_saved")
    return start_clock


def capture_failure(device, output, result):
    try:
        if not device.camera_foreground():
            return
        image = adb("-s", device.serial, "exec-out", "screencap", "-p", binary=True)
        if image.startswith(b"\x89PNG\r\n\x1a\n"):
            path = output / "failure.png"
            path.write_bytes(image)
            result["artifacts"]["failure_screen"] = str(path)
    except (RunError, OSError):
        pass


def run(args, checks, result, output):
    selected = select_device(args.device)
    device = Device(selected, result["run_id"])
    lock_key = device.shell("getprop", "ro.serialno") or selected
    with device_lock(lock_key):
        result["device"] = device.metadata()
        if result["device"]["camera_version"] != CAMERA_VERSION:
            raise RunError("UNSUPPORTED_CAMERA_VERSION", f"This profile requires Galaxy Camera {CAMERA_VERSION}.")

        check, reason = resolve_check(checks, args.check, result["device"]["soc_vendor"])
        if args.check:
            result["feature"]["reason"] = reason
        since = device.clock()
        tags = dict.fromkeys(LOG_TAGS)
        if check:
            tags[check.tag] = None
            result["feature"]["source"] = check.source
            if check.vendor:
                result["feature"]["vendor"] = check.vendor
        try:
            point = enter_portrait_video(device, result["steps"])
            result["steps"].append("portrait_video_ready")
            recording_clock = record(device, point, args.duration, result)
            result["scenario_status"] = "PASS"
            if check:
                feature_log = device.logs(recording_clock, (check.tag,))
                matched = any(entry[2] == check.tag and check.expected.search(entry[3])
                              for line in feature_log.splitlines() if (entry := LOG_LINE.match(line)))
                result["feature"]["status"] = "PASS" if matched else "UNVERIFIED"
                if matched:
                    result["feature"].pop("reason", None)
                else:
                    result["feature"]["reason"] = "EXPECTED_LOG_NOT_FOUND"
            if args.pull_video:
                clip = output / "portrait-video.mp4"
                try:
                    adb("-s", selected, "pull", result["video"]["device_path"], str(clip), timeout=60)
                    if clip.stat().st_size != result["video"]["bytes"]:
                        raise RunError("VIDEO_COPY_INCOMPLETE", "Local MP4 size does not match the device file.")
                except (RunError, OSError):
                    clip.unlink(missing_ok=True)
                    raise
                result["artifacts"]["video"] = str(clip)
        except (RunError, KeyboardInterrupt):
            if result["scenario_status"] != "PASS":
                capture_failure(device, output, result)
            raise
        finally:
            try:
                logs = output / "logcat.txt"
                logs.write_text(device.logs(since, tags) + "\n", encoding="utf-8")
                result["artifacts"]["logcat"] = str(logs)
            except (RunError, OSError):
                result["log_capture_error"] = "LOG_CAPTURE_FAILED"


def read_checks(path):
    try:
        return load_checks(path)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, re.error) as exc:
        raise RunError("INVALID_CHECKS_FILE", f"Check definition could not be read: {exc}") from exc


def main():
    parser = argparse.ArgumentParser(description="Run repeatable camera scenarios on an ADB device.")
    parser.add_argument("--version", action="version", version=VERSION)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="List runnable scenarios as JSON.")
    inspection = commands.add_parser("inspect", help="Discover modes and compatible checks on the attached device.")
    inspection.add_argument("--device", help="ADB serial. Optional when exactly one physical device is ready.")
    inspection.add_argument("--checks-file", type=Path, help="JSON definitions for named log checks.")
    inspection.add_argument("--no-ui", action="store_true", help="Read device and camera service information without opening Camera.")
    inspection.add_argument("--pretty", action="store_true", help="Indent inspection JSON for terminal reading.")
    command = commands.add_parser("run", help="Run a camera scenario and emit a short JSON result.")
    command.add_argument("scenario", choices=[SCENARIO])
    command.add_argument("--device", help="ADB serial. Optional when exactly one physical device is ready.")
    command.add_argument("--duration", type=int, choices=range(1, 61), metavar="SECONDS", default=5)
    command.add_argument("--output-dir", type=Path, default=Path("runs"))
    command.add_argument("--pull-video", action="store_true", help="Also copy the new MP4 to the run folder.")
    command.add_argument("--check", help="Optional named feature check.")
    command.add_argument("--checks-file", type=Path, help="JSON definitions for named log checks.")
    args = parser.parse_args()
    if args.command == "list":
        print(json.dumps([SCENARIO]))
        return 0
    if args.command == "inspect":
        try:
            result = inspect_device(args, read_checks(args.checks_file))
            exit_code = 0
        except (RunError, OSError) as exc:
            result = {"error": {"code": getattr(exc, "code", "IO_ERROR"), "message": str(exc)}}
            exit_code = 1
        if args.pretty and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        print(json.dumps(result, ensure_ascii=not args.pretty, indent=2 if args.pretty else None))
        return exit_code
    if args.checks_file and not args.check:
        parser.error("--checks-file requires --check")

    run_id = uuid4().hex[:12]
    output = args.output_dir.resolve() / run_id
    result = {"tool_version": VERSION, "run_id": run_id, "scenario": args.scenario, "scenario_status": "FAIL",
              "requested_seconds": args.duration, "steps": [], "artifacts": {}}
    if args.check:
        result["feature"] = {"name": args.check, "status": "UNVERIFIED", "reason": "CHECK_NOT_REGISTERED"}
    try:
        output.mkdir(parents=True)
        run(args, read_checks(args.checks_file), result, output)
    except RunError as exc:
        result["error"] = {"code": exc.code, "message": str(exc)}
    except OSError as exc:
        result["error"] = {"code": "IO_ERROR", "message": str(exc)[:240]}
    except KeyboardInterrupt:
        result["error"] = {"code": "INTERRUPTED", "message": "Camera test was interrupted."}

    if output.is_dir():
        path = output / "result.json"
        result["artifacts"]["result"] = str(path)
        try:
            path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError:
            result["artifacts"].pop("result")
            result["error"] = {"code": "RESULT_WRITE_FAILED", "message": "Could not save result.json."}
    print(json.dumps(result))
    if "error" in result or "log_capture_error" in result or result["scenario_status"] != "PASS":
        return 1
    return 2 if args.check and result["feature"]["status"] != "PASS" else 0


if __name__ == "__main__":
    sys.exit(main())
