"""App profiles: the selectors, labels and log signals a scenario depends on, kept as data.

A profile lists the app versions it was verified on. Supporting a new app build means
editing profile data (built in, or a --profile-file JSON), not scenario code.
"""

from dataclasses import dataclass
from difflib import get_close_matches
import re
from xml.etree import ElementTree

from .logcat import LOG_LINE


SCHEMA_VERSION = 1
PROFILE_FIELDS = {"schema_version", "scenario", "package", "activity", "verified_versions", "selectors", "labels", "log", "volatile_ids"}


@dataclass(frozen=True)
class AppProfile:
    scenario: str
    package: str
    activity: str
    verified_versions: tuple
    selectors: dict
    labels: dict
    log_tags: tuple
    record_state_tag: str | None
    record_state_pattern: re.Pattern | None
    source: str
    volatile_ids: tuple = ()  # Resource ids drift comparison ignores (transient hints and the like).

    def resource_id(self, key):
        return f"{self.package}:id/{self.selectors[key]}"

    def label_set(self, key):
        return frozenset(label.strip().casefold() for label in self.labels[key])

    def verifies(self, version):
        return version in self.verified_versions

    def summary(self, version):
        return {"source": self.source, "verified_versions": list(self.verified_versions),
                "version_verified": self.verifies(version)}

    def to_dict(self):
        log = {"tags": list(self.log_tags)}
        if self.record_state_pattern is not None:
            log["record_state"] = {"tag": self.record_state_tag, "pattern": self.record_state_pattern.pattern}
        return {
            "schema_version": SCHEMA_VERSION,
            "scenario": self.scenario,
            "package": self.package,
            "activity": self.activity,
            "verified_versions": list(self.verified_versions),
            "selectors": dict(self.selectors),
            "labels": {key: list(values) for key, values in self.labels.items()},
            "volatile_ids": list(self.volatile_ids),
            "log": log,
        }


def _text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string.")
    return value


def _strings(value, field, allow_empty=False):
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError(f"{field} must be a list of non-empty strings.")
    if not value and not allow_empty:
        raise ValueError(f"{field} must not be empty.")
    return tuple(value)


def _keyed(value, field, expected, convert):
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object.")
    if expected is not None:
        # Unknown keys are rejected so that a misspelled selector cannot silently fall back.
        missing, unknown = sorted(set(expected) - set(value)), sorted(set(value) - set(expected))
        if missing or unknown:
            raise ValueError(f"{field} keys must match the scenario (missing: {missing}, unknown: {unknown}).")
    return {key: convert(item, f"{field}.{key}") for key, item in value.items()}


def parse_profile(data, source, template=None):
    """Validate profile JSON. With a template, the key sets must match the built-in profile."""
    if not isinstance(data, dict):
        raise ValueError("Profile must be a JSON object.")
    if unknown := sorted(set(data) - PROFILE_FIELDS):
        raise ValueError(f"Unknown profile fields: {unknown}.")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"schema_version must be {SCHEMA_VERSION}.")
    scenario = _text(data.get("scenario"), "scenario")
    if template is not None and scenario != template.scenario:
        raise ValueError(f"Profile is for {scenario}, not {template.scenario}.")
    log = data.get("log")
    if not isinstance(log, dict):
        raise ValueError("log must be an object.")
    tags = _strings(log.get("tags"), "log.tags")
    record_tag = pattern = None
    if "record_state" in log or (template and template.record_state_pattern is not None):
        signal = log.get("record_state")
        if not isinstance(signal, dict):
            raise ValueError("log.record_state must be an object.")
        record_tag = _text(signal.get("tag"), "log.record_state.tag")
        pattern = re.compile(_text(signal.get("pattern"), "log.record_state.pattern"))
        if pattern.groups != 2:
            raise ValueError("log.record_state.pattern must capture exactly two groups: the previous and next state.")
        tags = [*tags, record_tag]
    selectors = _keyed(data.get("selectors"), "selectors", template and template.selectors, _text)
    volatile = _strings(data.get("volatile_ids", []), "volatile_ids", allow_empty=True)
    if overlap := sorted(set(volatile) & set(selectors.values())):
        raise ValueError(f"volatile_ids must not contain profile selectors: {overlap}.")
    return AppProfile(
        scenario=scenario,
        package=_text(data.get("package"), "package"),
        activity=_text(data.get("activity"), "activity"),
        verified_versions=_strings(data.get("verified_versions"), "verified_versions", allow_empty=True),
        selectors=selectors,
        labels=_keyed(data.get("labels"), "labels", template and template.labels, _strings),
        log_tags=tuple(dict.fromkeys(tags)),
        record_state_tag=record_tag,
        record_state_pattern=pattern,
        source=source,
        volatile_ids=volatile,
    )


def decode_json_text(raw):
    # Windows PowerShell 5.1 redirection writes UTF-16 with a BOM; accept it as well as UTF-8.
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    return raw.decode("utf-8-sig")


def selector_report(profile, xml_text, expected=None, limit=80):
    """Compare profile selectors with one captured UI hierarchy and suggest renamed IDs.

    `expected` limits the absent list to the selector keys the failing phase relies on.
    """
    root = ElementTree.fromstring(xml_text)
    prefix = f"{profile.package}:id/"
    labels = {label.strip().casefold() for values in profile.labels.values() for label in values}
    on_screen, labeled = [], []
    for node in root.iter("node"):
        if node.get("package") != profile.package:
            continue
        resource_id = node.get("resource-id", "")
        short = resource_id[len(prefix):] if resource_id.startswith(prefix) else None
        if short and short not in on_screen:
            on_screen.append(short)
        texts = [text for key in ("text", "content-desc") if (text := node.get(key, "").strip())]
        if matched := [text for text in texts if text.casefold() in labels]:
            labeled.append({"resource_id": short, "label": matched[0]})
    return {
        "present": [key for key, selector in profile.selectors.items() if selector in on_screen],
        "absent": {
            key: {"expected": selector, "similar": get_close_matches(selector, on_screen, n=3, cutoff=0.6)}
            for key, selector in profile.selectors.items()
            if selector not in on_screen and (expected is None or key in expected)
        },
        "labeled_nodes": labeled[:20],
        "resource_ids": on_screen[:limit],
    }


def record_state_stats(profile, log_text):
    """Tag lines without pattern matches mean the recording-state message format changed."""
    lines = matched = 0
    for line in log_text.splitlines():
        entry = LOG_LINE.match(line)
        if entry and entry[2] == profile.record_state_tag:
            lines += 1
            matched += bool(profile.record_state_pattern.search(entry[3]))
    return {"tag": profile.record_state_tag, "lines": lines, "matched": matched}
