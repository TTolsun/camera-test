"""suite: discover the modes of each camera facing, capture evidence in each and keep a registry.

The flow is rear photo, switch to front, front photo and video, return to rear. Every capture confirms
the camera facing and the selected mode twice (after the mode is selected and right before the shutter)
and once more after the capture, so a facing that silently resets cannot yield evidence for the wrong
camera. The switch button's label names the camera it switches TO, so the camera in use is the opposite
of that label. With no usable switch label the direction stays unknown; it is never inferred.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import sys
from uuid import uuid4

from . import media, preflight, registry
from .adb import RunError, device_lock, select_device
from .preview import SuiteDevice
from .failures import failure_class
from .guidance import PYZ, next_action
from .scenarios import SCENARIOS
from .scenarios.galaxy_portrait_video import (center, find_node, launch_camera, record, require_foreground,
                                              scan_modes, wait_for)
from .verify import write_json_atomic

SCENARIO = "galaxy.portrait-video"  # Its profile supplies the package, selectors, labels and recording log.

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
FACING_NAMES = {"rear": "후면", "front": "전면"}

SHUTTER_LABELS = frozenset(("촬영", "사진 촬영", "take picture", "shutter", "capture"))
SWITCH_LABEL_FACING = {
    "전면 카메라로 전환": "rear",
    "후면 카메라로 전환": "front",
    "switch to front camera": "rear",
    "switch to rear camera": "front",
    "switch to back camera": "front",
}

STATUS_BY_CLASS = {"environment": "ENVIRONMENT_ERROR", "automation": "AUTOMATION_ERROR", "product": "FAIL"}
STATUS_TEXT = {"PASS": "통과", "OPENED": "모드 열림", "FAIL": "실패(앱 동작)", "AUTOMATION_ERROR": "자동화 오류", "ENVIRONMENT_ERROR": "환경 문제",
               "MANUAL": "사람이 직접 필요", "UNKNOWN_MODE": "인식하지 못한 모드", "EXTERNAL_APP": "별도 앱",
               "SKIPPED": "건너뜀", "NOT_OBSERVED": "이번 인벤토리에 없음", "ENVIRONMENT_BLOCKED": "환경 문제로 중단",
               "INCOMPLETE": "필수 흐름 미완료", "UNVERIFIED_FACING": "이 방향으로 검증할 수 없음"}
STEP_NAMES = {"start_rear": "후면으로 시작", "switch_front": "전면으로 전환", "return_rear": "후면으로 복귀",
              "restore_rear_photo": "후면 사진 모드로 정리"}


def mode_key(label):
    return LABEL_KEY.get((label or "").strip().casefold())


def classify(labels):
    """(recognized {key: label}, unrecognized [label]) for the labels observed in the mode strip and More."""
    recognized, unrecognized = {}, []
    for label in labels:
        key = mode_key(label)
        if key is None:
            unrecognized.append(label)
        else:
            recognized.setdefault(key, label)
    return recognized, unrecognized


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
    return {"node": node, "facing": facing or hal, "label": label,
            "source": "switch_label" if facing else "camera_service" if hal else None}


def current_mode(root, profile):
    title = find_node(root, profile, "instant_mode_title")
    if title is not None and title.get("text", "").strip():
        return title.get("text").strip()
    strip = find_node(root, profile, "mode_strip")
    return (strip.get("content-desc", "").split(",", 1)[0].strip() or None) if strip is not None else None


def view(root, profile, state):
    return {"facing": state["facing"], "switch_label": state["label"], "source": state["source"],
            "mode": current_mode(root, profile)}


def ui_or_none(device):
    """A UI dump, or None while the preview animates and the dump is briefly unavailable."""
    try:
        return device.ui()
    except RunError as exc:
        if exc.code != "UI_UNAVAILABLE":
            raise
        return None


def select_mode(device, profile, key, labels):
    """Open the mode from the strip or More. Nodes come from the live hierarchy; nothing is guessed."""
    device.phase = "mode_select"
    root = wait_for(lambda: ui_or_none(device), 8, "UI_UNAVAILABLE", "Camera UI was unavailable before mode selection.")
    if mode_key(current_mode(root, profile)) == key and find_node(root, profile, "more_grid_item") is None:
        return
    if (back := find_node(root, profile, "instant_mode_back")) is not None:
        device.tap(center(back))
        def menu_open():
            menu = ui_or_none(device)
            if menu is not None and find_node(menu, profile, "instant_mode_back") is None:
                if find_node(menu, profile, "mode_button") is not None or find_node(menu, profile, "more_grid_item") is not None:
                    return menu
            return None
        root = wait_for(menu_open, 8, "MODE_NOT_READY", "Camera mode menu did not open after leaving the active mode.")
    if mode_key(current_mode(root, profile)) == key and find_node(root, profile, "more_grid_item") is None:
        return
    tile = find_node(root, profile, "more_grid_item", labels)
    if tile is None:
        tile = find_node(root, profile, "mode_button", labels)
    if tile is None and (more := find_node(root, profile, "mode_button", profile.label_set("more"))) is not None:
        device.tap(center(more))
        menu = wait_for(lambda: snapshot if (snapshot := ui_or_none(device)) is not None
                        and find_node(snapshot, profile, "more_grid_item") is not None else None,
                        8, "MODE_NOT_READY", "Camera More menu did not appear.")
        tile = find_node(menu, profile, "more_grid_item", labels)
    if tile is None:
        raise RunError("MODE_NOT_FOUND", f"{MODE_NAMES[key]} was not found on the Camera mode strip or in More.")
    device.tap(center(tile))


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


def check_media(clip, seconds, key, result):
    """Track and frame checks of media.verdict. Slow motion and hyperlapse skip the length check."""
    try:
        info = media.inspect(clip)
    except media.MediaError as exc:
        result["media"] = {"error": exc.code}
        raise RunError(exc.code, str(exc)) from exc
    failures, warnings = media.verdict(info, seconds)
    if key in TIME_SHIFTED:
        failures = [item for item in failures if item[0] != "MEDIA_DURATION"]
    result["media"] = {**info, "checks": media.check_summary(failures, warnings)}
    if key in TIME_SHIFTED:
        result["media"]["checks"]["duration"] = "SKIPPED"
    result.setdefault("warnings", []).extend(code for code, _ in warnings)
    if failures:
        raise RunError(*failures[0])
    return info


class Suite:
    def __init__(self, device, profile, out, seconds, tools):
        self.device, self.profile, self.out, self.seconds, self.tools = device, profile, out, seconds, tools
        self.steps, self.inventories, self.attempted = [], {}, set()
        self.verified = set()  # Facings whose direction was read and matched at least once.
        self.abort = None
        self.saved_inventories = {}

    # -- recording of steps -------------------------------------------------------------------------------------
    def note(self, step_id, facing, mode, status, mandatory=False, **meta):
        record = {"step": step_id, "facing": facing, "mode": mode, "mandatory": mandatory, "status": status, **meta}
        self.steps.append(record)
        return record

    def step(self, step_id, facing, mode, work, mandatory=False, blocked=None, **meta):
        """Run `work(result, folder)` and record its outcome. Returns True on PASS."""
        reason = self.abort or blocked
        if reason:
            self.note(step_id, facing, mode, "SKIPPED", mandatory, reason=reason, **meta)
            return False
        folder = self.out / step_id
        folder.mkdir(parents=True, exist_ok=True)
        result = {"steps": [], "artifacts": {}}
        record = self.note(step_id, facing, mode, "PASS", mandatory, **meta)
        print(f"[카메라 테스트] {step_title(record)} 시작", file=sys.stderr, flush=True)
        try:
            record.update(work(result, folder) or {})
            record["artifacts"] = result["artifacts"] or None
            return True
        except RunError as exc:
            if exc.code == "FACING_MISMATCH" and mode and not mandatory and result.get("mode_switched_facing"):
                record.update(status="UNVERIFIED_FACING", facing_checks=result["facing_checks"])
            else:
                code = "SUITE_UI_UNAVAILABLE" if exc.code == "UI_UNAVAILABLE" else exc.code
                self.fail(record, code, str(exc), result, folder)
                if code == "SUITE_UI_UNAVAILABLE" and result.get("can_exit_mode") and not self.abort:
                    self.recover_preview(record)
        except KeyboardInterrupt:
            self.fail(record, "INTERRUPTED", "Camera test was interrupted.", result, folder)
        finally:
            print(f"[카메라 테스트] {step_title(record)} · {STATUS_TEXT[record['status']]}", file=sys.stderr, flush=True)
        return False

    def recover_preview(self, record):
        """Leave a preview this run opened, without touching another app or an uncertain recording."""
        try:
            if not self.device.package_foreground(self.profile.package):
                return
            preflight.check(self.device, self.profile.package)
            self.device.shell("input", "keyevent", "KEYCODE_BACK")
            foreground = preflight.foreground_state(self.device, self.profile.package)
            if foreground["package"] and foreground["package"] == foreground["launcher"]:
                launch_camera(self.device, self.profile)
            record["recovery"] = "BACK_SENT"
        except RunError as exc:
            record["recovery"] = exc.code
            if failure_class(exc.code) == "environment":
                self.abort = "ENVIRONMENT"

    def fail(self, record, code, message, result, folder):
        cls = failure_class(code)
        record.update(status=STATUS_BY_CLASS.get(cls, "AUTOMATION_ERROR"), error_code=code, failure_class=cls,
                      message=message[:240])
        record.pop("evidence", None)
        if result.get("facing_checks"):
            record["facing_checks"] = result["facing_checks"]
        if result.get("evidence_discarded"):
            record["evidence_discarded"] = True
        if cls == "environment":
            self.abort = "ENVIRONMENT"
        if result.get("recording_may_be_active"):
            record["recording_may_be_active"] = True
            self.abort = "RECORDING_MAY_BE_ACTIVE"  # No more taps while a recording may still run.
        if self.device.last_ui is not None:
            try:
                (folder / "failure-ui.xml").write_text(self.device.last_ui, encoding="utf-8")
                result["artifacts"]["failure_ui"] = str(folder / "failure-ui.xml")
            except OSError:
                pass
        if cls != "environment":
            self.tools.capture_failure(self.device, self.profile.package, folder, result)
        record["artifacts"] = result["artifacts"] or None
        record["next_action"] = next_action(code, {"artifacts": result["artifacts"],
                                                   "recording_may_be_active": record.get("recording_may_be_active")})
        if code in {"MODE_NOT_FOUND", "MODE_NOT_SELECTED", "MODE_NOT_READY", "INVALID_UI_BOUNDS"}:
            record["next_action"] = (
                f"{step_title(record)} 단계에서 화면을 확인하지 못했습니다. 해당 단계 폴더의 화면 자료와 "
                f"suite.json을 확인하고, 모드 배치가 바뀌었으면 `{PYZ} suite --rescan`으로 다시 검사합니다.")

    # -- facing -------------------------------------------------------------------------------------------------
    def confirm(self, root, facing, stage, checks, state=None):
        """Compare the camera in use with `facing`. Anything but a definite match raises."""
        state = state or read_state(root, self.profile, self.device)
        checks[stage] = view(root, self.profile, state)
        if state["node"] is None and state["facing"] is None:
            raise RunError("SWITCH_NOT_FOUND", "No camera switch button was found, so the facing cannot be read.")
        if state["facing"] is None:
            raise RunError("FACING_UNVERIFIED", f"The switch label {state['label']!r} does not name one camera.")
        if state["facing"] != facing:
            raise RunError("FACING_MISMATCH", f"Expected the {facing} camera, but the switch label "
                                              f"{state['label']!r} shows the {state['facing']} camera ({stage}).")
        self.verified.add(facing)
        return state

    def switch_to(self, target, result, require_change=False):
        self.device.phase = "facing_switch"
        checks = result["facing_checks"] = {}
        select_mode(self.device, self.profile, "photo", CATALOG["photo"]["labels"])
        root = wait_for(self.photo_screen, 8, "MODE_NOT_SELECTED", "The Camera did not return to the Photo mode.")
        self.device.phase = "facing_switch"
        state = read_state(root, self.profile)
        checks["before"] = view(root, self.profile, state)
        if state["node"] is None:
            raise RunError("SWITCH_NOT_FOUND", "No camera switch button was found, so the facing cannot be read.")
        if state["facing"] is None:
            raise RunError("FACING_UNVERIFIED", f"The switch label {state['label']!r} does not name one camera.")
        if state["facing"] != target:
            self.device.tap(center(state["node"]))

            def switched():
                now = ui_or_none(self.device)
                if now is None:
                    return None
                after = read_state(now, self.profile)
                checks["after"] = view(now, self.profile, after)
                return after if after["facing"] == target and mode_key(current_mode(now, self.profile)) == "photo" else None

            wait_for(switched, 8, "FACING_SWITCH_UNVERIFIED",
                     f"The switch label never showed the {target} camera after the switch was tapped.")
        else:
            checks["after"] = checks["before"]
            if require_change:
                raise RunError("FACING_MISMATCH", f"The Camera was already using the {target} camera before the switch.")
        self.verified.add(target)
        return {"facing_checks": checks}

    def photo_screen(self):
        root = ui_or_none(self.device)
        return root if root is not None and mode_key(current_mode(root, self.profile)) == "photo" else None

    # -- inventory and capture ----------------------------------------------------------------------------------
    def inventory(self, facing, blocked=None):
        def work(result, folder):
            checks = result["facing_checks"] = {}
            self.confirm(self.device.ui(), facing, "before_inventory", checks)
            saved = self.saved_inventories.get(facing)
            if saved:
                observed = saved["observed"]
                metadata = {"source": "registry", "registered_run_id": saved["run_id"]}
            else:
                info, _ = scan_modes(self.device, self.profile, launch=False, allow_partial=True)
                observed = info["observed_modes"]
                metadata = {"source": "scan", "mode_strip": info["mode_strip"], "more_menu": info["more_menu"],
                            "warnings": info["warnings"]}
            recognized, unrecognized = classify(observed)
            self.inventories[facing] = {"observed": observed, "recognized": recognized,
                                        "unrecognized": unrecognized, **metadata}
            if saved is None:
                try:
                    result["can_exit_mode"] = True
                    select_mode(self.device, self.profile, "photo", CATALOG["photo"]["labels"])
                    self.confirm(wait_for(self.photo_screen, 8, "MODE_NOT_SELECTED",
                                          "The Camera did not return to Photo after scanning."), facing, "after_scan", checks)
                except RunError:
                    metadata["warnings"].append("SCAN_STATE_UNVERIFIED")
                    raise
            return {"observed_modes": observed, "facing_checks": checks, **metadata}

        if self.step(f"inventory_{facing}", facing, None, work, blocked=blocked):
            for key, label in self.inventories[facing]["recognized"].items():
                kind = CATALOG[key]["kind"]
                if kind in ("manual", "external"):
                    self.note(f"{facing}_{key}", facing, key, "MANUAL" if kind == "manual" else "EXTERNAL_APP",
                              label=label, kind=kind)
            for label in self.inventories[facing]["unrecognized"]:
                self.note(f"{facing}_unknown_{len(self.steps)}", facing, None, "UNKNOWN_MODE", label=label)

    def capture_step(self, facing, key, mandatory=False, blocked=None):
        self.attempted.add((facing, key))
        inventory = self.inventories.get(facing)
        meta = {"label": (inventory or {}).get("recognized", {}).get(key), "kind": CATALOG[key]["kind"]}
        if not mandatory and inventory is not None and key not in inventory["recognized"] and not (self.abort or blocked):
            self.note(f"{facing}_{key}", facing, key, "NOT_OBSERVED", mandatory, **meta)
            return
        return self.step(f"{facing}_{key}", facing, key,
                         lambda result, folder: self.capture(facing, key, result, folder, mandatory),
                         mandatory, blocked, **meta)

    def extras(self, facing):
        inventory = self.inventories.get(facing)
        for key in CATALOG:
            if inventory and key in inventory["recognized"] and CATALOG[key]["kind"] in ("photo", "video") \
                    and (facing, key) not in self.attempted:
                self.capture_step(facing, key)

    def capture(self, facing, key, result, folder, mandatory, *, preserve_mode=False, open_only=False):
        device, profile = self.device, self.profile
        kind, labels = CATALOG[key]["kind"], CATALOG[key]["labels"]
        shutter = profile.label_set("record_start") if kind == "video" else SHUTTER_LABELS
        checks = result["facing_checks"] = {}
        require_foreground(device, profile)
        if preserve_mode:
            root = wait_for(lambda: ui_or_none(device), 8, "UI_UNAVAILABLE", "Camera UI was unavailable before capture.")
            if mode_key(current_mode(root, profile)) != key and read_state(root, profile, device)["facing"] != facing:
                self.switch_to(facing, {}, require_change=False)
        elif not mandatory:
            self.switch_to(facing, {}, require_change=False)
        select_mode(device, profile, key, labels)
        result["can_exit_mode"] = True
        seen = {}

        def ready():
            root = ui_or_none(device)
            if root is None:
                return None
            state = read_state(root, profile, device)
            last_facing, last_mode = seen.get("facing"), seen.get("mode")
            seen.update(facing=state["facing"], mode=mode_key(current_mode(root, profile)))
            if seen["mode"] == key and state["facing"] not in (None, facing):
                checks["after_mode_select"] = view(root, profile, state)
                if last_mode == key and last_facing == state["facing"]:
                    result["mode_switched_facing"] = True
                    node = state["node"]
                    if (not mandatory and "before_mode_switch" not in checks and node is not None
                            and node.get("enabled") == "true" and node.get("clickable") == "true"):
                        checks["before_mode_switch"] = checks["after_mode_select"]
                        device.tap(center(node))
                        seen.clear()
                        return None
                    self.confirm(root, facing, "after_mode_select", checks, state)
                return None
            if state["facing"] != facing or seen["mode"] != key:
                return None
            button = find_node(root, profile, "record_button", shutter)
            if button is None or button.get("enabled") != "true" or button.get("clickable") != "true":
                return None
            return view(root, profile, state)

        device.phase = "mode_ready"
        try:
            checks["after_mode_select"] = wait_for(ready, 8, "MODE_NOT_READY", "The mode and its shutter were not ready.")
        except RunError as exc:
            if exc.code != "MODE_NOT_READY":
                raise
            if not seen:
                raise RunError("UI_UNAVAILABLE", "The Camera UI was unavailable after mode selection.") from exc
            if not mandatory and "before_mode_switch" in checks and seen["mode"] != key:
                raise RunError("FACING_MISMATCH", "The camera left the requested mode after the in-mode switch.") from exc
            if seen["facing"] is None:
                raise RunError("FACING_UNVERIFIED", "The camera facing could not be read after the mode was selected.") from exc
            if seen["mode"] != key:
                raise RunError("MODE_NOT_SELECTED", f"{MODE_NAMES[key]} was tapped but another mode stayed selected.") from exc
            raise
        result.pop("mode_switched_facing", None)
        if open_only:
            return {"facing_checks": checks}

        before = images(device) if kind == "photo" else None
        # Right before the shutter: facing and mode again, and the shutter point from this very dump.
        root = device.ui()
        self.confirm(root, facing, "before_shutter", checks)
        if mode_key(current_mode(root, profile)) != key:
            raise RunError("MODE_NOT_SELECTED", f"{MODE_NAMES[key]} was no longer selected right before the shutter.")
        button = find_node(root, profile, "record_button", shutter)
        if button is None or button.get("enabled") != "true" or button.get("clickable") != "true":
            raise RunError("MODE_NOT_READY", "The shutter was not available right before the shot.")
        point = center(button)

        if kind == "photo":
            device.phase = "capture"
            device.tap(point)
            evidence = {"kind": "image", "facing": facing, **verify_image(device, before)}
        else:
            record(device, profile, point, self.seconds, result)
            evidence = {"kind": "video", "facing": facing, **result["video"]}

        # The UI cannot be dumped while recording, so the facing is read again once the shot is finished.
        after = wait_for(lambda: [root] if (root := ui_or_none(device)) is not None else None, 6, "UI_UNAVAILABLE",
                         "The Camera UI was unavailable after the capture.")[0]
        try:
            self.confirm(after, facing, "after_capture", checks)
            if mode_key(current_mode(after, profile)) != key:
                raise RunError("MODE_NOT_SELECTED", "The selected mode changed before the capture was verified.")
        except RunError:
            result["evidence_discarded"] = True
            raise
        if kind == "video":
            clip = folder / f"{facing}-{key}.mp4"
            self.tools.pull_video(device, clip, result)
            info = check_media(clip, self.seconds, key, result)
            result["artifacts"]["video"] = str(clip)
            evidence.update(duration_s=info["duration_s"], local_path=str(clip), tracks=info["tracks"],
                            checks=result["media"]["checks"])
        return {"evidence": evidence, "facing_checks": checks, "metrics": result.get("metrics"),
                "warnings": result.get("warnings")}

    # -- the flow -----------------------------------------------------------------------------------------------
    def run(self):
        def start(result, folder):
            launch_camera(self.device, self.profile)
            return self.switch_to("rear", result)

        rear = self.step("start_rear", "rear", None, start, mandatory=True)
        gate = None if rear else "REAR_NOT_VERIFIED"
        self.inventory("rear", gate)
        self.capture_step("rear", "photo", True, gate)
        front = self.step("switch_front", "front", None,
                          lambda result, folder: self.switch_to("front", result, require_change=True),
                          mandatory=True, blocked=gate)
        gate_front = None if front else "FRONT_NOT_REACHED"
        self.inventory("front", gate_front)
        self.capture_step("front", "photo", True, gate_front)
        self.capture_step("front", "video", True, gate_front)
        back = self.step("return_rear", "rear", None,
                         lambda result, folder: self.switch_to("rear", result, require_change=True),
                         mandatory=True, blocked=gate_front)
        if back:
            self.extras("rear")
            self.extras("front")
        mandatory_captures = {("rear", "photo"), ("front", "photo"), ("front", "video")}
        if rear and not self.abort and (not back or self.attempted - mandatory_captures):
            self.step("restore_rear_photo", "rear", None, lambda result, folder: self.switch_to("rear", result))


def overall(steps):
    statuses = {step["status"] for step in steps}
    if "ENVIRONMENT_ERROR" in statuses:
        return "ENVIRONMENT_BLOCKED"
    for status in ("FAIL", "AUTOMATION_ERROR"):
        if status in statuses:
            return status
    return "INCOMPLETE" if any(step["mandatory"] and step["status"] != "PASS" for step in steps) else "PASS"


def exit_code(status):
    return {"PASS": 0, "ENVIRONMENT_BLOCKED": 3}.get(status, 1)


def first_action(result):
    """The next step of the most urgent failing step; otherwise what a person does with the finished run."""
    steps = result["steps"]
    for status in ("ENVIRONMENT_ERROR", "FAIL", "AUTOMATION_ERROR"):
        found = next((step for step in steps if step["status"] == status), None)
        if found:
            return found["next_action"]
    if result.get("registry", {}).get("error"):
        return next_action(result["registry"]["error"])
    if result["status"] == "INCOMPLETE":
        skipped = next(step for step in steps if step["mandatory"] and step["status"] != "PASS")
        return (f"필수 단계 `{skipped['step']}`가 {STATUS_TEXT[skipped['status']]} 상태입니다. "
                "단말 카메라 화면에서 해당 모드가 실제로 있는지 확인하고 `report.md`의 인벤토리를 확인합니다.")
    manual = [step for step in steps if step["status"] in ("MANUAL", "UNKNOWN_MODE", "UNVERIFIED_FACING")]
    if manual:
        return ("자동으로 확인하지 못한 모드가 있습니다(파노라마는 사람이 직접 촬영하고, 인식하지 못한 모드는 "
                "카탈로그에 추가한 뒤 검증합니다). 목록은 report.md의 모드별 결과에 있습니다.")
    if any(item.get("warnings") for item in result["inventories"].values()):
        return f"모드 목록을 일부만 읽었습니다. 단말 화면을 확인한 뒤 `{PYZ} suite --rescan`을 실행합니다."
    return f"조치가 필요 없습니다. 새 앱 빌드가 설치되면 `{PYZ} suite`를 다시 실행해 등록부와 비교합니다."


def step_title(step):
    if step["mode"]:
        return f"{FACING_NAMES[step['facing']]} {MODE_NAMES[step['mode']]}"
    if step["step"] in STEP_NAMES:
        return STEP_NAMES[step["step"]]
    if step["step"].startswith("inventory_"):
        return f"{FACING_NAMES[step['facing']]} 모드 목록 확인"
    return f"{FACING_NAMES[step['facing']]} 모드 '{step.get('label')}'"


def step_detail(step):
    status = step["status"]
    if status == "PASS":
        evidence, checks = step.get("evidence"), step.get("facing_checks") or {}
        parts = []
        if evidence and evidence["kind"] == "image":
            parts.append(f"사진 파일 {len(evidence['device_paths'])}개 {evidence['bytes']}바이트")
        elif evidence:
            parts.append(f"영상 {evidence['duration_s']}초 {evidence['bytes']}바이트")
        if step.get("observed_modes") is not None:
            name = "등록 목록" if step.get("source") == "registry" else "관찰한 모드"
            parts.append(f"{name} · " + ", ".join(step["observed_modes"]))
        shown = [stage for stage in ("after_mode_select", "before_shutter", "after_capture") if stage in checks]
        if shown:
            parts.append(f"방향 확인 {len(shown)}회 모두 {FACING_NAMES[step['facing']]}")
        elif checks.get("after"):
            parts.append(f"방향 확인: {FACING_NAMES.get(checks['after']['facing'], '알 수 없음')}")
        return ", ".join(parts)
    if status in ("MANUAL", "EXTERNAL_APP", "UNKNOWN_MODE"):
        return {"MANUAL": "사람이 직접 촬영해야 하는 모드라 누르지 않았습니다.",
                "EXTERNAL_APP": "별도 앱이라 설치하거나 열지 않았습니다.",
                "UNKNOWN_MODE": f"'{step.get('label')}'는 카탈로그에 없는 모드라 촬영하지 않았습니다."}[status]
    if status == "SKIPPED":
        return f"이전 단계 문제로 건너뜀({step.get('reason')})"
    if status == "NOT_OBSERVED":
        return "모드 목록에 없어 누르지 않았습니다."
    if status == "UNVERIFIED_FACING":
        actual = step["facing_checks"]["after_mode_select"]["facing"]
        opening = f"모드를 열자 {FACING_NAMES[actual]} 카메라가 켜졌습니다."
        if step["facing_checks"].get("before_mode_switch"):
            return f"{opening} 전환을 시도했으나 해당 방향의 같은 모드를 확인하지 못했습니다."
        return f"{opening} 해당 모드에서 방향을 바꿀 수 있는 버튼을 확인하지 못했습니다."
    text = f"{step['error_code']}: {step['message']}"
    if step.get("evidence_discarded"):
        text += " 이 촬영은 증거로 등록하지 않았습니다."
    return text


def render(result):
    """Korean report for a person. report.md and the default stdout are this text."""
    device, lines = result["device"], []
    lines += [f"# 카메라 스위트 결과: {STATUS_TEXT[result['status']]}", "",
              f"- 단말 {device.get('model')}, 갤럭시 카메라 {device.get('camera_version')} "
              f"(검증 목록 {'포함' if result['profile']['version_verified'] else '미포함'})",
              f"- 실행 {result['run_id']}, 등록부 {result['registry']['path']}"]
    if result.get("warnings"):
        labels = {"UNVERIFIED_CAMERA_VERSION": "내장 프로필의 검증 목록에 없는 카메라 앱 버전",
                  "MEDIA_NO_AUDIO": "일부 영상에 오디오 트랙이 없음"}
        lines.append("- 경고 · " + ", ".join(labels.get(code, code) for code in result["warnings"]))
    lines += ["", f"**다음 행동**: {result['next_action']}", "", "## 필수 흐름"]
    for number, step in enumerate((item for item in result["steps"] if item["mandatory"]), 1):
        lines.append(f"{number}. {step_title(step)}: {STATUS_TEXT[step['status']]}. {step_detail(step)}".rstrip())
    lines += ["", "## 모드별 결과"]
    registry_info = result["registry"].get("facings", {})
    for facing in ("rear", "front"):
        rows = [step for step in result["steps"] if step["facing"] == facing and not step["step"].startswith(
            ("start_", "switch_", "return_", "restore_", "inventory_"))]
        if not rows:
            continue
        lines.append(f"### {FACING_NAMES[facing]}")
        info = registry_info.get(facing, {})
        for step in rows:
            line = f"- {step_title(step)}: {STATUS_TEXT[step['status']]}. {step_detail(step)}"
            other = (info.get("verified_elsewhere") or {}).get(step["mode"])
            if step["status"] not in ("PASS", "MANUAL", "EXTERNAL_APP") and other:
                line += f" 이전 버전 {other['app_version']}에서는 검증된 모드입니다."
            lines.append(line.rstrip())
            if step.get("next_action"):
                lines.append(f"  - 다음: {step['next_action']}")
    restore = next((step for step in result["steps"] if step["step"] == "restore_rear_photo"), None)
    if restore is not None:
        lines.append(f"- {step_title(restore)} · {STATUS_TEXT[restore['status']]}. {step_detail(restore)}")
    lines += ["", "## 전·후면 모드 목록"]
    for facing, inventory in result["inventories"].items():
        source = "같은 앱 버전의 등록부" if inventory["source"] == "registry" else "이번 화면에서 확인"
        lines.append(f"- {FACING_NAMES[facing]} ({source}) · {', '.join(inventory['observed'])}")
        if inventory.get("warnings"):
            lines.append("  - 목록을 일부만 읽어 비교 기준으로 등록하지 않았습니다.")
    lines += ["", "## 등록부 비교"]
    if not registry_info:
        lines.append("- 방향을 확인한 카메라가 없어 등록부를 바꾸지 않았습니다.")
    for facing, info in registry_info.items():
        name = FACING_NAMES[facing]
        lines.append(f"- {name}: " + ("처음 등록합니다." if info["first_run"] else "기존 등록에 이번 결과를 더했습니다."))
        comparison = info.get("comparison")
        if comparison:
            base = ("같은 버전의 이전 실행" if comparison["same_version"] else f"이전 버전 {comparison['baseline_version']}")
            lines.append(f"  - {base}과 비교: 새 모드 {comparison['added'] or '없음'}, 사라진 모드 {comparison['removed'] or '없음'}")
        if info["new_modes"]:
            lines.append(f"  - 등록부에 없던 새 모드: {', '.join(MODE_NAMES[mode] for mode in info['new_modes'])}")
        if info["missing_modes"]:
            lines.append(f"  - 이전에 검증했지만 이번에 보이지 않는 모드: {', '.join(MODE_NAMES[mode] for mode in info['missing_modes'])}")
    if result["registry"].get("error"):
        lines.append(f"- 등록부를 갱신하지 못했습니다({result['registry']['error']}). 이번 증거는 실행 폴더에 있습니다.")
    lines += ["", f"- 전체 데이터: `{result['artifacts']['suite']}`", ""]
    return "\n".join(lines)


@contextmanager
def prepared_capture(args):
    """Hold the device lock while running preflight and using the live Camera UI."""
    profile = SCENARIOS[SCENARIO].profile
    serial = select_device(args.device)
    run_id = uuid4().hex[:12]
    device = SuiteDevice(serial, run_id)
    with device_lock(device.shell("getprop", "ro.serialno") or serial), device:
        info = device.metadata()
        version = info["camera_version"] = device.package_version(profile.package)
        if version is None:
            raise RunError("CAMERA_APP_NOT_FOUND", f"Galaxy Camera ({profile.package}) is not installed.")
        verified_version = profile.verifies(version)
        warnings = [] if verified_version else ["UNVERIFIED_CAMERA_VERSION"]
        measured = preflight.check(device, profile.package)
        out = args.output_dir.resolve() / run_id
        out.mkdir(parents=True)
        version_label = "" if verified_version else " · 내장 프로필 검증 목록에 없는 버전"
        print(f"[카메라 테스트] {info.get('model')} · 갤럭시 카메라 {version}{version_label}", file=sys.stderr, flush=True)
        yield device, profile, out, info, measured, warnings


def run_suite(args, tool_version, tools):
    """Run the whole suite. Returns (result, exit code). Raises RunError before the phone is touched."""
    registry_path = (args.registry or args.output_dir / "registry.json").resolve()
    saved_registry = registry.load(registry_path)
    with prepared_capture(args) as (device, profile, out, info, measured, warnings):
        run_id, version = out.name, info["camera_version"]
        verified_version = not warnings
        suite = Suite(device, profile, out, args.duration, tools)
        if not getattr(args, "rescan", False):
            for facing in FACING_NAMES:
                key = registry.entry_key(info.get("model"), version, facing)
                entry = saved_registry["entries"].get(key) or {}
                if entry.get("inventory"):
                    suite.saved_inventories[facing] = entry["inventory"]
        suite.run()
        status = overall(suite.steps)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        result = {"tool_version": tool_version, "run_id": run_id, "command": "suite", "finished": now,
                  "status": status, "device": info, "profile": profile.summary(version), "preflight": measured,
                  "requested_seconds": args.duration, "steps": suite.steps, "ui_backends": sorted(device.ui_backends),
                  "inventories": suite.inventories, "warnings": sorted({*warnings, *(
                      code for step in suite.steps for code in step.get("warnings") or [])}) or None,
                  "recording_may_be_active": any(step.get("recording_may_be_active") for step in suite.steps),
                  "registry": {"path": str(registry_path), "updated": False}, "artifacts": {}}
        run = {"run_id": run_id, "at": now, "model": info.get("model") or "unknown-model", "version": version,
               "version_verified": verified_version, "status": status,
               "facings": {facing: {"inventory": suite.inventories.get(facing),
                                    "steps": [step for step in suite.steps if step["facing"] == facing]}
                           for facing in sorted(suite.verified)}}
        if run["facings"]:
            try:
                result["registry"]["facings"] = registry.update(registry_path, run)
                result["registry"]["updated"] = True
            except (RunError, OSError) as exc:
                result["registry"]["error"] = getattr(exc, "code", "IO_ERROR")
        else:
            result["registry"]["reason"] = "FACING_NEVER_VERIFIED"
        result["next_action"] = first_action(result)
        result["artifacts"] = {"suite": str(out / "suite.json"), "report": str(out / "report.md")}
        write_json_atomic(out / "suite.json", result)
        (out / "report.md").write_text(render(result), encoding="utf-8")
    code = exit_code(status)
    if result["registry"].get("error") and code == 0:
        code = 1
    return result, code
