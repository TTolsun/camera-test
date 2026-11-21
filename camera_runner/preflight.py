"""Environment checks that run before a scenario touches the UI.

Signals were read on a Galaxy running Android 17 with `dumpsys`; a signal that is
missing on another Android version is recorded as "unknown" and never fails the run.
"""

import re

from .adb import RunError


MIN_FREE_BYTES = 500 * 1024 * 1024
STORAGE_PATH = "/sdcard"


def screen_state(device):
    """`mWakefulness=Awake` is interactive; `Asleep` and `Dozing` mean the screen is off."""
    text = device.shell("sh", "-c", "dumpsys power | grep -m1 -E '^ *mWakefulness=' || true")
    found = re.search(r"mWakefulness=(\w+)", text)
    if not found:
        return "unknown"
    return "awake" if found[1] == "Awake" else "off"


def lock_state(device):
    """`isKeyguardShowing=` (newer Android) or `mShowingLockscreen=` (older) in `dumpsys window`."""
    text = device.shell("sh", "-c", "dumpsys window | grep -m1 -E 'isKeyguardShowing=|mShowingLockscreen=' || true")
    found = re.search(r"(?:isKeyguardShowing|mShowingLockscreen)=(true|false)", text)
    if not found:
        return "unknown"
    return "locked" if found[1] == "true" else "unlocked"


def free_bytes(device):
    """Available space from the last line of `df -k`, whose columns end with `1K-blocks Used Available Use%`."""
    line = device.shell("df", "-k", STORAGE_PATH).splitlines()[-1:] or [""]
    found = re.search(r"\s(\d+)\s+(\d+)\s+(\d+)\s+\d+%", line[0])
    return int(found[3]) * 1024 if found else None


def foreground_state(device, camera_package):
    """Who owns the screen: the camera or the HOME launcher is `idle`; any other app is `busy`.

    The launcher is resolved only when another app is on top. If the top activity or the
    launcher cannot be read, the state is `unknown` and never fails the run.
    """
    top = device.top_package()
    launcher = None
    if top is None:
        state = "unknown"
    elif top == camera_package:
        state = "idle"
    else:
        launcher = device.launcher_package()
        state = "unknown" if launcher is None else "idle" if top == launcher else "busy"
    return {"package": top, "launcher": launcher, "state": state}


def measure(device, idle_package=None):
    """`idle_package` (the camera package) additionally measures whether another app is in the foreground."""
    measured = {"screen": screen_state(device), "lock": lock_state(device),
                "free_bytes": free_bytes(device), "min_free_bytes": MIN_FREE_BYTES}
    if idle_package is not None:
        measured["foreground"] = foreground_state(device, idle_package)
    return measured


def verdict(measured):
    """Return the first failing (code, message), or None. Unknown signals do not fail."""
    if measured["screen"] == "off":
        return "PRECHECK_SCREEN_OFF", "The phone screen is off. Wake it before running."
    if measured["lock"] == "locked":
        return "PRECHECK_LOCKED", "The phone is on the lock screen. Unlock it before running."
    free = measured["free_bytes"]
    if free is not None and free < measured["min_free_bytes"]:
        return "PRECHECK_LOW_STORAGE", (f"Only {free // 1048576} MB is free on {STORAGE_PATH}; "
                                        f"at least {measured['min_free_bytes'] // 1048576} MB is required.")
    foreground = measured.get("foreground", {})
    if foreground.get("state") == "busy":
        return "PRECHECK_DEVICE_BUSY", (f"{foreground['package']} is in the foreground, not the camera or the launcher. "
                                        "Another person may be using the phone.")
    return None


def check(device, idle_package=None):
    """Measure the device and raise RunError when the environment is not usable."""
    measured = measure(device, idle_package)
    if failed := verdict(measured):
        raise RunError(*failed)
    return measured
