"""Load named app and HAL log checks and resolve them for the device SoC."""

from dataclasses import dataclass
import json
import re

from .logcat import LOG_TAG_PATTERN


@dataclass(frozen=True)
class LogCheck:
    name: str
    source: str
    vendor: str | None
    tag: str
    expected: re.Pattern


def load_checks(path):
    if path is None:
        return []
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or data.get("schema_version") != 1 or not isinstance(data.get("checks"), list):
        raise ValueError("Expected schema_version 1 and a checks array.")

    checks = {}
    sources = {}
    for entry in data["checks"]:
        name, source, tag = (entry[key] for key in ("name", "source", "tag"))
        vendor = entry.get("vendor")
        pattern = entry["expect_log"]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name) or not LOG_TAG_PATTERN.fullmatch(tag):
            raise ValueError("Invalid check name or log tag.")
        if source == "hal":
            if vendor not in ("exynos", "qualcomm"):
                raise ValueError(f"HAL check '{name}' must specify vendor 'exynos' or 'qualcomm'.")
        elif source != "app" or vendor is not None:
            raise ValueError(f"Check '{name}' needs source 'app' without a vendor or source 'hal' with one.")
        if not pattern.strip():
            raise ValueError(f"Check '{name}' has an empty log pattern.")
        key = (name, vendor)
        if key in checks or (name in sources and sources[name] != source):
            raise ValueError(f"Duplicate or conflicting definitions for check '{name}'.")
        sources[name] = source
        checks[key] = LogCheck(name, source, vendor, tag, re.compile(pattern))
    return list(checks.values())


def resolve_check(checks, name, vendor):
    matches = [check for check in checks if check.name == name]
    if not matches:
        return None, "CHECK_NOT_REGISTERED"
    for check in matches:
        if check.vendor is None or check.vendor == vendor:
            return check, "CHECK_NOT_RUN"
    return None, "SOC_VENDOR_UNKNOWN" if vendor == "unknown" else "CHECK_VENDOR_MISMATCH"
