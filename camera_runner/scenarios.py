"""Scenarios shared by the list, inspect, run and profile commands."""

from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
import json
from pathlib import Path
import re

from .adb import Device, RunError
from .apps import galaxy_camera
from .capture import CaptureSession
from .profiles import AppProfile, decode_json_text, parse_profile


@dataclass(frozen=True)
class Scenario:
    app_name: str
    profile: AppProfile
    video_filename: str
    scan: Callable[[Device, AppProfile], tuple[dict, bool]]
    execute: Callable[[Device, AppProfile, int, dict], str]
    # Phase name -> profile selector keys that phase relies on. Failure diagnosis only reports
    # those selectors as absent; a scenario without phases reports every selector.
    phases: dict = field(default_factory=dict)


def execute_mode(app, key, device, profile, seconds, result):
    return CaptureSession(device, profile, app).capture(key, seconds, result)


SCENARIOS = {
    "galaxy.portrait-video": Scenario(
        app_name=galaxy_camera.NAME,
        profile=galaxy_camera.profile,
        video_filename="portrait-video.mp4",
        scan=galaxy_camera.scan_modes,
        execute=partial(execute_mode, galaxy_camera, "portrait_video"),
        phases=galaxy_camera.PHASES,
    ),
}


def read_profile_file(path):
    """Load a profile JSON and validate it against the built-in profile of the scenario it names."""
    try:
        data = json.loads(decode_json_text(Path(path).read_bytes()))
    except (OSError, UnicodeError, ValueError) as exc:
        raise RunError("INVALID_PROFILE_FILE", f"Profile could not be read: {exc}") from exc
    name = data.get("scenario") if isinstance(data, dict) else None
    if name not in SCENARIOS:
        raise RunError("INVALID_PROFILE_FILE", f"Profile names an unknown scenario: {name!r}.")
    try:
        return parse_profile(data, str(path), template=SCENARIOS[name].profile)
    except (ValueError, re.error) as exc:
        raise RunError("INVALID_PROFILE_FILE", f"Profile is invalid: {exc}") from exc


def active_profiles(path):
    """Built-in profiles, with the scenario named by an optional profile file replaced."""
    profiles = {name: scenario.profile for name, scenario in SCENARIOS.items()}
    if path is not None:
        override = read_profile_file(path)
        profiles[override.scenario] = override
    return profiles


def resolve_profile(name, path):
    """The profile a run of scenario `name` uses: the built-in one, or the profile file when given."""
    if path is None:
        return SCENARIOS[name].profile
    profile = read_profile_file(path)
    if profile.scenario != name:
        raise RunError("INVALID_PROFILE_FILE", f"Profile is for {profile.scenario}, not {name}.")
    return profile
