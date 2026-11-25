"""Galaxy Camera mode catalog, UI controls and saved-media evidence."""

import time

from ..adb import RunError
from ..android_ui import center, ui_or_none, wait_for
from ..logcat import LOG_LINE, elapsed_ms, line_stamp
from ..profiles import parse_profile


NAME = "Galaxy Camera"
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


profile = parse_profile(PROFILE, "built-in")

# Catalog of the Galaxy Camera modes seen on SM-S936N. kind: photo/video are captured, manual needs a person,
# external opens a separate app. A label outside this table is reported as unrecognized and never captured.
CATALOG = {key: {"kind": kind, "labels": frozenset(labels)} for key, kind, labels in (
    ("photo", "photo", ("사진", "photo")),
    ("portrait_photo", "photo", ("인물 사진", "인물사진", "portrait")),
    ("night", "photo", ("야간", "night")),
    ("food", "photo", ("음식", "food")),
    ("pro_photo", "photo", ("프로", "pro")),
    ("panorama", "manual", ("파노라마", "panorama")),
    ("video", "video", ("동영상", "video")),
    ("portrait_video", "video", ("인물 동영상", "portrait video")),
    ("pro_video", "video", ("프로 동영상", "pro video")),
    ("slow_motion", "video", ("슬로우 모션", "슬로 모션", "slow motion")),
    ("hyperlapse", "video", ("하이퍼랩스", "hyperlapse")),
    ("expert_raw", "external", ("expert raw", "엑스퍼트 raw")),
)}
LABEL_KEY = {label: key for key, mode in CATALOG.items() for label in mode["labels"]}
TIME_SHIFTED = {"slow_motion", "hyperlapse"}  # Their playback length is not the recording time.
MODE_NAMES = {"photo": "사진", "portrait_photo": "인물 사진", "night": "야간", "food": "음식", "pro_photo": "프로 사진",
              "panorama": "파노라마", "video": "동영상", "portrait_video": "인물 동영상", "pro_video": "프로 동영상",
              "slow_motion": "슬로우 모션", "hyperlapse": "하이퍼랩스", "expert_raw": "Expert RAW"}
SHUTTER_LABELS = frozenset(("촬영", "사진 촬영", "take picture", "shutter", "capture"))
SWITCH_LABEL_FACING = {
    "전면 카메라로 전환": "rear",
    "후면 카메라로 전환": "front",
    "switch to front camera": "rear",
    "switch to rear camera": "front",
    "switch to back camera": "front",
}


def mode_key(label):
    return LABEL_KEY.get((label or "").strip().casefold())


def switch_state(root, profile):
    """(switch node, camera in use). The label names the destination, so the camera in use is the opposite.

    (None, None): no switch on screen. (node, None): a switch whose label does not name exactly one camera.
    """
    switches = [node for node in root.iter("node") if node.get("package") == profile.package
                and node.get("resource-id") == f"{profile.package}:id/switch_camera_button"]
    if len(switches) != 1:
        return None, None
    node = switches[0]
    label = (node.get("content-desc") or node.get("text") or "").strip().casefold()
    return node, SWITCH_LABEL_FACING.get(label)


def read_state(root, profile, device=None):
    node, facing = switch_state(root, profile)
    label = (node.get("content-desc") or node.get("text") or "").strip() if node is not None else None
    hal = device.active_camera_facing(profile.package) if node is None and device is not None else None
    return {"switch": node, "facing": facing or hal, "label": label,
            "source": "switch_label" if facing else "camera_service" if hal else None}


def switch_facing(device, state, *, require_enabled=False):
    control = state["switch"]
    if control is None:
        return False
    if require_enabled and (control.get("enabled") != "true" or control.get("clickable") != "true"):
        return False
    device.tap(center(control))
    return True


def current_mode(root, profile):
    title = find_node(root, profile, "instant_mode_title")
    if title is not None and title.get("text", "").strip():
        return title.get("text").strip()
    strip = find_node(root, profile, "mode_strip")
    return (strip.get("content-desc", "").split(",", 1)[0].strip() or None) if strip is not None else None


def find_node(root, profile, key, labels=None):
    resource_id = profile.resource_id(key)
    for node in root.iter("node"):
        if node.get("package") != profile.package or node.get("resource-id") != resource_id:
            continue
        values = {node.get(attribute, "").strip().casefold() for attribute in ("text", "content-desc")}
        if labels is None or labels & values:
            return node
    return None


def profile_mode_ready(root, profile):
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


def select_mode(device, profile, key, *, scenario_result=None):
    """Open the mode from the strip or More. Nodes come from the live hierarchy; nothing is guessed."""
    traced = scenario_result is not None
    labels = profile.label_set("mode") if traced else CATALOG[key]["labels"]

    def on_mode_strip(root):
        return mode_key(current_mode(root, profile)) == key and find_node(root, profile, "more_grid_item") is None

    if traced:
        root = device.ui()
        already_ready = profile_mode_ready(root, profile)
        device.checkpoint("initial_in_mode" if already_ready else "initial")
        if already_ready:
            return
    else:
        root = wait_for(lambda: ui_or_none(device), 8, "UI_UNAVAILABLE",
                        "Camera UI was unavailable before mode selection.")
        if on_mode_strip(root):
            return
    device.phase = "mode_select"
    if (back := find_node(root, profile, "instant_mode_back")) is not None:
        device.tap(center(back))

        def menu_open():
            menu = ui_or_none(device)
            if menu is not None and find_node(menu, profile, "instant_mode_back") is None:
                if find_node(menu, profile, "mode_button") is not None or find_node(menu, profile, "more_grid_item") is not None:
                    return menu
            return None

        if traced:
            root = device.ui()
        else:
            root = wait_for(menu_open, 8, "MODE_NOT_READY", "Camera mode menu did not open after leaving the active mode.")
            if on_mode_strip(root):
                return
    tile = find_node(root, profile, "more_grid_item", labels)
    if tile is None:
        tile = find_node(root, profile, "mode_button", labels)
    if tile is None and (more := find_node(root, profile, "mode_button", profile.label_set("more"))) is not None:
        device.tap(center(more))
        if traced:
            menu = device.ui()
        else:
            def more_open():
                menu = ui_or_none(device)
                return menu if menu is not None and find_node(menu, profile, "more_grid_item") is not None else None

            menu = wait_for(more_open, 8, "MODE_NOT_READY", "Camera More menu did not appear.")
        tile = find_node(menu, profile, "more_grid_item", labels)
        if traced:
            device.checkpoint("more_opened")
            if tile is not None:
                scenario_result["steps"].append("more_opened")
    if tile is None:
        name = key.replace("_", " ").title() if traced else MODE_NAMES[key]
        raise RunError("MODE_NOT_FOUND", f"{name} was not found on the Camera mode strip or in More.")
    device.tap(center(tile))


def shutter_control(root, profile, key):
    labels = profile.label_set("record_start") if CATALOG[key]["kind"] == "video" else SHUTTER_LABELS
    node = find_node(root, profile, "record_button", labels)
    if node is not None and node.get("enabled") == "true" and node.get("clickable") == "true":
        return node
    return None


def shutter_point(root, profile, key):
    control = shutter_control(root, profile, key)
    return center(control) if control is not None else None


def scenario_shutter(device, profile, key, result):
    name = key.replace("_", " ").title()
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
        if profile_mode_ready(state, profile):
            point = shutter_point(state, profile, key)
            if point is not None:
                device.checkpoint("mode_ready")
                return point
        return None

    device.phase = "mode_ready"

    try:
        return wait_for(ready_button, 8, "MODE_NOT_READY", f"{name} and its Record button were not ready.")
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
    target_seen = profile_mode_ready(initial, profile) or any(mode.casefold() in mode_text for mode in observed)
    return {"observed_modes": sorted(observed), "mode_strip": sorted(strip_modes),
            "more_menu": sorted(more_modes), "warnings": warnings}, target_seen


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


def images(device):
    listing = device.shell(
        "sh", "-c", "if [ -d /sdcard/DCIM/Camera ]; then find /sdcard/DCIM/Camera -maxdepth 1 -type f "
        "\\( -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.heic' -o -iname '*.dng' \\) ! -name '.*'; fi")
    return {line for line in listing.splitlines() if line.startswith("/sdcard/DCIM/Camera/")}


def verify_image(device, before):
    """The new files of one shot (a JPEG and its RAW share a name) once every size is nonempty and unchanged."""
    sizes = {}

    def saved():
        new = images(device) - before
        if not new:
            return None
        if len({path.rsplit(".", 1)[0] for path in new}) > 1:
            raise RunError("AMBIGUOUS_IMAGE", "Images of more than one shot appeared in the Camera folder.")
        stable, total = True, 0
        for path in sorted(new):
            length = device.shell("stat", "-c", "%s", path)
            size = int(length) if length.isdecimal() else 0
            stable = stable and size > 0 and sizes.get(path) == size
            sizes[path] = size
            total += size
        return {"device_paths": sorted(new), "bytes": total} if stable else None

    return wait_for(saved, 15, "IMAGE_NOT_SAVED", "A new nonempty Camera image was not found.")
