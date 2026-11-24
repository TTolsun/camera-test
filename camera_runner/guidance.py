"""Korean next-step guidance for every error code and decision the tool prints.

Error `message` strings stay English; `next_action` tells the person (or the AI agent relaying to
them) what to do next, with the exact command when there is one. Texts follow the troubleshooting
table and the verify-build decision table that used to live in the docs; the docs now point here.
"""

PYZ = "python camera-test.pyz"
DEFAULT_SCENARIO = "galaxy.portrait-video"
ADB_ENV_LINE = '$env:CAMERA_TEST_ADB = "C:\\Users\\<사용자>\\AppData\\Local\\Android\\Sdk\\platform-tools\\adb.exe"'


def _scenario(result):
    scenario = (result or {}).get("scenario")
    if isinstance(scenario, dict):
        scenario = scenario.get("name")
    return scenario if isinstance(scenario, str) else DEFAULT_SCENARIO


def _artifacts(result, *keys):
    """Paths of the named artifacts that exist in this result."""
    artifacts = (result or {}).get("artifacts") or {}
    return [artifacts[key] for key in keys if artifacts.get(key)]


def _attach(result, *pairs):
    """Sentence naming the evidence to attach: (artifact key, fallback name) pairs."""
    names = _artifacts(result, *(key for key, _ in pairs)) or [name for _, name in pairs]
    return "첨부할 파일: " + ", ".join(names) + "."


def _profile_info(result):
    """Profile summary from a run result (`profile`) or an inspect scenario status (`scenario.profile`)."""
    result = result or {}
    scenario = result.get("scenario")
    return (scenario.get("profile") if isinstance(scenario, dict) else result.get("profile")) or {}


def _unsupported_version(result):
    installed = ((result or {}).get("device") or {}).get("camera_version")
    verified = ", ".join(_profile_info(result).get("verified_versions") or []) or "없음"
    command = f"{PYZ} verify-build {_scenario(result)} --repeat 5"
    return (f"설치된 카메라 앱 {installed or '(버전 미확인)'}은 검증 목록({verified})에 없습니다. "
            f"새 빌드로 보고 `{command}`를 실행해 반복 검증하고, 나온 결정(decision)에 따라 진행합니다.")


def _busy(result):
    package = ((((result or {}).get("preflight") or {}).get("foreground")) or {}).get("package")
    who = f"앞 화면의 앱은 {package}입니다. " if package else ""
    return (who + "단말을 쓰는 사람이 있을 수 있으니 실행하지 말고 주인에게 알립니다. "
            "비어 있음을 확인했다면 홈 화면으로 나간 뒤 같은 명령을 다시 실행합니다.")


def _automation(result):
    """Automation failures: point at the selector candidates the diagnosis found."""
    scenario = _scenario(result)
    diagnosis = (result or {}).get("diagnosis") or {}
    absent = ((diagnosis.get("ui") or {}).get("absent")) or {}
    parts = []
    for key, item in list(absent.items())[:3]:
        similar = ", ".join(f"`{name}`" for name in item.get("similar", [])) or "후보 없음"
        parts.append(f"{key}(기대 `{item.get('expected')}`) → {similar}")
    text = "앱 UI가 바뀌어 자동화가 맞지 않습니다. "
    if parts:
        text += "diagnosis.ui.absent의 선택자 후보: " + "; ".join(parts) + ". 후보는 이름이 비슷할 뿐이므로 failure-ui.xml에서 실제 컨트롤인지 확인합니다. "
    else:
        text += "result.json의 diagnosis(phase, ui.absent, record_state_log)를 확인합니다. "
    stats = diagnosis.get("record_state_log") or {}
    if stats.get("lines", 0) > 0 and stats.get("matched", 0) == 0:
        text += "녹화 상태 로그는 있지만 문구가 프로필의 log.record_state.pattern과 맞지 않습니다. 패턴을 새 문구에 맞게 고칩니다. "
    return (text + f"`{PYZ} profile show {scenario} --output galaxy-camera.json`로 프로필을 내보내 고친 뒤 "
            f"`{PYZ} profile validate galaxy-camera.json`와 `{PYZ} verify-build {scenario} --profile-file galaxy-camera.json`로 확인합니다.")


def _product(result):
    return ("앱 동작 문제로 보고합니다. " + _attach(result, ("logcat", "logcat.txt"), ("failure_screen", "failure.png"),
                                                    ("video", "영상(있는 경우)")))


def _record_start(result):
    # A start that was never logged is automation when the log tag was seen but did not match.
    return _automation(result) if (result or {}).get("failure_class") == "automation" else _product(result)


def _media(result):
    return ("영상 구조나 길이가 잘못되었으므로 앱 결함으로 보고합니다. "
            + _attach(result, ("video", "영상(--verify-media가 남긴 MP4)"), ("logcat", "logcat.txt")))


GUIDANCE = {
    # Environment: the person at the phone or PC acts.
    "NO_DEVICE": f"ADB에 연결된 단말이 없습니다. `{PYZ} doctor`로 원인과 연결 명령을 확인합니다. 무선 디버깅은 포트가 바뀌었을 수 있으니 `adb connect <IP>:<포트>`를 다시 합니다.",
    "DEVICE_REQUIRED": "서로 다른 단말이 여러 대 연결되어 있습니다. `adb devices -l`로 일련번호를 확인하고 `--device <serial>`을 붙여 다시 실행합니다.",
    "DEVICE_UNAVAILABLE": f"지정한 단말이 준비 상태가 아닙니다. `adb devices -l`로 상태를 확인합니다. unauthorized는 단말의 USB 디버깅 허용 팝업을 승인하고, offline이나 목록에 없음은 다시 연결합니다. 원인은 `{PYZ} doctor --device <serial>`로 확인합니다.",
    "DEVICE_BUSY": "같은 단말에서 다른 camera-test가 실행 중입니다. 앞선 실행이 끝난 뒤 다시 실행합니다.",
    "DEVICE_CLOCK": "단말 시각을 읽지 못했습니다. 단말 화면이 켜져 있고 잠금이 풀렸는지 확인한 뒤 다시 실행합니다.",
    "ADB_NOT_FOUND": ("adb를 찾지 못했습니다. Android SDK Platform-Tools를 설치한 뒤, 설치된 adb.exe 경로를 PowerShell에서 지정합니다: "
                      f"`{ADB_ENV_LINE}`. 설정 후 `{PYZ} doctor`로 다시 확인합니다."),
    "ADB_TIMEOUT": f"ADB 응답이 없습니다. 단말 연결을 확인하고 `adb kill-server`로 ADB를 재시작한 뒤 `{PYZ} doctor`를 실행합니다.",
    "ADB_ERROR": f"ADB 명령이 실패했습니다. error.message의 내용을 확인하고 `{PYZ} doctor`로 연결 상태를 점검합니다.",
    "PRECHECK_SCREEN_OFF": "화면이 꺼져 있습니다. 화면을 켜고 잠금을 해제해 홈 화면이나 카메라를 띄운 뒤 다시 실행합니다. 실행이 끝날 때까지 화면이 꺼지지 않아야 합니다. 자동 실행용 랩 단말은 docs/operations.md의 '랩 단말 설정'대로 화면 켜짐 유지를 켭니다.",
    "PRECHECK_LOCKED": "잠금 화면입니다. 잠금을 해제해 홈 화면이나 카메라를 띄운 뒤 다시 실행합니다. 실행이 끝날 때까지 화면이 켜져 있어야 합니다.",
    "PRECHECK_LOW_STORAGE": "단말 여유 공간이 500MB 미만입니다. 단말의 DCIM/Camera를 정리합니다. 반복 실행에는 `--cleanup-video`를 씁니다.",
    "PRECHECK_DEVICE_BUSY": _busy,
    "CAMERA_NOT_FOREGROUND": "카메라 앱이 앞 화면에 없습니다. 단말의 잠금을 풀고 다른 앱을 닫은 뒤 다시 실행합니다.",
    "CAMERA_APP_NOT_FOUND": f"단말에 갤럭시 카메라 앱(com.sec.android.app.camera)이 없습니다. 갤럭시 단말이 맞는지 확인하고 `{PYZ} doctor`로 단말 정보를 점검합니다.",
    "UI_UNAVAILABLE": "UI 구조를 읽지 못했습니다. 다른 앱이 앞에 있거나 화면이 잠겼는지 단말 화면을 확인한 뒤 다시 실행합니다.",
    "IO_ERROR": "파일을 읽거나 쓰지 못했습니다. error.message의 경로가 존재하는지, 쓰기 권한과 디스크 공간이 있는지 확인합니다.",
    "RESULT_WRITE_FAILED": "result.json을 저장하지 못했습니다. 출력 폴더(--output-dir)의 쓰기 권한과 디스크 공간을 확인한 뒤 다시 실행합니다.",
    "VIDEO_COPY_INCOMPLETE": "영상을 PC로 복사하지 못했습니다. 연결이 안정적인지(무선이면 신호 상태) 확인한 뒤 다시 실행합니다.",
    "INTERRUPTED": "실행이 중단되었습니다. 단말에서 녹화가 남아 있지 않은지 확인한 뒤 다시 실행합니다.",
    "LOG_CAPTURE_FAILED": "logcat을 저장하지 못했습니다. 연결 상태를 확인한 뒤 다시 실행합니다. 이번 결과의 로그 근거는 없습니다.",
    # Automation: the profile or scenario no longer matches the app.
    "MODE_NOT_FOUND": _automation,
    "MODE_NOT_READY": _automation,
    "INVALID_UI_BOUNDS": _automation,
    "UNSUPPORTED_CAMERA_VERSION": _unsupported_version,
    "INVALID_PROFILE_FILE": f"프로필 JSON이 잘못되었습니다. error.message를 보고 고친 뒤 `{PYZ} profile validate <파일>`로 검사합니다. 내보낼 때는 PowerShell의 `>` 대신 `profile show <시나리오> --output <파일>`을 씁니다.",
    "INVALID_CHECKS_FILE": "검사 정의 파일이 잘못되었습니다. error.message를 보고 고칩니다. 형식은 checks.example.json과 docs/reference.md를 참고합니다.",
    "INVALID_GOLDEN": f"기준 화면 폴더가 잘못되었거나 다른 시나리오의 것입니다. `{PYZ} screens promote --run runs/<run-id> --output golden/<시나리오>/<버전>`으로 만들거나 경로를 확인합니다.",
    # Product: the recording behavior itself failed.
    "RECORD_START_UNVERIFIED": _record_start,
    "RECORDING_ENDED_EARLY": _product,
    "RECORD_STOP_UNVERIFIED": _product,
    "VIDEO_NOT_SAVED": _product,
    "AMBIGUOUS_VIDEO": _product,
    "CAMERA_LAUNCH_FAILED": _product,
    "CAMERA_PID_NOT_FOUND": _product,
    "CLEANUP_UNVERIFIED": _product,
    "MEDIA_NO_MOOV": _media,
    "MEDIA_MALFORMED": _media,
    "MEDIA_NO_VIDEO": _media,
    "MEDIA_NO_FRAMES": _media,
    "MEDIA_DURATION": _media,
    # suite: camera facing, photos and the registry.
    "SUITE_UI_UNAVAILABLE": "이 단계에서 카메라 화면을 읽지 못했습니다. 단말을 확인하고, 카메라가 특수 모드에 있으면 사진 모드로 바꾼 뒤 다시 실행합니다. 특정 모드에서 반복되면 camera-ui-diagnostics 스킬로 해당 화면을 확인합니다.",
    "SWITCH_NOT_FOUND": "화면에서 전면·후면 전환 버튼을 찾지 못했습니다. 단말에서 사진 모드의 전환 버튼을 확인합니다. 버튼은 보이는데 같은 오류가 나면 실패한 단계 폴더의 failure-ui.xml에서 버튼의 리소스 ID를 확인합니다.",
    "FACING_UNVERIFIED": "촬영 화면의 카메라 방향을 확인하지 못했습니다. 전환 버튼의 문구가 달라졌다면 failure-ui.xml에서 문구와 실제 방향을 확인하고 `suite.py`의 `SWITCH_LABEL_FACING`을 갱신합니다.",
    "FACING_SWITCH_UNVERIFIED": "전환 버튼을 누른 뒤 목표 카메라를 확인하지 못했습니다. 단말 화면에서 전환 여부를 확인합니다. 전환됐다면 버튼 문구를 확인하고, 전환되지 않았다면 앱 동작 문제로 보고합니다.",
    "FACING_MISMATCH": "촬영하려던 카메라와 화면이 가리키는 카메라가 달라졌습니다. 앱이 방향을 조용히 되돌렸을 수 있으므로 앱 동작 문제로 보고합니다. 이 촬영은 증거로 등록하지 않았습니다. 첨부할 파일: 해당 단계 폴더의 failure-ui.xml, failure.png와 report.md.",
    "MODE_NOT_SELECTED": "모드를 눌렀지만 다른 모드가 그대로 선택되어 있습니다. 해당 단계 폴더의 failure-ui.xml에서 모드 표시(content-desc 또는 instant_mode_title)가 프로필 문구와 같은지 확인합니다.",
    "IMAGE_NOT_SAVED": "셔터를 눌렀지만 새로운 사진 파일이 저장되지 않았습니다. 앱 동작 문제로 보고합니다. 첨부할 파일: 해당 단계 폴더의 failure.png와 report.md.",
    "AMBIGUOUS_IMAGE": "한 번의 촬영에 서로 다른 사진 여러 장이 생겨 어느 것이 이번 촬영인지 알 수 없습니다. 단말을 다른 사람이 쓰고 있지 않은지 확인하고 다시 실행합니다.",
    "REGISTRY_INVALID": "등록부 파일을 읽지 못했습니다. 파일 내용을 확인해 복구하거나, 새로 시작하려면 파일을 다른 이름으로 옮긴 뒤 다시 실행합니다.",
    "DEVICE_ID_UNAVAILABLE": "ADB에서 단말 식별 정보를 읽을 수 없습니다. 연결한 테스트 단말에서 adb shell getprop ro.serialno 출력을 확인합니다.",
    "REGISTRY_BUSY": "다른 camera-test가 등록부를 갱신하고 있습니다. 실행 폴더의 결과 파일을 확인하고, 앞선 실행이 끝난 뒤 같은 명령을 다시 실행합니다.",
    # Arguments, saved runs, golden sets, baselines.
    "INVALID_ARGUMENTS": "옵션 조합이 잘못되었습니다. `--help`로 사용법을 확인합니다. `--merge`와 `--force`는 함께 쓸 수 없습니다.",
    "INVALID_BASELINE": f"기준 report.json을 읽지 못했습니다. 이전 `verify-build`가 만든 runs/verify/<batch-id>/report.json 경로인지 확인합니다. 기준이 없으면 `{PYZ} verify-build {DEFAULT_SCENARIO}`부터 실행합니다.",
    "INVALID_RUN": "실행 폴더에서 result.json을 읽지 못했습니다. `run`이 만든 runs/<run-id> 폴더 경로인지 확인합니다.",
    "INVALID_SCREENS": f"화면 스냅샷 파일이 손상되었습니다. `{PYZ} run {DEFAULT_SCENARIO} --capture-screens`로 다시 실행해 새 스냅샷을 만듭니다.",
    "RUN_NOT_PASSED": f"통과한 실행만 기준 화면이 될 수 있습니다. `{PYZ} run {DEFAULT_SCENARIO} --capture-screens --cleanup-video`가 PASS가 되도록 먼저 고친 뒤 그 실행 폴더로 다시 승격합니다.",
    "SCREENS_NOT_CAPTURED": f"그 실행에는 저장된 화면이 없습니다. `{PYZ} run {DEFAULT_SCENARIO} --capture-screens`로 다시 실행하고 새 실행 폴더를 씁니다.",
    "GOLDEN_EXISTS": "같은 이름의 기준 화면이 이미 있습니다. 덮어쓰려면 `--force`를, 체크포인트를 더하려면 `--merge`를 붙이거나 다른 `--output` 폴더를 씁니다.",
    "GOLDEN_MISMATCH": "합치려는 실행이 기존 기준 화면과 시나리오나 앱 버전이 다릅니다. 같은 버전에서 다시 실행한 폴더를 쓰거나, 새 버전이면 다른 `--output` 폴더를 씁니다.",
    # HAL trials.
    "EMPTY_LOG": "로그에 threadtime 형식 줄이 없습니다. `hal collect`가 만든 logcat 파일인지, 시험 중 해당 태그 로그가 나왔는지 확인합니다.",
    "SAME_LOG": "OFF와 ON에 같은 파일을 넣었습니다. 각 상태에서 따로 `hal begin`과 `hal collect`를 한 로그를 `--off`, `--on`에 지정합니다.",
    "INVALID_PATTERN": "정규식이 잘못되었거나 `--expect-log`에 `--tag`가 빠졌습니다. `--tag <태그> --expect-log <정규식>`을 함께 지정합니다.",
    "INVALID_CONTROL": "`--control-tag`와 `--control-log`는 `--expect-log`와 함께 둘 다 지정해야 합니다.",
    "INVALID_LOG_TAG": "로그 태그에는 영문자, 숫자, 점, 밑줄, 하이픈만 쓸 수 있습니다. `--tag` 값을 고칩니다.",
    "INVALID_TRIAL": "`hal begin`이 출력한 trial.json 경로를 `--trial`에 지정합니다.",
    # Decisions and states that are not errors but still need a next step.
    "MODE_NOT_OBSERVED": "카메라 모드 목록에서 대상 모드(인물 동영상)를 보지 못했습니다. 단말 화면에서 직접 모드가 있는지 확인합니다. 있다면 앱 UI가 바뀐 것이므로 `diagnosis` 절차(camera-ui-diagnostics)로 프로필을 고칩니다.",
    "UI_SCAN_SKIPPED": f"화면 탐색 없이 실행했습니다. 모드까지 확인하려면 `--no-ui` 없이 `{PYZ} inspect`를 실행합니다.",
}

# Prefix fallbacks for codes without an entry of their own.
PREFIX_GUIDANCE = (
    ("PRECHECK_", "단말이 실행 가능한 상태가 아닙니다. error.message의 안내대로 단말을 고친 뒤 다시 실행합니다."),
    ("DEVICE_", f"단말 연결 문제입니다. `{PYZ} doctor`로 원인을 확인합니다."),
    ("ADB_", f"ADB 문제입니다. `{PYZ} doctor`로 원인을 확인합니다."),
    ("MEDIA_", _media),
)
FALLBACK = "알 수 없는 오류 코드입니다. error.message와 result.json을 확인하고, 단말 환경은 `python camera-test.pyz doctor`로 점검합니다."

# Codes that must always have an entry: tests compare this with failures.CLASS_BY_CODE.
OTHER_KNOWN_CODES = (
    "DEVICE_REQUIRED", "DEVICE_UNAVAILABLE", "DEVICE_BUSY", "DEVICE_CLOCK", "ADB_NOT_FOUND", "ADB_TIMEOUT", "ADB_ERROR",
    "PRECHECK_SCREEN_OFF", "PRECHECK_LOCKED", "PRECHECK_LOW_STORAGE", "PRECHECK_DEVICE_BUSY", "LOG_CAPTURE_FAILED",
    "INVALID_ARGUMENTS", "INVALID_BASELINE", "INVALID_RUN", "INVALID_SCREENS", "RUN_NOT_PASSED", "SCREENS_NOT_CAPTURED",
    "GOLDEN_EXISTS", "GOLDEN_MISMATCH", "EMPTY_LOG", "SAME_LOG", "INVALID_PATTERN", "INVALID_CONTROL", "INVALID_LOG_TAG",
    "INVALID_TRIAL", "MODE_NOT_OBSERVED", "UI_SCAN_SKIPPED",
)


def next_action(code, result=None):
    """Korean next step for an error code. `result` (run or inspect output) adds context such as the busy package."""
    entry = GUIDANCE.get(code)
    if entry is None:
        entry = next((value for prefix, value in PREFIX_GUIDANCE if code.startswith(prefix)), FALLBACK)
    text = entry(result) if callable(entry) else entry
    if (result or {}).get("recording_may_be_active"):
        text += " 단말에서 녹화가 아직 진행 중일 수 있으니 화면을 보고 녹화를 멈춘 뒤 다시 실행합니다."
    return text


def error_object(code, message, result=None):
    return {"code": code, "message": message, "next_action": next_action(code, result)}


def decision_next_action(decision, report, failing_result=None):
    """Next step for a verify-build decision. `report` is the report.json content (with `proposal`)."""
    scenario, version = report["scenario"], report["app_version"]
    proposal = report.get("proposal")
    first_failed = next((run for run in report["runs"] if run["status"] != "PASS" and run["result"]), None)
    if decision == "promote":
        if proposal:
            command = f"{PYZ} profile validate {proposal['profile']}"
            return (f"제안 프로필 `{proposal['profile']}`" + (f"과 기준 화면 `{proposal['golden']}`" if proposal["golden"] else "")
                    + f"을 검토합니다. 승인하면 `{command}`로 검사하고, `verified_versions`에 {version}을 추가한 뒤 "
                    "`python build.py camera-test.pyz`로 다시 빌드해 커밋합니다. 도구는 내장 프로필을 직접 고치지 않습니다.")
        return "통과했습니다. 제안 파일을 만들지 못했으니 report.json을 확인합니다."
    if decision == "baseline_ok":
        return "조치가 필요 없습니다. 이 report.json을 다음 빌드의 `--baseline`으로 보관합니다."
    if decision == "needs_profile_fix":
        where = f"`{first_failed['result']}`의 diagnosis부터 봅니다. " if first_failed else "report.json의 drift를 먼저 봅니다. "
        return (where + f"`{PYZ} profile show {scenario} --output galaxy-camera.json`로 프로필을 내보내 고친 뒤 "
                f"`{PYZ} verify-build {scenario} --profile-file galaxy-camera.json`로 다시 검증합니다.")
    if decision == "product_failure":
        return "녹화·저장 동작이 실제로 실패했습니다. 앱 결함으로 보고합니다. 실패한 실행의 logcat.txt, failure.png, 영상을 첨부합니다."
    if decision == "flaky":
        return (f"통과와 실패가 섞였고 원인을 분류하지 못했습니다. report.json의 error_codes와 실행별 result.json을 확인하고, "
                f"`{PYZ} verify-build {scenario} --repeat 10`으로 횟수를 늘려 다시 확인합니다.")
    if decision == "environment_blocked":
        code = next((run["error_code"] for run in report["runs"] if run["failure_class"] == "environment" and run["error_code"]), None)
        cause = next_action(code, failing_result) if code else "단말 화면, 잠금, 다른 앱 사용 여부, 저장 공간을 확인합니다."
        return f"{cause} 고친 뒤 같은 명령을 다시 실행합니다."
    return "report.json을 확인합니다."
