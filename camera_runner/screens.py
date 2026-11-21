"""Normalized UI snapshots, golden sets and drift comparison. Nothing here uses ADB."""

from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
from xml.etree import ElementTree

from .adb import RunError


SCHEMA_VERSION = 1
MANIFEST = "golden.json"
SEVERITY_ORDER = ("none", "cosmetic", "breaking")


def normalize(xml_text, package):
    """Keep only app-package nodes with an id or a label, and only stable attributes (no bounds)."""
    prefix = f"{package}:id/"
    nodes = []
    for node in ElementTree.fromstring(xml_text).iter("node"):
        if node.get("package") != package:
            continue
        resource_id = node.get("resource-id", "")
        entry = {
            "resource-id": resource_id[len(prefix):] if resource_id.startswith(prefix) else resource_id,
            "text": node.get("text", "").strip(),
            "content-desc": node.get("content-desc", "").strip(),
            "class": node.get("class", ""),
        }
        if entry["resource-id"] or entry["text"] or entry["content-desc"]:
            nodes.append(entry)
    return nodes


def snapshot_sink(directory, package, saved):
    """Return a Device checkpoint sink that writes `<directory>/<name>.json` and records the name."""
    def sink(name, xml_text):
        try:
            data = {"schema_version": SCHEMA_VERSION, "checkpoint": name, "package": package,
                    "nodes": normalize(xml_text, package)}
            directory.mkdir(parents=True, exist_ok=True)
            text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
            (directory / f"{name}.json").write_text(text, encoding="utf-8")
        except (OSError, ElementTree.ParseError):
            return  # A snapshot problem must not fail the run.
        if name not in saved:
            saved.append(name)
    return sink


def read_json(path, code, what):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RunError(code, f"{what} could not be read: {exc}") from exc
    if not isinstance(data, dict):
        raise RunError(code, f"{what} must be a JSON object.")
    return data


def read_snapshots(directory):
    """Map checkpoint name to node list for every snapshot file in a directory."""
    snapshots = {}
    for path in sorted(Path(directory).glob("*.json")):
        if path.name == MANIFEST:
            continue
        data = read_json(path, "INVALID_SCREENS", f"Snapshot {path.name}")
        if not isinstance(data.get("nodes"), list):
            raise RunError("INVALID_SCREENS", f"Snapshot {path.name} has no node list.")
        snapshots[path.stem] = data["nodes"]
    return snapshots


def load_golden(directory, scenario=None):
    manifest = read_json(Path(directory) / MANIFEST, "INVALID_GOLDEN", "Golden manifest")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise RunError("INVALID_GOLDEN", f"Golden schema_version must be {SCHEMA_VERSION}.")
    if scenario is not None and manifest.get("scenario") != scenario:
        raise RunError("INVALID_GOLDEN", f"Golden set is for {manifest.get('scenario')}, not {scenario}.")
    snapshots = read_snapshots(directory)
    if not snapshots:
        raise RunError("INVALID_GOLDEN", "Golden set has no snapshot files.")
    return manifest, snapshots


def promote(run_dir, output, force=False):
    """Create a golden set from a passing run that captured screens."""
    run_dir, output = Path(run_dir), Path(output)
    result = read_json(run_dir / "result.json", "INVALID_RUN", "Run result")
    if result.get("scenario_status") != "PASS" or "error" in result:
        raise RunError("RUN_NOT_PASSED", "Only a run that PASSED can become a golden set.")
    screens = run_dir / "screens"
    snapshots = read_snapshots(screens) if screens.is_dir() else {}
    if not snapshots:
        raise RunError("SCREENS_NOT_CAPTURED", "The run has no screens. Rerun with --capture-screens.")
    if (output / MANIFEST).exists() and not force:
        raise RunError("GOLDEN_EXISTS", f"{output} already holds a golden set. Pass --force to replace it.")
    try:
        output.mkdir(parents=True, exist_ok=True)
        for name in snapshots:
            shutil.copyfile(screens / f"{name}.json", output / f"{name}.json")
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "scenario": result.get("scenario"),
            "app_version": result.get("device", {}).get("camera_version"),
            "profile_source": result.get("profile", {}).get("source"),
            "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "checkpoints": sorted(snapshots),
        }
        (output / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        raise RunError("IO_ERROR", str(exc)[:240]) from exc
    return manifest


def _ids(nodes):
    return {node["resource-id"] for node in nodes if node.get("resource-id")}


def _labels(nodes):
    """Resource id -> every non-empty text and content-desc carried by nodes with that id."""
    labels = {}
    for node in nodes:
        if node.get("resource-id"):
            values = labels.setdefault(node["resource-id"], set())
            values.update(value for key in ("text", "content-desc") if (value := node.get(key)))
    return labels


def compare_checkpoint(profile, golden, now):
    """Differences between two node lists, with a severity judged against the profile."""
    golden_ids, now_ids = _ids(golden), _ids(now)
    golden_labels, now_labels = _labels(golden), _labels(now)
    profile_labels = {label.strip().casefold() for values in profile.labels.values() for label in values}
    changed = [
        {"id": key, "golden": sorted(golden_labels[key]), "now": sorted(now_labels[key])}
        for key in sorted(golden_ids & now_ids) if golden_labels[key] != now_labels[key]
    ]
    selectors_missing = [
        {"key": key, "selector": selector}
        for key, selector in profile.selectors.items() if selector in golden_ids and selector not in now_ids
    ]
    label_changed = any(label.casefold() in profile_labels for item in changed for label in item["golden"])
    missing, new = sorted(golden_ids - now_ids), sorted(now_ids - golden_ids)
    if selectors_missing or label_changed:
        severity = "breaking"
    elif missing or new or changed:
        severity = "cosmetic"
    else:
        severity = "none"
    return {"severity": severity, "missing_ids": missing, "new_ids": new, "changed_labels": changed,
            "profile_selectors_missing": selectors_missing}


def compare(profile, golden, now):
    """Compare snapshot sets by checkpoint. One the run did not reach (other start state, or a failure the verdict reports) is `not_reached`."""
    checkpoints = {}
    for name, nodes in golden.items():
        checkpoints[name] = (compare_checkpoint(profile, nodes, now[name]) if name in now
                             else {"severity": "none", "not_reached": True})
    severity = max((item["severity"] for item in checkpoints.values()), key=SEVERITY_ORDER.index, default="none")
    return {"severity": severity, "checkpoints": checkpoints, "extra_checkpoints": sorted(set(now) - set(golden))}
