"""Select a camera mode, verify the requested facing and capture through the selected app."""

from .adb import RunError
from .android_ui import ui_or_none, wait_for


class CaptureSession:
    def __init__(self, device, profile, app):
        self.device = device
        self.profile = profile
        self.app = app
        self.verified = set()

    def view(self, root, state):
        return {"facing": state["facing"], "switch_label": state["label"], "source": state["source"],
                "mode": self.app.current_mode(root, self.profile)}

    def confirm(self, root, facing, stage, checks, state=None):
        """Compare the camera in use with `facing`. Anything but a definite match raises."""
        state = state or self.app.read_state(root, self.profile, self.device)
        checks[stage] = self.view(root, state)
        if state["switch"] is None and state["facing"] is None:
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
        self.app.select_mode(self.device, self.profile, "photo")
        root = wait_for(self.photo_screen, 8, "MODE_NOT_SELECTED", "The Camera did not return to the Photo mode.")
        self.device.phase = "facing_switch"
        state = self.app.read_state(root, self.profile)
        checks["before"] = self.view(root, state)
        if state["switch"] is None:
            raise RunError("SWITCH_NOT_FOUND", "No camera switch button was found, so the facing cannot be read.")
        if state["facing"] is None:
            raise RunError("FACING_UNVERIFIED", f"The switch label {state['label']!r} does not name one camera.")
        if state["facing"] != target:
            self.app.switch_facing(self.device, state)

            def switched():
                now = ui_or_none(self.device)
                if now is None:
                    return None
                after = self.app.read_state(now, self.profile)
                checks["after"] = self.view(now, after)
                return after if after["facing"] == target and self.selected_mode(now) == "photo" else None

            wait_for(switched, 8, "FACING_SWITCH_UNVERIFIED",
                     f"The switch label never showed the {target} camera after the switch was tapped.")
        else:
            checks["after"] = checks["before"]
            if require_change:
                raise RunError("FACING_MISMATCH", f"The Camera was already using the {target} camera before the switch.")
        self.verified.add(target)
        return {"facing_checks": checks}

    def selected_mode(self, root):
        return self.app.mode_key(self.app.current_mode(root, self.profile))

    def photo_screen(self):
        root = ui_or_none(self.device)
        return root if root is not None and self.selected_mode(root) == "photo" else None

    def capture(self, key, seconds, result, *, facing=None, mandatory=False, preserve_mode=False, open_only=False):
        """Return the recording start clock for a video. Store the verified media in result."""
        device, profile, app = self.device, self.profile, self.app
        scenario = facing is None
        if scenario:
            app.launch_camera(device, profile)
        else:
            app.require_foreground(device, profile)
            self.prepare_facing(facing, key, mandatory, preserve_mode)
        app.select_mode(device, profile, key, scenario_result=result if scenario else None)

        if scenario:
            before = app.images(device) if app.CATALOG[key]["kind"] == "photo" else None
            point = app.scenario_shutter(device, profile, key, result)
            result["steps"].append(f"{key}_ready")
        else:
            point, before = self.checked_shutter(facing, key, result, mandatory, open_only)
            if open_only:
                return None

        if app.CATALOG[key]["kind"] == "video":
            clock = app.record(device, profile, point, seconds, result)
        else:
            device.phase = "capture"
            device.tap(point)
            result["image"] = app.verify_image(device, before)
            clock = None
        if not scenario:
            self.check_after_capture(facing, key, result)
        return clock

    def prepare_facing(self, facing, key, mandatory, preserve_mode):
        device, profile = self.device, self.profile
        if preserve_mode:
            if self.app.CATALOG.get("photo", {}).get("kind") != "photo":
                # checked_shutter can switch and verify the facing inside the requested mode.
                return
            root = wait_for(lambda: ui_or_none(device), 8, "UI_UNAVAILABLE", "Camera UI was unavailable before capture.")
            if self.selected_mode(root) != key and self.app.read_state(root, profile, device)["facing"] != facing:
                self.switch_to(facing, {}, require_change=False)
        elif not mandatory:
            self.switch_to(facing, {}, require_change=False)

    def checked_shutter(self, facing, key, result, mandatory, open_only):
        device, profile = self.device, self.profile
        checks = result["facing_checks"] = {}
        result["can_exit_mode"] = True
        seen = {}

        def ready():
            root = ui_or_none(device)
            if root is None:
                return None
            state = self.app.read_state(root, profile, device)
            last_facing, last_mode = seen.get("facing"), seen.get("mode")
            seen.update(facing=state["facing"], mode=self.selected_mode(root))
            if seen["mode"] == key and state["facing"] not in (None, facing):
                checks["after_mode_select"] = self.view(root, state)
                if last_mode == key and last_facing == state["facing"]:
                    result["mode_switched_facing"] = True
                    if (not mandatory and "before_mode_switch" not in checks
                            and self.app.switch_facing(device, state, require_enabled=True)):
                        checks["before_mode_switch"] = checks["after_mode_select"]
                        seen.clear()
                        return None
                    self.confirm(root, facing, "after_mode_select", checks, state)
                return None
            if state["facing"] != facing or seen["mode"] != key:
                return None
            if self.app.shutter_control(root, profile, key) is None:
                return None
            return self.view(root, state)

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
                raise RunError("MODE_NOT_SELECTED", f"{self.app.MODE_NAMES[key]} was tapped but another mode stayed selected.") from exc
            raise
        result.pop("mode_switched_facing", None)
        result["mode_opened"] = True
        if open_only:
            return None, None

        before = self.app.images(device) if self.app.CATALOG[key]["kind"] == "photo" else None
        # Right before the shutter: facing and mode again, and the shutter point from this very dump.
        root = device.ui()
        self.confirm(root, facing, "before_shutter", checks)
        if self.selected_mode(root) != key:
            raise RunError("MODE_NOT_SELECTED", f"{self.app.MODE_NAMES[key]} was no longer selected right before the shutter.")
        point = self.app.shutter_point(root, profile, key)
        if point is None:
            raise RunError("MODE_NOT_READY", "The shutter was not available right before the shot.")
        return point, before

    def check_after_capture(self, facing, key, result):
        device, profile = self.device, self.profile
        # The UI cannot be dumped while recording, so the facing is read again once the shot is finished.
        after = wait_for(lambda: ui_or_none(device), 6, "UI_UNAVAILABLE",
                         "The Camera UI was unavailable after the capture.")
        try:
            self.confirm(after, facing, "after_capture", result["facing_checks"])
            if self.selected_mode(after) != key:
                raise RunError("MODE_NOT_SELECTED", "The selected mode changed before the capture was verified.")
        except RunError:
            result["evidence_discarded"] = True
            raise
