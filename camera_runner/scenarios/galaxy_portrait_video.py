"""Samsung Galaxy Camera Portrait Video scenario."""

import re
import time

from ..adb import RunError
from ..logcat import LOG_LINE


# Built-in profile. The selectors, labels and recording-state log were confirmed on SM-S936N
# (Qualcomm SM8750, Korean UI) for the verified versions below. A new app build is supported by
# editing profile data: export it with `profile show`, adjust it, then verify it on a device with
# `run --profile-file <path> --allow-unverified-version`.
PROFILE = {
    "schema_version": 1,
    "scenario": "galaxy.portrait-video",
    "package": "com.sec.android.app.camera",
    "activity": ".Camera",
    "verified_versions": ["17.0.00.43"],
    "selectors": {
        "mode_strip": "shooting_mode_list",
        "mode_button": "shooting_mode_item_button",
        "more_grid_item": "more_grid_list_text",
        "instant_mode_title": "instant_mode_title",
        "instant_mode_back": "instant_mode_back_button",
        "mode_ready": "shooting_mode_portrait_video_main",
        "record_button": "center_button_container",
    },
    "labels": {
        "mode": ["인물 동영상", "portrait video"],
        "more": ["더보기", "more"],
        "record_start": ["녹화 시작", "start recording", "record video"],
    },
    "log": {
        "tags": ["RecordingManager", "CameraService", "Camera3-Device", "MediaRecorder", "MPEG4Writer"],
        "record_state": {"tag": "RecordingManager", "pattern": r"changeRecordingState\s*:\s*(\w+)\s*->\s*(\w+)"},
    },
}


def wait_for(probe, timeout, code, message, interval=0.25):
    deadline = time.monotonic() + timeout
    while True:
        if value := probe():
            return value
        if time.monotonic() >= deadline:
            raise RunError(code, message)
        time.sleep(interval)


def find_node(root, profile, key, labels=None):
    resource_id = profile.resource_id(key)
    for node in root.iter("node"):
        if node.get("package") != profile.package or node.get("resource-id") != resource_id:
            continue
        values = {node.get(attribute, "").strip().casefold() for attribute in ("text", "content-desc")}
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


def portrait_ready(root, profile):
    return find_node(root, profile, "mode_ready") is not None


def launch_camera(device, profile):
    launch = device.shell(
        "am", "start", "-W", "-a", "android.intent.action.MAIN", "-c",
        "android.intent.category.LAUNCHER", "-n", f"{profile.package}/{profile.activity}"
    )
    if "Status: ok" not in launch:
        raise RunError("CAMERA_LAUNCH_FAILED", "Galaxy Camera could not be launched.")
    require_foreground(device, profile)


def enter_portrait_video(device, profile, steps):
    launch_camera(device, profile)
    mode_text, more_text = profile.label_set("mode"), profile.label_set("more")

    root = device.ui()
    if not portrait_ready(root, profile):
        if (back := find_node(root, profile, "instant_mode_back")) is not None:
            device.tap(center(back))
            root = device.ui()
        tile = find_node(root, profile, "more_grid_item", mode_text)
        if tile is None:
            pinned = find_node(root, profile, "mode_button", mode_text)
            if pinned is not None:
                tile = pinned
            elif (more := find_node(root, profile, "mode_button", more_text)) is not None:
                device.tap(center(more))
                tile = find_node(device.ui(), profile, "more_grid_item", mode_text)
                if tile is not None:
                    steps.append("more_opened")
        if tile is None:
            raise RunError("MODE_NOT_FOUND", "Portrait Video was not found on the Camera mode strip or in More.")
        device.tap(center(tile))

    start_text = profile.label_set("record_start")

    def ready_button():
        state = device.ui()
        if portrait_ready(state, profile):
            node = find_node(state, profile, "record_button", start_text)
            if node is not None and node.get("enabled") == "true" and node.get("clickable") == "true":
                return center(node)
        return None

    return wait_for(ready_button, 8, "MODE_NOT_READY", "Portrait Video and its Record button were not ready.")


def mode_labels(root, profile, key):
    exact_id, more_text = profile.resource_id(key), profile.label_set("more")
    return {
        text for node in root.iter("node")
        if node.get("package") == profile.package and node.get("resource-id") == exact_id
        and (text := node.get("text", "").strip()) and text.casefold() not in more_text
    }


def scan_modes(device, profile):
    launch_camera(device, profile)
    mode_text, more_text = profile.label_set("mode"), profile.label_set("more")
    initial = device.ui()
    root = initial
    warnings = []
    title = find_node(initial, profile, "instant_mode_title")
    strip = find_node(initial, profile, "mode_strip")
    initial_mode = title.get("text") if title is not None else None
    if initial_mode is None and strip is not None:
        initial_mode = strip.get("content-desc", "").split(",", 1)[0].strip() or None

    if (back := find_node(initial, profile, "instant_mode_back")) is not None:
        device.tap(center(back))
        root = device.ui()
    strip_modes = mode_labels(root, profile, "mode_button")
    more_modes = mode_labels(root, profile, "more_grid_item")
    opened_more = bool(more_modes)
    if not opened_more:
        more = find_node(root, profile, "mode_button", more_text)
        if more is not None:
            device.tap(center(more))
            root = device.ui()
            strip_modes.update(mode_labels(root, profile, "mode_button"))
            more_modes = mode_labels(root, profile, "more_grid_item")
            opened_more = bool(more_modes)
    if not opened_more:
        warnings.append("MORE_MENU_NOT_OBSERVED")

    if initial_mode and initial_mode.casefold() not in more_text:
        labels = {initial_mode.casefold()}
        restore = find_node(root, profile, "more_grid_item", labels) if title is not None else None
        if restore is None:
            restore = find_node(root, profile, "mode_button", labels)
        if restore is not None:
            device.tap(center(restore))
            restored = device.ui()
            actual_title = find_node(restored, profile, "instant_mode_title", labels)
            actual_strip = find_node(restored, profile, "mode_strip")
            actual_mode = actual_strip.get("content-desc", "").split(",", 1)[0].strip().casefold() if actual_strip is not None else None
            if actual_title is None and actual_mode not in labels:
                warnings.append("INITIAL_MODE_RESTORE_UNVERIFIED")
        else:
            warnings.append("INITIAL_MODE_RESTORE_UNAVAILABLE")

    observed = strip_modes | more_modes
    if title is not None and initial_mode:
        observed.add(initial_mode)
    portrait_seen = portrait_ready(initial, profile) or any(mode.casefold() in mode_text for mode in observed)
    return {"observed_modes": sorted(observed), "mode_strip": sorted(strip_modes),
            "more_menu": sorted(more_modes), "warnings": warnings}, portrait_seen


def require_foreground(device, profile):
    if not device.package_foreground(profile.package):
        raise RunError("CAMERA_NOT_FOREGROUND", "Unlock the phone and bring the Galaxy Camera app to the foreground.")


def videos(device):
    # An empty DCIM directory is normal on a device that has never used the camera.
    listing = device.shell(
        "sh", "-c", "if [ -d /sdcard/DCIM/Camera ]; then "
        "find /sdcard/DCIM/Camera -maxdepth 1 -type f -iname '*.mp4' ! -name '.*'; fi"
    )
    return {line for line in listing.splitlines() if line.startswith("/sdcard/DCIM/Camera/")}


def record_transitions(device, profile, since, pid):
    for line in device.logs(since, (profile.record_state_tag,)).splitlines():
        entry = LOG_LINE.match(line)
        if entry and entry[1] == pid and (transition := profile.record_state_pattern.search(entry[3])):
            yield transition.groups()


def latest_record_state(device, profile, since, pid):
    transitions = list(record_transitions(device, profile, since, pid))
    return transitions[-1][1] if transitions else None


def recording_finished(device, profile, since, pid):
    return ("STOPPING", "IDLE") in record_transitions(device, profile, since, pid)


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


def record(device, profile, point, seconds, result):
    require_foreground(device, profile)
    pid = device.shell("pidof", "-s", profile.package)
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
        wait_for(lambda: latest_record_state(device, profile, start_clock, pid) == "RECORDING", 5,
                 "RECORD_START_UNVERIFIED", "Camera log did not confirm the recording start.")
        result["steps"].append("recording_started")
        time.sleep(seconds)
        require_foreground(device, profile)
        if latest_record_state(device, profile, start_clock, pid) != "RECORDING":
            raise RunError("RECORDING_ENDED_EARLY", "The camera stopped recording before the planned stop.")
        stop_clock = device.clock()
        stop_attempted = True
        device.tap(point)
        wait_for(lambda: recording_finished(device, profile, stop_clock, pid),
                 6, "RECORD_STOP_UNVERIFIED", "Camera log did not confirm that recording ended.")
        ended = True
        result["steps"].append("recording_stopped")
    finally:
        if armed and not ended:
            result["recording_may_be_active"] = True
            try:
                state = latest_record_state(device, profile, start_clock, pid)
                if state == "RECORDING" and not stop_attempted:
                    require_foreground(device, profile)
                    cleanup_clock = device.clock()
                    device.tap(point)
                    wait_for(lambda: recording_finished(device, profile, cleanup_clock, pid),
                             6, "CLEANUP_UNVERIFIED", "Recording cleanup was not confirmed.")
                    result["recording_may_be_active"] = False
                elif state in {"STOPPING", "IDLE", "PREPARING", "PREPARED"}:
                    result["recording_may_be_active"] = False
            except RunError:
                pass
    result["video"] = verify_video(device, baseline)
    result["steps"].append("video_saved")
    return start_clock


def execute(device, profile, seconds, result):
    point = enter_portrait_video(device, profile, result["steps"])
    result["steps"].append("portrait_video_ready")
    return record(device, profile, point, seconds, result)
