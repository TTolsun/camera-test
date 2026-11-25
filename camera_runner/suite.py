"""Discover modes and verify captures on both cameras of the selected handset."""

from datetime import datetime, timezone

from . import device_registry as registry
from .adb import RunError
from .android_ui import wait_for
from .apps import APPS, app_option, classify
from .capture_run import (CaptureRun, FACING_NAMES, STATUS_TEXT, exit_code, prepared_capture,
                          step_detail, step_title)
from .guidance import PYZ, next_action
from .json_files import write_json_atomic


class Suite(CaptureRun):
    def __init__(self, device, profile, out, seconds, tools, app):
        super().__init__(device, profile, out, seconds, tools, app)
        self.inventories = {}
        self.attempted = set()
        self.saved_inventories = {}

    def inventory(self, facing, blocked=None):
        def work(result, folder):
            checks = result["facing_checks"] = {}
            self.session.confirm(self.device.ui(), facing, "before_inventory", checks)
            saved = self.saved_inventories.get(facing)
            if saved:
                observed = saved["observed"]
                metadata = {"source": "registry", "registered_run_id": saved["run_id"]}
            else:
                info, _ = self.app.scan_modes(self.device, self.profile, launch=False, allow_partial=True)
                observed = info["observed_modes"]
                metadata = {"source": "scan", "mode_strip": info["mode_strip"], "more_menu": info["more_menu"],
                            "warnings": info["warnings"]}
            recognized, unrecognized = classify(observed, self.app)
            self.inventories[facing] = {"observed": observed, "recognized": recognized,
                                        "unrecognized": unrecognized, **metadata}
            if saved is None:
                try:
                    result["can_exit_mode"] = True
                    self.app.select_mode(self.device, self.profile, "photo")
                    self.session.confirm(wait_for(self.session.photo_screen, 8, "MODE_NOT_SELECTED",
                                          "The Camera did not return to Photo after scanning."), facing, "after_scan", checks)
                except RunError:
                    metadata["warnings"].append("SCAN_STATE_UNVERIFIED")
                    raise
            return {"observed_modes": observed, "facing_checks": checks, **metadata}

        if self.step(f"inventory_{facing}", facing, None, work, blocked=blocked):
            for key, label in self.inventories[facing]["recognized"].items():
                kind = self.app.CATALOG[key]["kind"]
                if kind in ("manual", "external"):
                    self.note(f"{facing}_{key}", facing, key, "MANUAL" if kind == "manual" else "EXTERNAL_APP",
                              label=label, kind=kind)
            for label in self.inventories[facing]["unrecognized"]:
                self.note(f"{facing}_unknown_{len(self.steps)}", facing, None, "UNKNOWN_MODE", label=label)

    def capture_step(self, facing, key, mandatory=False, blocked=None):
        self.attempted.add((facing, key))
        inventory = self.inventories.get(facing)
        meta = {"label": (inventory or {}).get("recognized", {}).get(key), "kind": self.app.CATALOG[key]["kind"]}
        if not mandatory and inventory is not None and key not in inventory["recognized"] and not (self.abort or blocked):
            self.note(f"{facing}_{key}", facing, key, "NOT_OBSERVED", mandatory, **meta)
            return
        return self.step(f"{facing}_{key}", facing, key,
                         lambda result, folder: self.capture(facing, key, result, folder, mandatory),
                         mandatory, blocked, **meta)

    def extras(self, facing):
        inventory = self.inventories.get(facing)
        for key in self.app.CATALOG:
            if inventory and key in inventory["recognized"] and self.app.CATALOG[key]["kind"] in ("photo", "video") \
                    and (facing, key) not in self.attempted:
                self.capture_step(facing, key)

    def run(self):
        def start(result, folder):
            self.app.launch_camera(self.device, self.profile)
            return self.session.switch_to("rear", result)

        rear = self.step("start_rear", "rear", None, start, mandatory=True)
        gate = None if rear else "REAR_NOT_VERIFIED"
        self.inventory("rear", gate)
        self.capture_step("rear", "photo", True, gate)
        front = self.step("switch_front", "front", None,
                          lambda result, folder: self.session.switch_to("front", result, require_change=True),
                          mandatory=True, blocked=gate)
        gate_front = None if front else "FRONT_NOT_REACHED"
        self.inventory("front", gate_front)
        self.capture_step("front", "photo", True, gate_front)
        self.capture_step("front", "video", True, gate_front)
        back = self.step("return_rear", "rear", None,
                         lambda result, folder: self.session.switch_to("rear", result, require_change=True),
                         mandatory=True, blocked=gate_front)
        if back:
            self.extras("rear")
            self.extras("front")
        mandatory_captures = {("rear", "photo"), ("front", "photo"), ("front", "video")}
        if rear and not self.abort and (not back or self.attempted - mandatory_captures):
            self.step("restore_rear_photo", "rear", None, lambda result, folder: self.session.switch_to("rear", result))


def overall(steps):
    statuses = {step["status"] for step in steps}
    if "ENVIRONMENT_ERROR" in statuses:
        return "ENVIRONMENT_BLOCKED"
    for status in ("FAIL", "AUTOMATION_ERROR"):
        if status in statuses:
            return status
    return "INCOMPLETE" if any(step["mandatory"] and step["status"] != "PASS" for step in steps) else "PASS"


def first_action(result):
    """The next step of the most urgent failing step; otherwise what a person does with the finished run."""
    option = app_option(result["package"])
    steps = result["steps"]
    for status in ("ENVIRONMENT_ERROR", "FAIL", "AUTOMATION_ERROR"):
        found = next((step for step in steps if step["status"] == status), None)
        if found:
            return found["next_action"]
    if result.get("registry", {}).get("error"):
        return next_action(result["registry"]["error"])
    if result["status"] == "INCOMPLETE":
        skipped = next(step for step in steps if step["mandatory"] and step["status"] != "PASS")
        return (f"필수 단계 `{skipped['step']}`가 {STATUS_TEXT[skipped['status']]} 상태입니다. "
                "단말 카메라 화면에서 해당 모드가 실제로 있는지 확인하고 `report.md`의 인벤토리를 확인합니다.")
    manual = [step for step in steps if step["status"] in ("MANUAL", "UNKNOWN_MODE", "UNVERIFIED_FACING")]
    if manual:
        return ("자동으로 확인하지 못한 모드가 있습니다(파노라마는 사람이 직접 촬영하고, 인식하지 못한 모드는 "
                "카탈로그에 추가한 뒤 검증합니다). 목록은 report.md의 모드별 결과에 있습니다.")
    if any(item.get("warnings") for item in result["inventories"].values()):
        return f"모드 목록을 일부만 읽었습니다. 단말 화면을 확인한 뒤 `{PYZ} suite --rescan{option}`을 실행합니다."
    return f"조치가 필요 없습니다. 새 앱 빌드가 설치되면 `{PYZ} suite{option}`를 다시 실행해 등록부와 비교합니다."


def render(result):
    """Korean report for a person. report.md and the default stdout are this text."""
    device, lines = result["device"], []
    names = {step["mode"]: step["mode_name"] for step in result["steps"] if step.get("mode_name")}
    lines += [f"# 카메라 스위트 결과: {STATUS_TEXT[result['status']]}", "",
              f"- 단말 {device.get('model')}, {result['app_name']} {device.get('camera_version')} "
              f"(검증 목록 {'포함' if result['profile']['version_verified'] else '미포함'})",
              f"- 실행 {result['run_id']}, 등록부 {result['registry']['path']}"]
    if result.get("warnings"):
        labels = {"UNVERIFIED_CAMERA_VERSION": "내장 프로필의 검증 목록에 없는 카메라 앱 버전",
                  "MEDIA_NO_AUDIO": "일부 영상에 오디오 트랙이 없음"}
        lines.append("- 경고 · " + ", ".join(labels.get(code, code) for code in result["warnings"]))
    lines += ["", f"**다음 행동**: {result['next_action']}", "", "## 필수 흐름"]
    for number, step in enumerate((item for item in result["steps"] if item["mandatory"]), 1):
        lines.append(f"{number}. {step_title(step)}: {STATUS_TEXT[step['status']]}. {step_detail(step)}".rstrip())
    lines += ["", "## 모드별 결과"]
    registry_info = result["registry"].get("facings", {})
    for facing in ("rear", "front"):
        rows = [step for step in result["steps"] if step["facing"] == facing and not step["step"].startswith(
            ("start_", "switch_", "return_", "restore_", "inventory_"))]
        if not rows:
            continue
        lines.append(f"### {FACING_NAMES[facing]}")
        info = registry_info.get(facing, {})
        for step in rows:
            line = f"- {step_title(step)}: {STATUS_TEXT[step['status']]}. {step_detail(step)}"
            other = (info.get("verified_elsewhere") or {}).get(step["mode"])
            if step["status"] not in ("PASS", "MANUAL", "EXTERNAL_APP") and other:
                line += f" 이전 버전 {other['app_version']}에서는 검증된 모드입니다."
            lines.append(line.rstrip())
            if step.get("next_action"):
                lines.append(f"  - 다음: {step['next_action']}")
    restore = next((step for step in result["steps"] if step["step"] == "restore_rear_photo"), None)
    if restore is not None:
        lines.append(f"- {step_title(restore)} · {STATUS_TEXT[restore['status']]}. {step_detail(restore)}")
    lines += ["", "## 전·후면 모드 목록"]
    for facing, inventory in result["inventories"].items():
        source = "같은 앱 버전의 등록부" if inventory["source"] == "registry" else "이번 화면에서 확인"
        lines.append(f"- {FACING_NAMES[facing]} ({source}) · {', '.join(inventory['observed'])}")
        if inventory.get("warnings"):
            lines.append("  - 목록을 일부만 읽어 비교 기준으로 등록하지 않았습니다.")
    lines += ["", "## 등록부 비교"]
    if not registry_info:
        lines.append("- 방향을 확인한 카메라가 없어 등록부를 바꾸지 않았습니다.")
    for facing, info in registry_info.items():
        name = FACING_NAMES[facing]
        lines.append(f"- {name}: " + ("처음 등록합니다." if info["first_run"] else "기존 등록에 이번 결과를 더했습니다."))
        comparison = info.get("comparison")
        if comparison:
            base = ("같은 버전의 이전 실행" if comparison["same_version"] else f"이전 버전 {comparison['baseline_version']}")
            lines.append(f"  - {base}과 비교: 새 모드 {comparison['added'] or '없음'}, 사라진 모드 {comparison['removed'] or '없음'}")
        if info["new_modes"]:
            lines.append(f"  - 등록부에 없던 새 모드: {', '.join(names.get(mode, mode) for mode in info['new_modes'])}")
        if info["missing_modes"]:
            lines.append(f"  - 이전에 검증했지만 이번에 보이지 않는 모드: {', '.join(names.get(mode, mode) for mode in info['missing_modes'])}")
    if result["registry"].get("error"):
        lines.append(f"- 등록부를 갱신하지 못했습니다({result['registry']['error']}). 이번 증거는 실행 폴더에 있습니다.")
    lines += ["", f"- 전체 데이터: `{result['artifacts']['suite']}`", ""]
    return "\n".join(lines)


def run_suite(args, tool_version, tools):
    """Run the whole suite. Returns (result, exit code). Raises RunError before the phone is touched."""
    app = APPS[args.app]
    missing = [key for key in ("photo", "video") if app.CATALOG.get(key, {}).get("kind") != key]
    if missing:
        raise RunError("SUITE_MODE_UNSUPPORTED", f"{app.NAME} does not provide the required suite modes: {', '.join(missing)}.")
    registry_path = (args.registry or args.output_dir / "registry.json").resolve()
    saved_registry = None if args.register_device else registry.load(registry_path)
    with prepared_capture(args, app) as (device, profile, out, info, measured, warnings, device_registry):
        if device_registry:
            registry_path = device_registry
            saved_registry = registry.load(registry_path)
        run_id, version = out.name, info["camera_version"]
        verified_version = not warnings
        suite = Suite(device, profile, out, args.duration, tools, app)
        if not getattr(args, "rescan", False):
            for facing in FACING_NAMES:
                key = registry.entry_key(info.get("model") or "unknown-model", version, facing, profile.package)
                entry = saved_registry["entries"].get(key) or {}
                if entry.get("inventory"):
                    suite.saved_inventories[facing] = entry["inventory"]
        suite.run()
        status = overall(suite.steps)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        result = {"tool_version": tool_version, "run_id": run_id, "command": "suite", "app_name": app.NAME, "package": profile.package,
                  "finished": now,
                  "status": status, "device": info, "profile": profile.summary(version), "preflight": measured,
                  "requested_seconds": args.duration, "steps": suite.steps, "ui_backends": sorted(device.ui_backends),
                  "inventories": suite.inventories, "warnings": sorted({*warnings, *(
                      code for step in suite.steps for code in step.get("warnings") or [])}) or None,
                  "recording_may_be_active": any(step.get("recording_may_be_active") for step in suite.steps),
                  "registry": {"path": str(registry_path), "updated": False}, "artifacts": {}}
        run = {"run_id": run_id, "at": now, "model": info.get("model") or "unknown-model", "version": version,
               "package": profile.package, "version_verified": verified_version, "status": status,
               "facings": {facing: {"inventory": suite.inventories.get(facing),
                                    "steps": [step for step in suite.steps if step["facing"] == facing]}
                           for facing in sorted(suite.session.verified)}}
        if run["facings"]:
            try:
                result["registry"]["facings"] = registry.update(registry_path, run)
                result["registry"]["updated"] = True
            except (RunError, OSError) as exc:
                result["registry"]["error"] = getattr(exc, "code", "IO_ERROR")
        else:
            result["registry"]["reason"] = "FACING_NEVER_VERIFIED"
        result["next_action"] = first_action(result)
        result["artifacts"] = {"suite": str(out / "suite.json"), "report": str(out / "report.md")}
        write_json_atomic(out / "suite.json", result)
        (out / "report.md").write_text(render(result), encoding="utf-8")
    code = exit_code(status)
    if result["registry"].get("error") and code == 0:
        code = 1
    return result, code
