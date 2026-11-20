"""Scenarios shared by the list, inspect and run commands."""

from collections.abc import Callable
from dataclasses import dataclass

from ..adb import Device
from . import galaxy_portrait_video


@dataclass(frozen=True)
class Scenario:
    package: str
    app_name: str
    supported_version: str
    log_tags: tuple[str, ...]
    video_filename: str
    scan: Callable[[Device], tuple[dict, bool]]
    execute: Callable[[Device, int, dict], str]


SCENARIOS = {
    "galaxy.portrait-video": Scenario(
        package=galaxy_portrait_video.PACKAGE,
        app_name="Galaxy Camera",
        supported_version=galaxy_portrait_video.CAMERA_VERSION,
        log_tags=galaxy_portrait_video.LOG_TAGS,
        video_filename="portrait-video.mp4",
        scan=galaxy_portrait_video.scan_modes,
        execute=galaxy_portrait_video.execute,
    ),
}
