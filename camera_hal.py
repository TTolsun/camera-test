"""Capture bounded logcat trials and compare them without a device."""

from collections import Counter
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import tempfile

from camera_checks import LOG_CLOCK_PATTERN, LOG_LINE, LOG_TAG_PATTERN


SUMMARY_LIMIT = 10


class HalError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Trial:
    path: Path
    state: str
    serial: str
    since: str
    tags: tuple[str, ...]

    @property
    def log(self):
        return self.path.with_name("logcat.txt")


def log_tags(values):
    tags = tuple(values)
    if not all(isinstance(tag, str) and LOG_TAG_PATTERN.fullmatch(tag) for tag in tags):
        raise HalError("INVALID_LOG_TAG", "Use a log tag containing letters, digits, dots, underscores or hyphens.")
    return tuple(dict.fromkeys(tags))


def begin_trial(root, state, serial, since, tags):
    root.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix=f"{state}-", dir=root.resolve())) / "trial.json"
    marker = {"schema_version": 1, "state": state, "serial": serial, "since": since, "tags": tags}
    path.write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
    return {"status": "READY", "state": state, "trial": str(path), "since": since}


def load_trial(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if (data["schema_version"] != 1 or data["state"] not in ("off", "on")
                or not isinstance(data["serial"], str) or not data["serial"]
                or not LOG_CLOCK_PATTERN.fullmatch(data["since"]) or not isinstance(data["tags"], list)):
            raise ValueError("invalid trial fields")
        return Trial(path.resolve(), data["state"], data["serial"], data["since"], log_tags(data["tags"]))
    except (OSError, ValueError, TypeError, KeyError, HalError) as exc:
        raise HalError("INVALID_TRIAL", "Use the trial.json path returned by hal begin.") from exc


def save_trial_logs(trial, lines):
    file = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                       dir=trial.path.parent, delete=False)
    temporary = Path(file.name)
    try:
        with file:
            if lines:
                file.write(lines + "\n")
        os.replace(temporary, trial.log)
    finally:
        temporary.unlink(missing_ok=True)


def collected_trial(trial):
    with trial.log.open(encoding="utf-8-sig") as file:
        entries = sum(1 for line in file if LOG_LINE.match(line))
    return {"status": "COLLECTED", "state": trial.state, "trial": str(trial.path),
            "logcat": str(trial.log), "entries": entries}


def summarize_log(path, tag, expected, control_tag, control):
    tags = Counter()
    messages = Counter()
    matches = 0
    controls = 0
    with path.open(encoding="utf-8-sig", errors="replace") as file:
        for line in file:
            entry = LOG_LINE.match(line)
            if not entry:
                continue
            observed_tag, message = entry[2], entry[3]
            tags[observed_tag] += 1
            if observed_tag == tag:
                if expected is None:
                    messages[message] += 1
                elif expected.search(message):
                    matches += 1
            if control is not None and observed_tag == control_tag and control.search(message):
                controls += 1
    if not tags:
        raise HalError("EMPTY_LOG", f"No threadtime log entries in {path.name}.")
    return tags, messages, matches, controls


def changes(off, on, field):
    keys = off.keys() | on.keys()
    changed = [key for key in keys if off[key] != on[key]]
    changed.sort(key=lambda key: (-abs(on[key] - off[key]), key))
    return [{field: key[:160], "off": off[key], "on": on[key]} for key in changed[:SUMMARY_LIMIT]]


def compare_logs(args):
    if args.off.resolve() == args.on.resolve():
        raise HalError("SAME_LOG", "Provide separate OFF and ON log files.")
    log_tags(tag for tag in (args.tag, args.control_tag) if tag is not None)
    if args.expect_log is not None and (not args.tag or not args.expect_log.strip()):
        raise HalError("INVALID_PATTERN", "--expect-log requires --tag and a nonempty regular expression.")
    if args.control_tag is not None or args.control_log is not None:
        if not args.expect_log or not args.control_tag or not args.control_log or not args.control_log.strip():
            raise HalError("INVALID_CONTROL", "Use --control-tag and --control-log together with --expect-log.")
    try:
        expected = re.compile(args.expect_log) if args.expect_log else None
        control = re.compile(args.control_log) if args.control_log else None
    except re.error as exc:
        raise HalError("INVALID_PATTERN", str(exc)) from exc

    off_tags, off_messages, off_matches, off_controls = summarize_log(args.off, args.tag, expected, args.control_tag, control)
    on_tags, on_messages, on_matches, on_controls = summarize_log(args.on, args.tag, expected, args.control_tag, control)
    result = {"status": "COMPARED", "entries": {"off": sum(off_tags.values()), "on": sum(on_tags.values())}}
    if args.tag is None:
        result["tag_changes"] = changes(off_tags, on_tags, "tag")
        return result, 0

    result["tag"] = args.tag
    result["tag_entries"] = {"off": off_tags[args.tag], "on": on_tags[args.tag]}
    if expected is None:
        result["message_changes"] = changes(off_messages, on_messages, "message")
        return result, 0

    result["matches"] = {"off": off_matches, "on": on_matches}
    if control is not None:
        result["control"] = {"tag": args.control_tag, "off": off_controls, "on": on_controls}
        if not (off_controls and on_controls):
            result["status"] = "CONTROL_MISSING"
            return result, 2
    if off_matches:
        result["status"] = "BOTH_MATCH" if on_matches else "OFF_ONLY"
    else:
        result["status"] = "ON_ONLY" if on_matches else "NO_MATCH"
    return result, 0 if result["status"] == "ON_ONLY" else 2
