"""Polling and UI controls shared by Android camera apps."""

import re
import time

from .adb import RunError


def wait_for(probe, timeout, code, message, interval=0.25):
    deadline = time.monotonic() + timeout
    while True:
        value = probe()
        if value is not None and value is not False:
            return value
        if time.monotonic() >= deadline:
            raise RunError(code, message)
        time.sleep(interval)


def center(node):
    bounds = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
    if bounds:
        left, top, right, bottom = map(int, bounds.groups())
        if left < right and top < bottom:
            return (left + right) // 2, (top + bottom) // 2
    raise RunError("INVALID_UI_BOUNDS", "Camera control is not visible on screen.")


def ui_or_none(device):
    """A UI dump, or None while the preview animates and the dump is briefly unavailable."""
    try:
        return device.ui()
    except RunError as exc:
        if exc.code != "UI_UNAVAILABLE":
            raise
        return None
