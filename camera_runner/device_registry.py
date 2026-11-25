"""Persistent registry of verified camera captures, keyed by device model, package, app version and camera facing.

The registry is only ever changed by `update`, which holds a file lock, re-reads the file, applies one
run and replaces the file atomically. A failed run records what it saw but never removes or replaces
verified evidence, and a new app version starts with an empty entry of its own.
"""

from hashlib import sha256
from pathlib import Path
import time

from .adb import RunError, device_lock
from .json_files import read_json_file, write_json_atomic

SCHEMA_VERSION = 2
# Version 1 was produced exclusively by the built-in Galaxy Camera runner.
LEGACY_PACKAGE = "com.sec.android.app.camera"
HISTORY_LIMIT = 100
DEVICE_DIRECTORY = Path("runs/devices")
LOCK_TRIES = 10
# Statuses that say nothing new about the mode: environment errors, skipped steps and modes not on screen.
NOT_RECORDED = {"SKIPPED", "ENVIRONMENT_ERROR", "NOT_OBSERVED"}


def device_path(hardware):
    """Keep USB and wireless runs of one handset together, independently of its model."""
    if not hardware or hardware.strip().casefold() in {"", "unknown", "null", "none"}:
        raise RunError("DEVICE_ID_UNAVAILABLE", "ADB did not return a hardware serial for this device.")
    key = sha256(hardware.strip().encode("utf-8")).hexdigest()
    return (DEVICE_DIRECTORY / key / "registry.json").resolve()


def entry_key(model, version, facing, package):
    return f"{package}|{model}|{version}|{facing}"


def load(path):
    """The registry content. A missing file is empty; a damaged file stops the run instead of being overwritten."""
    if not Path(path).exists():
        return {"schema_version": SCHEMA_VERSION, "entries": {}}
    data = read_json_file(path, "REGISTRY_INVALID", "Registry")
    version = data.get("schema_version")
    if version not in (1, SCHEMA_VERSION) or not isinstance(data.get("entries"), dict):
        raise RunError("REGISTRY_INVALID", "Registry has an unknown layout.")
    entries = {}
    for key, entry in data["entries"].items():
        if not isinstance(entry, dict):
            raise RunError("REGISTRY_INVALID", "Registry contains an invalid entry.")
        model, app_version, facing = (entry.get(field) for field in ("model", "app_version", "facing"))
        package = LEGACY_PACKAGE if version == 1 else entry.get("package")
        if not all(isinstance(field, str) and field for field in (model, app_version, facing, package)):
            raise RunError("REGISTRY_INVALID", "Registry entry has an incomplete identity.")
        qualified = entry_key(model, app_version, facing, package)
        expected = f"{model}|{app_version}|{facing}" if version == 1 else qualified
        if key != expected:
            raise RunError("REGISTRY_INVALID", "Registry key does not match its entry.")
        entries[qualified] = {**entry, "package": package}
        inventory = entry.get("inventory")
        if inventory is not None and not (isinstance(inventory, dict) and isinstance(inventory.get("observed"), list)
                and all(isinstance(label, str) for label in inventory["observed"]) and inventory.get("run_id")):
            raise RunError("REGISTRY_INVALID", "Registry contains an invalid mode inventory.")
        modes = entry.get("modes")
        if not isinstance(modes, dict) or not isinstance(entry.get("history"), list) or not entry.get("updated"):
            raise RunError("REGISTRY_INVALID", "Registry entry is missing its mode results or history.")
        for mode in modes.values():
            if not isinstance(mode, dict) or (mode.get("verified") is not None and not
                    (isinstance(mode["verified"], dict) and isinstance(mode["verified"].get("at"), str))):
                raise RunError("REGISTRY_INVALID", "Registry contains invalid verification evidence.")
            for field in ("opened", "last"):
                value = mode.get(field)
                if value is not None and (not isinstance(value, dict) or not isinstance(value.get("run_id"), str)
                        or (field == "last" and not isinstance(value.get("status"), str))):
                    raise RunError("REGISTRY_INVALID", f"Registry contains an invalid {field} result.")
    return {**data, "schema_version": SCHEMA_VERSION, "entries": entries}


def baseline(data, run, facing, key):
    """The earlier entry to compare an inventory with: this version's own, else the newest other version."""
    own = data["entries"].get(key)
    if own and own.get("inventory"):
        return own
    others = [entry for entry in data["entries"].values()
              if entry["package"] == run["package"] and entry.get("model") == run["model"] and entry.get("facing") == facing
              and entry.get("app_version") != run["version"] and entry.get("inventory")]
    return max(others, key=lambda entry: entry["updated"], default=None)


def compare_inventory(previous, observed):
    before = {label.casefold(): label for label in previous["inventory"]["observed"]}
    now = {label.casefold(): label for label in observed}
    return {"baseline_version": previous["app_version"], "same_version": False,
            "added": sorted(now[name] for name in now.keys() - before.keys()),
            "removed": sorted(before[name] for name in before.keys() - now.keys())}


def prior_verified(data, run, facing, mode):
    """Newest verification of `mode` under another app version of the same model and facing."""
    found = [(entry["modes"][mode]["verified"]["at"], entry["app_version"]) for entry in data["entries"].values()
             if entry["package"] == run["package"] and entry.get("model") == run["model"] and entry.get("facing") == facing
             and entry.get("app_version") != run["version"] and entry.get("modes", {}).get(mode, {}).get("verified")]
    return {"app_version": max(found)[1], "at": max(found)[0]} if found else None


def apply_facing(data, run, facing, block):
    key = entry_key(run["model"], run["version"], facing, run["package"])
    entry = data["entries"].get(key)
    report = {"key": key, "first_run": entry is None, "comparison": None, "new_modes": [], "missing_modes": [],
              "verified_before": {}, "verified_elsewhere": {}}
    inventory = block.get("inventory")
    # Only a complete fresh UI scan can replace the inventory from which later runs are planned.
    if inventory and (inventory.get("source") == "registry" or inventory.get("warnings") or not inventory["observed"]):
        inventory = None
    if inventory is not None:
        previous = baseline(data, run, facing, key)
        if previous is not None:
            report["comparison"] = compare_inventory(previous, inventory["observed"])
            report["comparison"]["same_version"] = previous["app_version"] == run["version"]
    if entry is None:
        entry = {"model": run["model"], "package": run["package"], "app_version": run["version"], "facing": facing,
                 "first_seen": run["at"], "profile_verified": run["version_verified"],
                 "inventory": None, "unrecognized": [], "modes": {}, "history": []}
        data["entries"][key] = entry
    modes = entry["modes"]
    report["verified_before"] = {name: mode["verified"] for name, mode in modes.items() if mode.get("verified")}
    if inventory is not None:
        known = bool(modes)
        report["new_modes"] = [name for name in inventory["recognized"] if known and name not in modes]
        report["missing_modes"] = [name for name in report["verified_before"] if name not in inventory["recognized"]]
        entry["inventory"] = {"observed": inventory["observed"], "run_id": run["run_id"], "at": run["at"]}
        entry["unrecognized"] = inventory["unrecognized"]
    for step in block["steps"]:
        mode = step.get("mode")
        if mode is None or (step["status"] in NOT_RECORDED and not block.get("requested_mode")):
            continue
        record = modes.setdefault(mode, {"label": step.get("label"), "kind": step.get("kind")})
        record["last"] = {"run_id": run["run_id"], "at": run["at"], "status": step["status"],
                          "error_code": step.get("error_code")}
        if step["status"] == "OPENED" or step.get("mode_opened") or (step["status"] == "PASS" and step.get("evidence")):
            record["opened"] = {"run_id": run["run_id"], "at": run["at"]}
        if step["status"] == "PASS" and step.get("evidence"):
            record["verified"] = {"run_id": run["run_id"], "at": run["at"], "evidence": step["evidence"],
                                  "facing_checks": step.get("facing_checks"),
                                  "profile_verified": run["version_verified"]}
    for name, mode in modes.items():
        if mode.get("verified") is None and (other := prior_verified(data, run, facing, name)):
            report["verified_elsewhere"][name] = other
    entry["updated"] = run["at"]
    entry["history"] = (entry["history"] + [{
        "run_id": run["run_id"], "at": run["at"], "status": run["status"],
        "steps": {step.get("mode") or step["step"]: step["status"] for step in block["steps"]},
        "comparison": report["comparison"]}])[-HISTORY_LIMIT:]
    return report


def update(path, run):
    """Apply one run to the registry under a file lock. `run` carries model, version, facings and their steps."""
    lock_key = f"registry:{Path(path).resolve()}"
    for attempt in range(LOCK_TRIES):
        try:
            with device_lock(lock_key):
                data = load(path)
                reports = {facing: apply_facing(data, run, facing, block) for facing, block in run["facings"].items()}
                data["schema_version"] = SCHEMA_VERSION
                write_json_atomic(path, data)
                return reports
        except RunError as exc:
            if exc.code != "DEVICE_BUSY":
                raise
            if attempt == LOCK_TRIES - 1:
                raise RunError("REGISTRY_BUSY", "Another camera-test process is updating the registry.") from exc
        time.sleep(0.2)
