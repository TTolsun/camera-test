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


def measure(device):
    return {"screen": screen_state(device), "lock": lock_state(device),
            "free_bytes": free_bytes(device), "min_free_bytes": MIN_FREE_BYTES}


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
    return None


def check(device):
    """Measure the device and raise RunError when the environment is not usable."""
    measured = measure(device)
    if failed := verdict(measured):
        raise RunError(*failed)
    return measured
