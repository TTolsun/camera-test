"""Samsung Galaxy Camera Portrait Video scenario."""

import re
import time

from ..adb import RunError
from ..logcat import LOG_LINE


PACKAGE = "com.sec.android.app.camera"
ACTIVITY = f"{PACKAGE}/.Camera"
# The stop gesture and RecordingManager messages were confirmed on this app build.
CAMERA_VERSION = "17.0.00.43"
MODE_TEXT = {"인물 동영상", "portrait video"}
MORE_TEXT = {"더보기", "more"}
START_TEXT = {"녹화 시작", "start recording", "record video"}
LOG_TAGS = ("RecordingManager", "CameraService", "Camera3-Device", "MediaRecorder", "MPEG4Writer")
RECORD_STATE = re.compile(r"changeRecordingState\s*:\s*(\w+)\s*->\s*(\w+)")


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
    require_foreground(device)


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


def require_foreground(device):
    if not device.package_foreground(PACKAGE):
        raise RunError("CAMERA_NOT_FOREGROUND", "Unlock the phone and bring the Galaxy Camera app to the foreground.")


def videos(device):
    # An empty DCIM directory is normal on a device that has never used the camera.
    listing = device.shell(
        "sh", "-c", "if [ -d /sdcard/DCIM/Camera ]; then "
        "find /sdcard/DCIM/Camera -maxdepth 1 -type f -iname '*.mp4' ! -name '.*'; fi"
    )
    return {line for line in listing.splitlines() if line.startswith("/sdcard/DCIM/Camera/")}


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
        new = videos(device) - before
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
    require_foreground(device)
    pid = device.shell("pidof", "-s", PACKAGE)
    if not pid.isdecimal():
        raise RunError("CAMERA_PID_NOT_FOUND", "Could not identify the Galaxy Camera process.")
    baseline = videos(device)
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
        require_foreground(device)
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
                    require_foreground(device)
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


def execute(device, seconds, result):
    point = enter_portrait_video(device, result["steps"])
    result["steps"].append("portrait_video_ready")
    return record(device, point, seconds, result)
