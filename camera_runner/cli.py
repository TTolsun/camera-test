"""Command-line entry points for Camera Test."""

import argparse
import json
from pathlib import Path
import re
import shlex
import sys
from uuid import uuid4

from .adb import Device, RunError, device_lock, select_device
from . import preflight, screens
from .checks import load_checks, resolve_check
from .failures import failure_class
from .hal import HalError, begin_trial, collected_trial, compare_logs, load_trial, log_tags, save_trial_logs
from .logcat import LOG_LINE
from .profiles import record_state_stats, selector_report
from .scenarios import SCENARIOS, active_profiles, read_profile_file


VERSION = "1.3.0"


def inspect_scenario(device, name, scenario, profile, version, skip_reason):
    status = {"name": name, "status": "unknown", "reason": skip_reason or "UI_SCAN_SKIPPED"}
    result = {"scenario": status}
    if version is None:
        status.update(status="unavailable", reason="CAMERA_APP_NOT_FOUND")
        return result
    status["profile"] = profile.summary(version)
    if skip_reason is None:
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
        skip_reason = "UI_SCAN_SKIPPED" if args.no_ui else None
        if not args.no_ui:
            # Scanning taps the screen, so a locked or sleeping phone is reported instead of scanned.
            result["preflight"] = preflight.measure(device)
            if failed := preflight.verdict(result["preflight"]):
                result["preflight"]["failed"] = skip_reason = failed[0]
        result.update(inspect_scenario(device, primary_name, primary, profiles[primary_name],
                                       info["camera_version"], skip_reason))
        if len(SCENARIOS) > 1:
            versions = {profiles[primary_name].package: info["camera_version"]}
            result["scenarios"] = [result["scenario"]]
            for name, scenario in registered:
                package = profiles[name].package
                if package not in versions:
                    versions[package] = device.package_version(package)
                registration = inspect_scenario(device, name, scenario, profiles[name], versions[package], skip_reason)
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


def diagnose_failure(device, scenario, profile, output, result):
    """Keep the last UI dump and compare it with the selectors the failing phase relies on."""
    if device.phase:
        result.setdefault("diagnosis", {})["phase"] = device.phase
    if device.last_ui is None:
        return
    try:
        path = output / "failure-ui.xml"
        path.write_text(device.last_ui, encoding="utf-8")
        result["artifacts"]["failure_ui"] = str(path)
        result.setdefault("diagnosis", {})["ui"] = selector_report(
            profile, device.last_ui, scenario.phases.get(device.phase))
    except (OSError, ValueError):
        pass


def warn(result, code):
    result.setdefault("warnings", []).append(code)


def cleanup_video(device, result):
    """Delete only the exact MP4 this run created. A failed deletion is a warning, not a failure."""
    video = result.get("video")
    path = video.get("device_path", "") if video else ""
    if not (path.startswith("/sdcard/") and path.lower().endswith(".mp4") and "\n" not in path):
        warn(result, "VIDEO_CLEANUP_FAILED")
        return
    try:
        video["deleted"] = device.shell("sh", "-c", f"rm {shlex.quote(path)} && echo deleted") == "deleted"
    except RunError:
        video["deleted"] = False
    if not video["deleted"]:
        warn(result, "VIDEO_CLEANUP_FAILED")


def finish_screens(profile, golden, output, saved, result):
    """Record the captured checkpoints and, when a golden set is given, attach the drift report."""
    result["screens"] = list(saved)
    if not saved:
        return
    result["artifacts"]["screens"] = str(output / "screens")
    if golden is None:
        return
    try:
        drift = screens.compare(profile, golden, screens.read_snapshots(output / "screens"))
    except RunError as exc:
        result["drift_error"] = exc.code
        return
    result["drift"] = drift
    if drift["severity"] != "none":
        warn(result, f"UI_DRIFT_{drift['severity'].upper()}")


def run(args, checks, result, output):
    scenario = SCENARIOS[args.scenario]
    profile = scenario.profile
    if args.profile_file:
        profile = read_profile_file(args.profile_file)
        if profile.scenario != args.scenario:
            raise RunError("INVALID_PROFILE_FILE", f"Profile is for {profile.scenario}, not {args.scenario}.")
    golden = screens.load_golden(args.golden, args.scenario)[1] if args.golden else None
    selected = select_device(args.device)
    device = Device(selected, result["run_id"])
    saved = []
    if args.capture_screens or golden is not None:
        device.checkpoint_sink = screens.snapshot_sink(output / "screens", profile.package, saved)
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
            warn(result, "UNVERIFIED_CAMERA_VERSION")

        check, reason = resolve_check(checks, args.check, result["device"]["soc_vendor"])
        if args.check:
            result["feature"]["reason"] = reason
        result["preflight"] = preflight.check(device)
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
            if args.cleanup_video:
                cleanup_video(device, result)
        except (RunError, KeyboardInterrupt):
            if result["scenario_status"] != "PASS":
                capture_failure(device, profile.package, output, result)
                diagnose_failure(device, scenario, profile, output, result)
            raise
        finally:
            finish_screens(profile, golden, output, saved, result)
            try:
                logs = output / "logcat.txt"
                text = device.logs(since, tags)
                logs.write_text(text + "\n", encoding="utf-8")
                result["artifacts"]["logcat"] = str(logs)
                if result["scenario_status"] != "PASS":
                    result.setdefault("diagnosis", {})["record_state_log"] = record_state_stats(profile, text)
            except (RunError, OSError):
                result["log_capture_error"] = "LOG_CAPTURE_FAILED"


def classify(result):
    if "error" in result:
        result["failure_class"] = failure_class(result["error"]["code"], result.get("diagnosis"))


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


def run_screens(args):
    """Create or compare golden screen sets. Neither uses ADB."""
    if args.screens_command == "promote":
        return {"golden": str(args.output.resolve()), **screens.promote(args.run, args.output, args.force)}
    manifest, golden = screens.load_golden(args.golden)
    if args.profile_file:
        profile = read_profile_file(args.profile_file)
    elif manifest.get("scenario") in SCENARIOS:
        profile = SCENARIOS[manifest["scenario"]].profile
    else:
        raise RunError("INVALID_GOLDEN", f"Golden set names an unknown scenario: {manifest.get('scenario')!r}.")
    if not (args.run / "screens").is_dir():
        raise RunError("SCREENS_NOT_CAPTURED", "The run has no screens. Rerun with --capture-screens.")
    summary = {key: manifest.get(key) for key in ("scenario", "app_version", "created")}
    report = screens.compare(profile, golden, screens.read_snapshots(args.run / "screens"))
    return {"golden": summary, "profile": profile.source, **report}


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
    screen_cmd = commands.add_parser("screens", help="Create and compare golden UI snapshot sets without ADB.")
    screen_commands = screen_cmd.add_subparsers(dest="screens_command", required=True)
    promote = screen_commands.add_parser("promote", help="Turn a passing run that used --capture-screens into a golden set.")
    promote.add_argument("--run", type=Path, required=True, help="Run folder, e.g. runs/<run-id>.")
    promote.add_argument("--output", type=Path, required=True, help="Golden set folder to create.")
    promote.add_argument("--force", action="store_true", help="Replace an existing golden set.")
    screen_compare = screen_commands.add_parser("compare", help="Report UI drift between a golden set and a run.")
    screen_compare.add_argument("--golden", type=Path, required=True)
    screen_compare.add_argument("--run", type=Path, required=True)
    screen_compare.add_argument("--profile-file", type=Path, help="Profile the run used, when it was not the built-in one.")
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
    command.add_argument("--capture-screens", action="store_true",
                         help="Save normalized UI snapshots at checkpoints under runs/<run-id>/screens.")
    command.add_argument("--golden", type=Path,
                         help="Golden set folder. Implies --capture-screens and adds a drift report; never changes the verdict.")
    command.add_argument("--cleanup-video", action="store_true",
                         help="After verification, delete the exact MP4 this run created from the device.")
    command.add_argument("--allow-unverified-version", action="store_true",
                         help="Run even when the app version is not in the profile's verified_versions.")
    args = parser.parse_args()
    if args.command == "list":
        print(json.dumps(list(SCENARIOS)))
        return 0
    if args.command == "profile":
        return run_profile(args)
    if args.command == "screens":
        try:
            result, exit_code = run_screens(args), 0
        except (RunError, OSError) as exc:
            result = {"error": {"code": getattr(exc, "code", "IO_ERROR"), "message": str(exc)}}
            exit_code = 1
        print(json.dumps(result, ensure_ascii=False))
        return exit_code
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

    classify(result)
    if output.is_dir():
        path = output / "result.json"
        result["artifacts"]["result"] = str(path)
        try:
            path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError:
            result["artifacts"].pop("result")
            result["error"] = {"code": "RESULT_WRITE_FAILED", "message": "Could not save result.json."}
            classify(result)
    print(json.dumps(result))
    if "error" in result or "log_capture_error" in result or result["scenario_status"] != "PASS":
        return 1
    return 2 if args.check and result["feature"]["status"] != "PASS" else 0
