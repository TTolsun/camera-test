"""Scenarios shared by the list, inspect, run and profile commands."""

from collections.abc import Callable
from dataclasses import dataclass
import json
from pathlib import Path
import re

from ..adb import Device, RunError
from ..profiles import AppProfile, decode_json_text, parse_profile
from . import galaxy_portrait_video


@dataclass(frozen=True)
class Scenario:
    app_name: str
    profile: AppProfile
    video_filename: str
    scan: Callable[[Device, AppProfile], tuple[dict, bool]]
    execute: Callable[[Device, AppProfile, int, dict], str]


SCENARIOS = {
    "galaxy.portrait-video": Scenario(
        app_name="Galaxy Camera",
        profile=parse_profile(galaxy_portrait_video.PROFILE, "built-in"),
        video_filename="portrait-video.mp4",
        scan=galaxy_portrait_video.scan_modes,
        execute=galaxy_portrait_video.execute,
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
