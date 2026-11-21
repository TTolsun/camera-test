"""Command-line entry points for Camera Test."""

import argparse
import json
from pathlib import Path
import re
import sys
from uuid import uuid4

from .adb import Device, RunError, device_lock, select_device
from .checks import load_checks, resolve_check
from .hal import HalError, begin_trial, collected_trial, compare_logs, load_trial, log_tags, save_trial_logs
from .logcat import LOG_LINE
from .profiles import record_state_stats, selector_report
from .scenarios import SCENARIOS, active_profiles, read_profile_file


VERSION = "1.2.0"


def inspect_scenario(device, name, scenario, profile, version, no_ui):
    status = {"name": name, "status": "unknown", "reason": "UI_SCAN_SKIPPED"}
    result = {"scenario": status}
    if version is None:
        status.update(status="unavailable", reason="CAMERA_APP_NOT_FOUND")
        return result
    status["profile"] = profile.summary(version)
    if not no_ui:
        try:
            result["camera_ui"], seen = scenario.scan(device, profile)
            status.update(status="runnable" if seen else "unavailable",
                          reason="MODE_OBSERVED" if seen else "MODE_NOT_OBSERVED")
        except RunError as exc:
            result["camera_ui_error"] = exc.code
            status.update(reason=exc.code)
    if not profile.verifies(version):
        # The mode was found with the profile's selectors, so a verification run is worth trying.
        status.update(status="unverified" if status["status"] == "runnable" else "unavailable",
                      reason="UNSUPPORTED_CAMERA_VERSION")
    return result


def inspect_device(args, checks):
    profiles = active_profiles(args.profile_file)
    serial = select_device(args.device)
    device = Device(serial, uuid4().hex[:12])
    with device_lock(device.shell("getprop", "ro.serialno") or serial):
        registered = iter(SCENARIOS.items())
        primary_name, primary = next(registered)
        info = device.metadata()
        info["camera_version"] = device.package_version(profiles[primary_name].package)
        vendor = info["soc_vendor"]
        result = {
            "tool_version": VERSION,
            "device": info,
            "scenario": {},
            "checks": {
                "configured": args.checks_file is not None,
                "registered_for_device": [
                    {"name": item.name, "source": item.source, **({"vendor": item.vendor} if item.vendor else {})}
                    for item in checks if item.vendor is None or item.vendor == vendor
                ],
            },
        }
        try:
            service = device.shell("sh", "-c", "dumpsys media.camera | "
                                   "grep -E '^Number of camera devices:|^== Camera Provider HAL ' || true")
            number = re.search(r"^Number of camera devices: (\d+)", service, re.M)
            result["camera_service"] = {
                "camera_count": int(number[1]) if number else None,
                "providers": re.findall(r"^== Camera Provider HAL ([^\s]+)", service, re.M),
            }
        except RunError as exc:
            result["camera_service_error"] = exc.code
        result.update(inspect_scenario(device, primary_name, primary, profiles[primary_name],
                                       info["camera_version"], args.no_ui))
        if len(SCENARIOS) > 1:
            versions = {profiles[primary_name].package: info["camera_version"]}
            result["scenarios"] = [result["scenario"]]
            for name, scenario in registered:
                package = profiles[name].package
                if package not in versions:
                    versions[package] = device.package_version(package)
                registration = inspect_scenario(device, name, scenario, profiles[name], versions[package], args.no_ui)
                result["scenarios"].append(registration["scenario"])
        return result


def capture_failure(device, package, output, result):
    try:
        if not device.package_foreground(package):
            return
        image = device.screenshot()
        if image.startswith(b"\x89PNG\r\n\x1a\n"):
            path = output / "failure.png"
            path.write_bytes(image)
            result["artifacts"]["failure_screen"] = str(path)
    except (RunError, OSError):
        pass


def diagnose_failure(device, profile, output, result):
    """Keep the last UI dump and compare it with the profile so selector drift is visible."""
    if device.last_ui is None:
        return
    try:
        path = output / "failure-ui.xml"
        path.write_text(device.last_ui, encoding="utf-8")
        result["artifacts"]["failure_ui"] = str(path)
        result.setdefault("diagnosis", {})["ui"] = selector_report(profile, device.last_ui)
    except (OSError, ValueError):
        pass


def run(args, checks, result, output):
    scenario = SCENARIOS[args.scenario]
    profile = scenario.profile
    if args.profile_file:
        profile = read_profile_file(args.profile_file)
        if profile.scenario != args.scenario:
            raise RunError("INVALID_PROFILE_FILE", f"Profile is for {profile.scenario}, not {args.scenario}.")
    selected = select_device(args.device)
    device = Device(selected, result["run_id"])
    lock_key = device.shell("getprop", "ro.serialno") or selected
    with device_lock(lock_key):
        result["device"] = device.metadata()
        version = result["device"]["camera_version"] = device.package_version(profile.package)
        if version is None:
            raise RunError("CAMERA_APP_NOT_FOUND", f"{scenario.app_name} ({profile.package}) is not installed.")
        result["profile"] = profile.summary(version)
        if not profile.verifies(version):
            if not args.allow_unverified_version:
                verified = ", ".join(profile.verified_versions) or "none"
                raise RunError("UNSUPPORTED_CAMERA_VERSION",
                               f"{scenario.app_name} {version} is not a verified version of this profile ({verified}). "
                               "Rerun with --allow-unverified-version to verify it.")
            result["warnings"] = ["UNVERIFIED_CAMERA_VERSION"]

        check, reason = resolve_check(checks, args.check, result["device"]["soc_vendor"])
        if args.check:
            result["feature"]["reason"] = reason
        since = device.clock()
        tags = dict.fromkeys(profile.log_tags)
        if check:
            tags[check.tag] = None
            result["feature"]["source"] = check.source
            if check.vendor:
                result["feature"]["vendor"] = check.vendor
        try:
            recording_clock = scenario.execute(device, profile, args.duration, result)
            result["scenario_status"] = "PASS"
            if check:
                feature_log = device.logs(recording_clock, (check.tag,))
                matched = any(entry[2] == check.tag and check.expected.search(entry[3])
                              for line in feature_log.splitlines() if (entry := LOG_LINE.match(line)))
                result["feature"]["status"] = "PASS" if matched else "UNVERIFIED"
                if matched:
                    result["feature"].pop("reason", None)
                else:
                    result["feature"]["reason"] = "EXPECTED_LOG_NOT_FOUND"
            if args.pull_video:
                clip = output / scenario.video_filename
                try:
                    device.pull(result["video"]["device_path"], clip)
                    if clip.stat().st_size != result["video"]["bytes"]:
                        raise RunError("VIDEO_COPY_INCOMPLETE", "Local MP4 size does not match the device file.")
                except (RunError, OSError):
                    clip.unlink(missing_ok=True)
                    raise
                result["artifacts"]["video"] = str(clip)
        except (RunError, KeyboardInterrupt):
            if result["scenario_status"] != "PASS":
                capture_failure(device, profile.package, output, result)
                diagnose_failure(device, profile, output, result)
            raise
        finally:
            try:
                logs = output / "logcat.txt"
                text = device.logs(since, tags)
                logs.write_text(text + "\n", encoding="utf-8")
                result["artifacts"]["logcat"] = str(logs)
                if result["scenario_status"] != "PASS":
                    result.setdefault("diagnosis", {})["record_state_log"] = record_state_stats(profile, text)
            except (RunError, OSError):
                result["log_capture_error"] = "LOG_CAPTURE_FAILED"


def read_checks(path):
    try:
        return load_checks(path)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, re.error) as exc:
        raise RunError("INVALID_CHECKS_FILE", f"Check definition could not be read: {exc}") from exc


def run_hal(args):
    if args.hal_command == "compare":
        return compare_logs(args)
    if args.hal_command == "begin":
        tags = log_tags(args.tag)
        serial = select_device(args.device)
        device = Device(serial, uuid4().hex[:12])
        return begin_trial(args.output_dir, args.state, serial, device.clock(), tags), 0

    trial = load_trial(args.trial)
    if trial.log.is_file():
        return collected_trial(trial), 0
    device = Device(select_device(trial.serial), uuid4().hex[:12])
    with device_lock(device.shell("getprop", "ro.serialno") or trial.serial):
        if not trial.log.is_file():
            save_trial_logs(trial, device.logs(trial.since, trial.tags))
        return collected_trial(trial), 0


def run_profile(args):
    if args.profile_command == "show":
        text = json.dumps(SCENARIOS[args.scenario].profile.to_dict(), ensure_ascii=False, indent=2) + "\n"
        if args.output is None:
            if hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(encoding="utf-8")
            sys.stdout.write(text)
            return 0
        try:
            args.output.write_text(text, encoding="utf-8")
            result, exit_code = {"scenario": args.scenario, "profile_file": str(args.output.resolve())}, 0
        except OSError as exc:
            result, exit_code = {"error": {"code": "IO_ERROR", "message": str(exc)}}, 1
    else:
        try:
            profile = read_profile_file(args.path)
            result = {"valid": True, "scenario": profile.scenario, "verified_versions": list(profile.verified_versions)}
            exit_code = 0
        except RunError as exc:
            result, exit_code = {"valid": False, "error": {"code": exc.code, "message": str(exc)}}, 1
    print(json.dumps(result))
    return exit_code


def main():
    parser = argparse.ArgumentParser(description="Run repeatable camera scenarios on an ADB device.")
    parser.add_argument("--version", action="version", version=VERSION)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="List runnable scenarios as JSON.")
    inspection = commands.add_parser("inspect", help="Discover modes and compatible checks on the attached device.")
    inspection.add_argument("--device", help="ADB serial. Optional when exactly one physical device is ready.")
    inspection.add_argument("--checks-file", type=Path, help="JSON definitions for named log checks.")
    inspection.add_argument("--no-ui", action="store_true", help="Read device and camera service information without opening Camera.")
    inspection.add_argument("--pretty", action="store_true", help="Indent inspection JSON for terminal reading.")
    inspection.add_argument("--profile-file", type=Path, help="App profile JSON that replaces the built-in profile of the scenario it names.")
    profiles = commands.add_parser("profile", help="Export or validate app profiles without ADB.")
    profile_commands = profiles.add_subparsers(dest="profile_command", required=True)
    show = profile_commands.add_parser("show", help="Write the built-in profile of a scenario as JSON.")
    show.add_argument("scenario", choices=tuple(SCENARIOS))
    show.add_argument("--output", type=Path, help="Write a UTF-8 file instead of printing to stdout.")
    validate = profile_commands.add_parser("validate", help="Check a profile JSON against its scenario.")
    validate.add_argument("path", type=Path)
    hal = commands.add_parser("hal", help="Capture and compare Camera HAL logcat trials.")
    hal_commands = hal.add_subparsers(dest="hal_command", required=True)
    begin = hal_commands.add_parser("begin", help="Mark the start of an OFF or ON trial.")
    begin.add_argument("--state", choices=("off", "on"), required=True)
    begin.add_argument("--device", help="ADB serial. Optional when exactly one physical device is ready.")
    begin.add_argument("--output-dir", type=Path, default=Path("runs/exynos-hal"))
    begin.add_argument("--tag", action="append", default=[], help="Limit collection to a tag. Repeat for more tags.")
    collect = hal_commands.add_parser("collect", help="Save logcat from a marked trial.")
    collect.add_argument("--trial", type=Path, required=True, help="Trial JSON path emitted by hal begin.")
    compare = hal_commands.add_parser("compare", help="Compare two saved threadtime logs without ADB.")
    compare.add_argument("--off", type=Path, required=True)
    compare.add_argument("--on", type=Path, required=True)
    compare.add_argument("--tag", help="Compare message counts for a single tag.")
    compare.add_argument("--expect-log", help="Regular expression expected in ON messages for --tag.")
    compare.add_argument("--control-tag", help="Tag for a signal expected in both trials.")
    compare.add_argument("--control-log", help="Message regular expression expected in both trials.")
    command = commands.add_parser("run", help="Run a camera scenario and emit a short JSON result.")
    command.add_argument("scenario", choices=tuple(SCENARIOS))
    command.add_argument("--device", help="ADB serial. Optional when exactly one physical device is ready.")
    command.add_argument("--duration", type=int, choices=range(1, 61), metavar="SECONDS", default=5)
    command.add_argument("--output-dir", type=Path, default=Path("runs"))
    command.add_argument("--pull-video", action="store_true", help="Also copy the new MP4 to the run folder.")
    command.add_argument("--check", help="Optional named feature check.")
    command.add_argument("--checks-file", type=Path, help="JSON definitions for named log checks.")
    command.add_argument("--profile-file", type=Path, help="App profile JSON for this scenario, e.g. one adjusted for a new app build.")
    command.add_argument("--allow-unverified-version", action="store_true",
                         help="Run even when the app version is not in the profile's verified_versions.")
    args = parser.parse_args()
    if args.command == "list":
        print(json.dumps(list(SCENARIOS)))
        return 0
    if args.command == "profile":
        return run_profile(args)
    if args.command == "hal":
        try:
            result, exit_code = run_hal(args)
        except (HalError, RunError, OSError) as exc:
            result = {"error": {"code": getattr(exc, "code", "IO_ERROR"), "message": str(exc)}}
            exit_code = 1
        print(json.dumps(result))
        return exit_code
    if args.command == "inspect":
        try:
            result = inspect_device(args, read_checks(args.checks_file))
            exit_code = 0
        except (RunError, OSError) as exc:
            result = {"error": {"code": getattr(exc, "code", "IO_ERROR"), "message": str(exc)}}
            exit_code = 1
        if args.pretty and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        print(json.dumps(result, ensure_ascii=not args.pretty, indent=2 if args.pretty else None))
        return exit_code
    if args.checks_file and not args.check:
        parser.error("--checks-file requires --check")

    run_id = uuid4().hex[:12]
    output = args.output_dir.resolve() / run_id
    result = {"tool_version": VERSION, "run_id": run_id, "scenario": args.scenario, "scenario_status": "FAIL",
              "requested_seconds": args.duration, "steps": [], "artifacts": {}}
    if args.check:
        result["feature"] = {"name": args.check, "status": "UNVERIFIED", "reason": "CHECK_NOT_REGISTERED"}
    try:
        output.mkdir(parents=True)
        run(args, read_checks(args.checks_file), result, output)
    except RunError as exc:
        result["error"] = {"code": exc.code, "message": str(exc)}
    except OSError as exc:
        result["error"] = {"code": "IO_ERROR", "message": str(exc)[:240]}
    except KeyboardInterrupt:
        result["error"] = {"code": "INTERRUPTED", "message": "Camera test was interrupted."}

    if output.is_dir():
        path = output / "result.json"
        result["artifacts"]["result"] = str(path)
        try:
            path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError:
            result["artifacts"].pop("result")
            result["error"] = {"code": "RESULT_WRITE_FAILED", "message": "Could not save result.json."}
    print(json.dumps(result))
    if "error" in result or "log_capture_error" in result or result["scenario_status"] != "PASS":
        return 1
    return 2 if args.check and result["feature"]["status"] != "PASS" else 0
