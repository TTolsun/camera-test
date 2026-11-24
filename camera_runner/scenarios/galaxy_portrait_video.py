"""Samsung Galaxy Camera Portrait Video scenario."""

import re
import time

from ..adb import RunError
from ..logcat import LOG_LINE, elapsed_ms, line_stamp


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
    # Short-lived "ready" hint shown right after entering Portrait Video; whether a UI dump catches it is
    # timing. The level-meter overlay swaps its nodes with how the phone is tilted. Both observed on
    # SM-S936N / 17.0.00.43 between runs of an unchanged build.
    "volatile_ids": [
        "guide_text",
        "level_meter_guide", "level_meter_target", "level_meter_target_bg",
        "guide_bar", "inclinometer", "level_bg", "vertical_guide_bg",
        # Scene suggestion (e.g. night shot) on the Photo screen; appears with room lighting.
        "scene_button_bg",
    ],
    "log": {
        "tags": ["RecordingManager", "CameraService", "Camera3-Device", "MediaRecorder", "MPEG4Writer"],
        "record_state": {"tag": "RecordingManager", "pattern": r"changeRecordingState\s*:\s*(\w+)\s*->\s*(\w+)"},
    },
}


# Selector keys each phase relies on. `execute` moves `device.phase` through these names.
PHASES = {
    "launch": (),
    "mode_select": ("mode_button", "more_grid_item"),
    "mode_ready": ("mode_ready", "record_button"),
    "recording": (),  # The UI is never dumped while recording.
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
    device.phase = "launch"
    launch = device.shell(
        "am", "start", "-W", "-a", "android.intent.action.MAIN", "-c",
        "android.intent.category.LAUNCHER", "-n", f"{profile.package}/{profile.activity}"
    )
    if "Status: ok" not in launch:
        raise RunError("CAMERA_LAUNCH_FAILED", "Galaxy Camera could not be launched.")
    require_foreground(device, profile)


def enter_portrait_video(device, profile, result):
    steps = result["steps"]
    launch_camera(device, profile)
    mode_text, more_text = profile.label_set("mode"), profile.label_set("more")

    root = device.ui()
    # Galaxy Camera remembers its last mode, so the start state is part of the checkpoint name.
    device.checkpoint("initial_in_mode" if portrait_ready(root, profile) else "initial")
    if not portrait_ready(root, profile):
        device.phase = "mode_select"
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
                opened = device.ui()
                device.checkpoint("more_opened")
                tile = find_node(opened, profile, "more_grid_item", mode_text)
                if tile is not None:
                    steps.append("more_opened")
        if tile is None:
            raise RunError("MODE_NOT_FOUND", "Portrait Video was not found on the Camera mode strip or in More.")
        device.tap(center(tile))

    start_text = profile.label_set("record_start")

    dumps = {"failed": False, "count": 0}

    def ready_button():
        # A dump can fail transiently while the preview animates after a mode switch.
        try:
            state = device.ui()
        except RunError as exc:
            if exc.code != "UI_UNAVAILABLE":
                raise
            dumps["failed"] = True
            dumps["count"] += 1
            return None
        dumps["failed"] = False
        if portrait_ready(state, profile):
            node = find_node(state, profile, "record_button", start_text)
            if node is not None and node.get("enabled") == "true" and node.get("clickable") == "true":
                device.checkpoint("mode_ready")
                return center(node)
        return None

    device.phase = "mode_ready"

    try:
        return wait_for(ready_button, 8, "MODE_NOT_READY", "Portrait Video and its Record button were not ready.")
    except RunError as exc:
        if exc.code == "MODE_NOT_READY" and dumps["failed"]:
            raise RunError("UI_UNAVAILABLE", "Camera UI hierarchy stayed unavailable while waiting for the Record button.") from exc
        raise
    finally:
        if dumps["count"]:
            result["ui_retries"] = dumps["count"]


def mode_labels(root, profile, key):
    exact_id, more_text = profile.resource_id(key), profile.label_set("more")
    return {
        text for node in root.iter("node")
        if node.get("package") == profile.package and node.get("resource-id") == exact_id
        and (text := node.get("text", "").strip()) and text.casefold() not in more_text
    }


def scan_modes(device, profile, launch=True, allow_partial=False):
    """`launch=False` scans the camera that is already in the foreground, so a facing the caller set stays."""
    if launch:
        launch_camera(device, profile)
    else:
        require_foreground(device, profile)
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
            try:
                root = device.ui()
            except RunError as exc:
                if not allow_partial or exc.code != "UI_UNAVAILABLE":
                    raise
                root = None
            if root is not None:
                strip_modes.update(mode_labels(root, profile, "mode_button"))
                more_modes = mode_labels(root, profile, "more_grid_item")
                opened_more = bool(more_modes)
    if not opened_more:
        warnings.append("MORE_MENU_NOT_OBSERVED")

    if root is not None and initial_mode and initial_mode.casefold() not in more_text:
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


def record_events(device, profile, since, pid):
    """Yield (log timestamp, (from_state, to_state)) for this process's recording-state changes."""
    for line in device.logs(since, (profile.record_state_tag,)).splitlines():
        entry = LOG_LINE.match(line)
        if entry and entry[1] == pid and (transition := profile.record_state_pattern.search(entry[3])):
            yield line_stamp(line), transition.groups()


def record_transitions(device, profile, since, pid):
    for _, transition in record_events(device, profile, since, pid):
        yield transition


def latest_record_state(device, profile, since, pid):
    transitions = list(record_transitions(device, profile, since, pid))
    return transitions[-1][1] if transitions else None


def recording_finished(device, profile, since, pid):
    return ("STOPPING", "IDLE") in record_transitions(device, profile, since, pid)


def recording_started_event(device, profile, since, pid):
    """{"stamp": ...} when the latest state change enters RECORDING, else None. Reuses the poll's log read."""
    events = list(record_events(device, profile, since, pid))
    return {"stamp": events[-1][0]} if events and events[-1][1][1] == "RECORDING" else None


def recording_finished_event(device, profile, since, pid):
    """{"stamp": ...} of the first STOPPING -> IDLE change, else None."""
    stamp = next((stamp for stamp, transition in record_events(device, profile, since, pid)
                  if transition == ("STOPPING", "IDLE")), False)
    return None if stamp is False else {"stamp": stamp}


def add_metric(result, name, value):
    """Store an integer-millisecond metric; a value that could not be computed is left out."""
    if value is not None:
        result.setdefault("metrics", {})[name] = value


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
    device.phase = "recording"
    require_foreground(device, profile)
    pid = device.shell("pidof", "-s", profile.package)
    if not pid.isdecimal():
        raise RunError("CAMERA_PID_NOT_FOUND", "Could not identify the Galaxy Camera process.")
    baseline = videos(device)
    start_clock = device.clock()
    stop_attempted = False
    stop_began = None
    ended = False
    armed = False
    try:
        armed = True
        device.tap(point)
        started = wait_for(lambda: recording_started_event(device, profile, start_clock, pid), 5,
                           "RECORD_START_UNVERIFIED", "Camera log did not confirm the recording start.")
        # start_clock is read on the device just before the tap, so this includes one ADB round trip.
        add_metric(result, "record_start_latency_ms", elapsed_ms(start_clock, started["stamp"]))
        result["steps"].append("recording_started")
        time.sleep(seconds)
        require_foreground(device, profile)
        if latest_record_state(device, profile, start_clock, pid) != "RECORDING":
            raise RunError("RECORDING_ENDED_EARLY", "The camera stopped recording before the planned stop.")
        stop_clock = device.clock()
        stop_attempted = True
        stop_began = time.monotonic()
        device.tap(point)
        finished = wait_for(lambda: recording_finished_event(device, profile, stop_clock, pid),
                            6, "RECORD_STOP_UNVERIFIED", "Camera log did not confirm that recording ended.")
        add_metric(result, "record_stop_latency_ms", elapsed_ms(stop_clock, finished["stamp"]))
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
    # Host monotonic time from the stop tap, so it includes ADB overhead and the file-size stability polls.
    add_metric(result, "video_ready_ms", round((time.monotonic() - stop_began) * 1000))
    result["steps"].append("video_saved")
    return start_clock


def execute(device, profile, seconds, result):
    point = enter_portrait_video(device, profile, result)
    result["steps"].append("portrait_video_ready")
    return record(device, profile, point, seconds, result)
