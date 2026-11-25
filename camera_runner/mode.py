"""Resolve one catalog mode and verify a single capture on the requested camera."""

from datetime import datetime, timezone

from . import device_registry as registry
from . import capture_run
from .apps import APPS, app_option, classify
from .json_files import write_json_atomic
from .adb import Device, RunError, select_device
from .failures import failure_class
from .guidance import PYZ, error_object, next_action


def normalized(name):
    return " ".join(name.casefold().replace("-", " ").replace("_", " ").split())


def list_modes(app):
    return [
        {"id": key, "name": app.MODE_NAMES[key], "kind": entry["kind"],
         "aliases": sorted({key, key.replace("_", "-"), key.replace("_", " "), app.MODE_NAMES[key], *entry["labels"]})}
        for key, entry in app.CATALOG.items()
    ]


def run_mode(args, tool_version, tools):
    app = APPS[args.app]
    catalog = list_modes(app)
    result = {"command": "mode", "tool_version": tool_version, "package": app.profile.package}
    if args.list:
        return {**result, "status": "LISTED", "modes": catalog}, 0

    requested = normalized(args.name)
    entry = next((item for item in catalog if any(normalized(alias) == requested for alias in item["aliases"])), None)
    result.update(requested_mode=args.name, facing=args.facing, mode=entry["id"] if entry else None, open_only=args.open_only,
                  recording_may_be_active=False)
    if entry is None:
        return {**result, "status": "UNKNOWN_MODE",
                "next_action": f"`{PYZ} mode --list{app_option(app.profile.package)}`에서 모드 이름을 확인합니다."}, 1
    result.update(name=entry["name"], kind=entry["kind"])
    if entry["kind"] == "manual":
        return {**result, "status": "MANUAL", "next_action": "카메라 앱에서 해당 모드를 열어 직접 촬영합니다."}, 1
    if entry["kind"] == "external":
        return {**result, "status": "EXTERNAL_APP", "next_action": "해당 앱의 별도 검증 절차를 사용합니다."}, 1

    try:
        return capture(args, result, tools, app)
    except (RunError, OSError) as exc:
        code = getattr(exc, "code", "IO_ERROR")
        status = "ENVIRONMENT_BLOCKED" if failure_class(code) == "environment" else "AUTOMATION_ERROR"
        error = error_object(code, str(exc)[:240])
        return {**result, "status": status, "error": error, "next_action": error["next_action"]}, capture_run.exit_code(status)


def capture(args, result, tools, app):
    facing, key = args.facing, result["mode"]
    with capture_run.prepared_capture(args, app) as (device, profile, out, info, measured, warnings, device_registry):
        if device_registry:
            registry.load(device_registry)
        runner = capture_run.CaptureRun(device, profile, out, args.duration, tools, app)

        def work(evidence, folder):
            if measured["foreground"]["package"] != profile.package:
                app.launch_camera(device, profile)
            return runner.capture(facing, key, evidence, folder, mandatory=False, preserve_mode=True, open_only=args.open_only)

        runner.step(f"{facing}_{key}", facing, key, work, kind=result["kind"])
        step, = runner.steps
        status = step["status"]
        if status == "PASS" and args.open_only:
            status = step["status"] = "OPENED"
        if status == "ENVIRONMENT_ERROR":
            status = "ENVIRONMENT_BLOCKED"
        if step.get("error_code") == "MODE_NOT_FOUND":
            status = step["status"] = "NOT_OBSERVED"
        action = step.get("next_action")
        if step.get("error_code") in {"MODE_NOT_FOUND", "MODE_NOT_SELECTED", "MODE_NOT_READY", "INVALID_UI_BOUNDS"}:
            action = step["next_action"] = "단말의 카메라 화면에서 해당 방향의 모드를 확인하고 실행 폴더의 화면 자료를 봅니다."
        if status == "UNVERIFIED_FACING":
            action = "단말에서 해당 모드가 요청한 카메라 방향을 지원하는지 확인합니다."
        result.update(run_id=out.name, finished=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                      status=status, device=info, profile=profile.summary(info["camera_version"]),
                      preflight=measured, requested_seconds=args.duration if result["kind"] == "video" and not args.open_only else None,
                      step=step, ui_backends=sorted(device.ui_backends),
                      warnings=sorted({*warnings, *(step.get("warnings") or [])}) or None,
                      recording_may_be_active=bool(step.get("recording_may_be_active")),
                      next_action=action or "조치가 필요 없습니다.",
                      artifacts={"mode": str(out / "mode.json")})
        if device_registry:
            result["registry"] = {"path": str(device_registry), "updated": False}
            run = {"run_id": out.name, "at": result["finished"], "model": info.get("model") or "unknown-model",
                   "package": profile.package, "version": info["camera_version"], "version_verified": profile.verifies(info["camera_version"]),
                   "status": status, "facings": {facing: {"steps": [step], "requested_mode": True}}}
            try:
                registry.update(device_registry, run)
                result["registry"]["updated"] = True
            except (RunError, OSError) as exc:
                code = getattr(exc, "code", "IO_ERROR")
                result["registry"]["error"] = code
                result["next_action"] = " ".join(filter(None, (action, next_action(code))))
        write_json_atomic(out / "mode.json", result)
    code = 0 if status == "OPENED" else capture_run.exit_code(status)
    if code == 0 and result.get("registry", {}).get("error"):
        code = 1
    return result, code


def capabilities(serial, app):
    device = Device(select_device(serial), "capabilities")
    path = registry.device_path(device.shell("getprop", "ro.serialno"))
    model = device.metadata().get("model") or "unknown-model"
    version = device.package_version(app.profile.package)
    if not version:
        raise RunError("CAMERA_APP_NOT_FOUND", f"{app.NAME} ({app.profile.package}) is not installed.")
    entries = registry.load(path)["entries"]
    facings = {}
    for facing in capture_run.FACING_NAMES:
        entry = entries.get(registry.entry_key(model, version, facing, app.profile.package), {})
        labels = (entry.get("inventory") or {}).get("observed", [])
        observed, unknown = classify(labels, app)
        modes = {}
        for key in app.CATALOG:
            record = entry.get("modes", {}).get(key, {})
            verified = record.get("verified")
            opened = record.get("opened") or verified
            modes[key] = {"observed": key in observed, "opened_run": opened.get("run_id") if opened else None,
                          "capture_run": verified.get("run_id") if verified else None, "last": record.get("last")}
        facings[facing] = {"modes": modes, "unrecognized": unknown}
    return {"model": model, "package": app.profile.package, "app_version": version, "registry": str(path), "facings": facings}


def render_capabilities(result, app):
    lines = [f"{result['model']} · 카메라 {result['app_version']}"]
    for facing, block in result["facings"].items():
        lines.append(capture_run.FACING_NAMES[facing])
        for key, item in block["modes"].items():
            evidence = ("촬영·저장 확인" if item["capture_run"] else "진입 확인" if item["opened_run"]
                        else "목록에서 관찰" if item["observed"] else "확인 기록 없음")
            last = item["last"]
            suffix = f" · 최근 {capture_run.STATUS_TEXT.get(last['status'], last['status'])}" if last else ""
            lines.append(f"  {app.MODE_NAMES[key]} · {evidence}{suffix}")
        lines.extend(f"  {label} · 목록에서 관찰 (미등록 모드)" for label in block["unrecognized"])
    return "\n".join(lines)


def render(result):
    if result["status"] == "LISTED":
        return "\n".join(f"{entry['id']} · {entry['name']} · {entry['kind']} · {', '.join(entry['aliases'])}"
                         for entry in result["modes"])
    facing = capture_run.FACING_NAMES[result["facing"]]
    name = result.get("name", result["requested_mode"])
    status = capture_run.STATUS_TEXT[result["status"]]
    lines = [f"{facing} {name} · {status}"]
    if step := result.get("step"):
        if step["status"] == "OPENED":
            shown = step["facing_checks"]["after_mode_select"]
            lines.append(f"화면에서 확인한 모드 {shown['mode']} · {capture_run.FACING_NAMES[shown['facing']]}")
        else:
            lines.append(capture_run.step_detail(step) if step["status"] != "NOT_OBSERVED" else step["message"])
    elif error := result.get("error"):
        lines.append(f"{error['code']} · {error['message']}")
    if result.get("warnings"):
        lines.append("경고 · " + ", ".join(result["warnings"]))
    if result.get("artifacts"):
        lines.append(result["artifacts"]["mode"])
    lines.append(result["next_action"])
    return "\n".join(lines)
