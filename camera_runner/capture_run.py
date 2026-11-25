"""Run camera capture steps, collect their evidence and report recoverable failures."""

from contextlib import contextmanager
import sys
from uuid import uuid4

from . import device_registry, media, preflight
from .adb import RunError, device_lock, select_device
from .apps import app_option
from .capture import CaptureSession
from .failures import failure_class
from .guidance import PYZ, next_action
from .preview import PreviewDevice


FACING_NAMES = {"rear": "후면", "front": "전면"}

STATUS_BY_CLASS = {"environment": "ENVIRONMENT_ERROR", "automation": "AUTOMATION_ERROR", "product": "FAIL"}
STATUS_TEXT = {"PASS": "통과", "OPENED": "모드 열림", "FAIL": "실패(앱 동작)", "AUTOMATION_ERROR": "자동화 오류", "ENVIRONMENT_ERROR": "환경 문제",
               "MANUAL": "사람이 직접 필요", "UNKNOWN_MODE": "인식하지 못한 모드", "EXTERNAL_APP": "별도 앱",
               "SKIPPED": "건너뜀", "NOT_OBSERVED": "이번 인벤토리에 없음", "ENVIRONMENT_BLOCKED": "환경 문제로 중단",
               "INCOMPLETE": "필수 흐름 미완료", "UNVERIFIED_FACING": "이 방향으로 검증할 수 없음"}
STEP_NAMES = {"start_rear": "후면으로 시작", "switch_front": "전면으로 전환", "return_rear": "후면으로 복귀",
              "restore_rear_photo": "후면 사진 모드로 정리"}


def check_media(clip, seconds, time_shifted, result):
    """Track and frame checks of media.verdict. Slow motion and hyperlapse skip the length check."""
    try:
        info = media.inspect(clip)
    except media.MediaError as exc:
        result["media"] = {"error": exc.code}
        raise RunError(exc.code, str(exc)) from exc
    failures, warnings = media.verdict(info, seconds)
    if time_shifted:
        failures = [item for item in failures if item[0] != "MEDIA_DURATION"]
    result["media"] = {**info, "checks": media.check_summary(failures, warnings)}
    if time_shifted:
        result["media"]["checks"]["duration"] = "SKIPPED"
    result.setdefault("warnings", []).extend(code for code, _ in warnings)
    if failures:
        raise RunError(*failures[0])
    return info


class CaptureRun:
    def __init__(self, device, profile, out, seconds, tools, app):
        self.device, self.profile, self.out, self.seconds, self.tools = device, profile, out, seconds, tools
        self.steps = []
        self.app = app
        self.session = CaptureSession(device, profile, app)
        self.abort = None

    def note(self, step_id, facing, mode, status, mandatory=False, **meta):
        record = {"step": step_id, "facing": facing, "mode": mode, "mandatory": mandatory, "status": status, **meta}
        if mode:
            record["mode_name"] = self.app.MODE_NAMES[mode]
        self.steps.append(record)
        return record

    def step(self, step_id, facing, mode, work, mandatory=False, blocked=None, **meta):
        """Run `work(result, folder)` and record its outcome. Returns True on PASS."""
        reason = self.abort or blocked
        if reason:
            self.note(step_id, facing, mode, "SKIPPED", mandatory, reason=reason, **meta)
            return False
        folder = self.out / step_id
        folder.mkdir(parents=True, exist_ok=True)
        result = {"steps": [], "artifacts": {}}
        record = self.note(step_id, facing, mode, "PASS", mandatory, **meta)
        print(f"[카메라 테스트] {step_title(record)} 시작", file=sys.stderr, flush=True)
        try:
            record.update(work(result, folder) or {})
            record["artifacts"] = result["artifacts"] or None
            return True
        except RunError as exc:
            if exc.code == "FACING_MISMATCH" and mode and not mandatory and result.get("mode_switched_facing"):
                record.update(status="UNVERIFIED_FACING", facing_checks=result["facing_checks"])
            else:
                code = "SUITE_UI_UNAVAILABLE" if exc.code == "UI_UNAVAILABLE" else exc.code
                self.fail(record, code, str(exc), result, folder)
                if code == "SUITE_UI_UNAVAILABLE" and result.get("can_exit_mode") and not self.abort:
                    self.recover_preview(record)
        except KeyboardInterrupt:
            self.fail(record, "INTERRUPTED", "Camera test was interrupted.", result, folder)
        finally:
            print(f"[카메라 테스트] {step_title(record)} · {STATUS_TEXT[record['status']]}", file=sys.stderr, flush=True)
        return False

    def recover_preview(self, record):
        """Leave a preview this run opened, without touching another app or an uncertain recording."""
        try:
            if not self.device.package_foreground(self.profile.package):
                return
            preflight.check(self.device, self.profile.package)
            self.device.shell("input", "keyevent", "KEYCODE_BACK")
            foreground = preflight.foreground_state(self.device, self.profile.package)
            if foreground["package"] and foreground["package"] == foreground["launcher"]:
                self.app.launch_camera(self.device, self.profile)
            record["recovery"] = "BACK_SENT"
        except RunError as exc:
            record["recovery"] = exc.code
            if failure_class(exc.code) == "environment":
                self.abort = "ENVIRONMENT"

    def fail(self, record, code, message, result, folder):
        cls = failure_class(code)
        record.update(status=STATUS_BY_CLASS.get(cls, "AUTOMATION_ERROR"), error_code=code, failure_class=cls,
                      message=message[:240])
        record.pop("evidence", None)
        if result.get("facing_checks"):
            record["facing_checks"] = result["facing_checks"]
        if result.get("mode_opened"):
            record["mode_opened"] = True
        if result.get("evidence_discarded"):
            record["evidence_discarded"] = True
        if cls == "environment":
            self.abort = "ENVIRONMENT"
        if result.get("recording_may_be_active"):
            record["recording_may_be_active"] = True
            self.abort = "RECORDING_MAY_BE_ACTIVE"  # No more taps while a recording may still run.
        if self.device.last_ui is not None:
            try:
                (folder / "failure-ui.xml").write_text(self.device.last_ui, encoding="utf-8")
                result["artifacts"]["failure_ui"] = str(folder / "failure-ui.xml")
            except OSError:
                pass
        if cls != "environment":
            self.tools.capture_failure(self.device, self.profile.package, folder, result)
        record["artifacts"] = result["artifacts"] or None
        record["next_action"] = next_action(code, {"artifacts": result["artifacts"],
                                                   "recording_may_be_active": record.get("recording_may_be_active")})
        if code in {"MODE_NOT_FOUND", "MODE_NOT_SELECTED", "MODE_NOT_READY", "INVALID_UI_BOUNDS"}:
            record["next_action"] = (
                f"{step_title(record)} 단계에서 화면을 확인하지 못했습니다. 해당 단계 폴더의 화면 자료와 "
                f"suite.json을 확인하고, 모드 배치가 바뀌었으면 `{PYZ} suite --rescan{app_option(self.profile.package)}`으로 다시 검사합니다.")

    def capture(self, facing, key, result, folder, mandatory, *, preserve_mode=False, open_only=False):
        self.session.capture(key, self.seconds, result, facing=facing, mandatory=mandatory,
                             preserve_mode=preserve_mode, open_only=open_only)
        checks = result["facing_checks"]
        if open_only:
            return {"facing_checks": checks}
        if "image" in result:
            evidence = {"kind": "image", "facing": facing, **result["image"]}
        else:
            evidence = {"kind": "video", "facing": facing, **result["video"]}
            clip = folder / f"{facing}-{key}.mp4"
            self.tools.pull_video(self.device, clip, result)
            info = check_media(clip, self.seconds, key in self.app.TIME_SHIFTED, result)
            result["artifacts"]["video"] = str(clip)
            evidence.update(duration_s=info["duration_s"], local_path=str(clip), tracks=info["tracks"],
                            checks=result["media"]["checks"])
        return {"evidence": evidence, "facing_checks": checks, "metrics": result.get("metrics"),
                "warnings": result.get("warnings")}

def exit_code(status):
    return {"PASS": 0, "ENVIRONMENT_BLOCKED": 3}.get(status, 1)


def step_title(step):
    if step["mode"]:
        return f"{FACING_NAMES[step['facing']]} {step.get('mode_name', step['mode'])}"
    if step["step"] in STEP_NAMES:
        return STEP_NAMES[step["step"]]
    if step["step"].startswith("inventory_"):
        return f"{FACING_NAMES[step['facing']]} 모드 목록 확인"
    return f"{FACING_NAMES[step['facing']]} 모드 '{step.get('label')}'"


def step_detail(step):
    status = step["status"]
    if status == "PASS":
        evidence, checks = step.get("evidence"), step.get("facing_checks") or {}
        parts = []
        if evidence and evidence["kind"] == "image":
            parts.append(f"사진 파일 {len(evidence['device_paths'])}개 {evidence['bytes']}바이트")
        elif evidence:
            parts.append(f"영상 {evidence['duration_s']}초 {evidence['bytes']}바이트")
        if step.get("observed_modes") is not None:
            name = "등록 목록" if step.get("source") == "registry" else "관찰한 모드"
            parts.append(f"{name} · " + ", ".join(step["observed_modes"]))
        shown = [stage for stage in ("after_mode_select", "before_shutter", "after_capture") if stage in checks]
        if shown:
            parts.append(f"방향 확인 {len(shown)}회 모두 {FACING_NAMES[step['facing']]}")
        elif checks.get("after"):
            parts.append(f"방향 확인: {FACING_NAMES.get(checks['after']['facing'], '알 수 없음')}")
        return ", ".join(parts)
    if status in ("MANUAL", "EXTERNAL_APP", "UNKNOWN_MODE"):
        return {"MANUAL": "사람이 직접 촬영해야 하는 모드라 누르지 않았습니다.",
                "EXTERNAL_APP": "별도 앱이라 설치하거나 열지 않았습니다.",
                "UNKNOWN_MODE": f"'{step.get('label')}'는 카탈로그에 없는 모드라 촬영하지 않았습니다."}[status]
    if status == "SKIPPED":
        return f"이전 단계 문제로 건너뜀({step.get('reason')})"
    if status == "NOT_OBSERVED":
        return "모드 목록에 없어 누르지 않았습니다."
    if status == "UNVERIFIED_FACING":
        actual = step["facing_checks"]["after_mode_select"]["facing"]
        opening = f"모드를 열자 {FACING_NAMES[actual]} 카메라가 켜졌습니다."
        if step["facing_checks"].get("before_mode_switch"):
            return f"{opening} 전환을 시도했으나 해당 방향의 같은 모드를 확인하지 못했습니다."
        return f"{opening} 해당 모드에서 방향을 바꿀 수 있는 버튼을 확인하지 못했습니다."
    text = f"{step['error_code']}: {step['message']}"
    if step.get("evidence_discarded"):
        text += " 이 촬영은 증거로 등록하지 않았습니다."
    return text


@contextmanager
def prepared_capture(args, app):
    """Hold the device lock while running preflight and using the live Camera UI."""
    profile = app.profile
    serial = select_device(args.device)
    run_id = uuid4().hex[:12]
    device = PreviewDevice(serial, run_id)
    hardware = device.shell("getprop", "ro.serialno")
    registry_path = device_registry.device_path(hardware) if args.register_device else None
    with device_lock(hardware or serial), device:
        info = device.metadata()
        version = info["camera_version"] = device.package_version(profile.package)
        if version is None:
            raise RunError("CAMERA_APP_NOT_FOUND", f"{app.NAME} ({profile.package}) is not installed.")
        verified_version = profile.verifies(version)
        warnings = [] if verified_version else ["UNVERIFIED_CAMERA_VERSION"]
        measured = preflight.check(device, profile.package)
        out = args.output_dir.resolve() / run_id
        out.mkdir(parents=True)
        version_label = "" if verified_version else " · 내장 프로필 검증 목록에 없는 버전"
        print(f"[카메라 테스트] {info.get('model')} · {app.NAME} {version}{version_label}", file=sys.stderr, flush=True)
        yield device, profile, out, info, measured, warnings, registry_path
