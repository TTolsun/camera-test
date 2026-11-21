"""doctor: read-only environment check that says what to do next.

It only reads: `adb version`, `adb devices -l`, `adb mdns services`, getprop, dumpsys and df.
It never taps, launches an app, takes a screenshot or records, so it is safe on any phone.
"""

import os
import re
import shutil
import sys

from . import adb as adb_module
from . import preflight
from .adb import Device, RunError
from .guidance import DEFAULT_SCENARIO, PYZ, next_action
from .scenarios import SCENARIOS

MIN_PYTHON = (3, 10)
KNOWN_STATES = ("device", "unauthorized", "offline")
MDNS_LINE = re.compile(r"^\s*(\S+)\s+_adb-tls-(connect|pairing)\._tcp\.?\s+(\S+)", re.M)
FIRST_RUN = f"{PYZ} run {DEFAULT_SCENARIO} --duration 5 --verify-media --cleanup-video"
MARKERS = {"ok": "[OK]  ", "warn": "[WARN]", "fail": "[FAIL]", "skip": "[SKIP]"}


def check(name, status, detail, action=None):
    item = {"name": name, "status": status, "detail": detail}
    if action and status in ("warn", "fail"):
        item["next_action"] = action
    return item


def skipped(name, because):
    return check(name, "skip", f"앞 단계({because})가 해결되지 않아 건너뜁니다.")


def check_python():
    version = ".".join(map(str, sys.version_info[:3]))
    if sys.version_info >= MIN_PYTHON:
        return check("python", "ok", f"Python {version}")
    return check("python", "fail", f"Python {version}",
                 "Python 3.10 이상을 설치한 뒤 `python --version`으로 확인합니다.")


def adb_origin(path):
    if os.environ.get("CAMERA_TEST_ADB") == path:
        return "환경 변수 CAMERA_TEST_ADB"
    if shutil.which("adb") == path:
        return "PATH"
    return "Android SDK 폴더"


def check_adb():
    try:
        path = adb_module.adb_path()
        first_line = (adb_module.adb("version", timeout=8).splitlines() or ["(버전 출력 없음)"])[0]
    except RunError as exc:
        return check("adb", "fail", f"{exc.code}: {exc}", next_action(exc.code))
    return check("adb", "ok", f"{first_line} ({adb_origin(path)}: {path})")


def parse_devices(text):
    """[(serial, state, model)] from `adb devices -l`; states outside the known three are kept as 'other'."""
    entries = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 2 or line.startswith(("List of devices", "*")):
            continue
        model = re.search(r"\bmodel:(\S+)", line)
        entries.append((fields[0], fields[1] if fields[1] in KNOWN_STATES else "other", model[1] if model else None))
    return entries


def discover_wireless():
    """(connect endpoints, pairing endpoints) advertised on the network; empty when mdns is unavailable."""
    try:
        text = adb_module.adb("mdns", "services", timeout=8)
    except RunError:
        return [], []
    found = {"connect": [], "pairing": []}
    for _, kind, endpoint in MDNS_LINE.findall(text):
        if endpoint not in found[kind]:
            found[kind].append(endpoint)
    return found["connect"], found["pairing"]


def no_device_action(entries):
    states = {state for _, state, _ in entries}
    if "unauthorized" in states:
        serial = next(serial for serial, state, _ in entries if state == "unauthorized")
        return (f"단말 {serial}이 승인되지 않았습니다. 단말 화면의 'USB 디버깅을 허용하시겠습니까?' 팝업에서 허용을 누릅니다"
                "(이 컴퓨터에서 항상 허용을 선택). 팝업이 없으면 케이블을 다시 꽂거나 `adb kill-server` 후 다시 확인합니다.")
    if "offline" in states:
        serial = next(serial for serial, state, _ in entries if state == "offline")
        return (f"단말 {serial}이 offline입니다. `adb disconnect {serial}` 후 다시 연결합니다. USB면 케이블을 다시 꽂고, "
                "무선이면 아래 `adb connect`를 다시 합니다. 계속 offline이면 `adb kill-server`를 실행합니다.")
    connect, pairing = discover_wireless()
    if connect:
        commands = " 또는 ".join(f"`adb connect {endpoint}`" for endpoint in connect)
        return f"네트워크에서 무선 디버깅 단말을 찾았습니다. {commands}를 실행한 뒤 doctor를 다시 실행합니다."
    if pairing:
        commands = " 또는 ".join(f"`adb pair {endpoint}`" for endpoint in pairing)
        return (f"무선 디버깅 페어링 대기 중인 단말이 보입니다. {commands}를 실행하고, 단말의 페어링 화면에 표시된 6자리 코드를 입력합니다. "
                "페어링 뒤에는 무선 디버깅 첫 화면의 'IP 주소 및 포트'(페어링 포트와 다릅니다)로 `adb connect <IP>:<포트>`를 실행합니다.")
    return ("연결된 단말이 없습니다. 개발자 옵션(설정 > 휴대전화 정보 > 소프트웨어 정보에서 빌드번호 7번 누르기)에서 USB 디버깅을 켜고 케이블을 연결하거나, "
            "같은 Wi-Fi에서 무선 디버깅을 켭니다. 무선은 '페어링 코드로 기기 페어링'의 IP:포트로 `adb pair <IP>:<포트>`를 한 뒤 "
            "`adb connect <IP>:<포트>`를 합니다.")


def check_devices(requested):
    """Returns (check, serial or None)."""
    try:
        entries = parse_devices(adb_module.adb("devices", "-l"))
    except RunError as exc:
        return check("devices", "fail", f"{exc.code}: {exc}", next_action(exc.code)), None
    listing = ", ".join(f"{serial}({state})" for serial, state, _ in entries) or "없음"
    ready = [entry for entry in entries if entry[1] == "device" and not entry[0].startswith("emulator-")]
    if requested:
        state = next((state for serial, state, _ in entries if serial == requested), "목록에 없음")
        if state != "device":
            action = no_device_action([entry for entry in entries if entry[0] == requested]) if state != "목록에 없음" \
                else no_device_action([])
            return check("devices", "fail", f"--device {requested}: {state}. 목록: {listing}", action), None
        return check("devices", "ok", f"--device {requested} 사용"), requested
    if not ready:
        return check("devices", "fail", f"준비된 단말이 없습니다. 목록: {listing}", no_device_action(entries)), None
    try:
        serial = adb_module.select_device(None)
    except RunError as exc:
        if exc.code != "DEVICE_REQUIRED":
            return check("devices", "fail", f"{exc.code}: {exc}", next_action(exc.code)), None
        options = "; ".join(f"`--device {serial}`" + (f"({model})" if model else "") for serial, _, model in ready)
        return check("devices", "warn", f"서로 다른 단말이 {len(ready)}대 연결되어 있습니다. 목록: {listing}",
                     f"어느 단말을 쓸지 지정합니다: {options}. 예: `{PYZ} doctor --device {ready[0][0]}`"), None
    detail = f"단말 {serial} 준비됨"
    if len(ready) > 1:
        detail += f"(같은 단말이 {len(ready)}개 연결 형태로 나열됩니다. 도구가 {serial} 형태를 자동으로 고릅니다)"
    return check("devices", "ok", detail), serial


def check_device(device):
    try:
        info = device.metadata()
    except RunError as exc:
        return check("device", "fail", f"{exc.code}: {exc}", next_action(exc.code)), None
    detail = f"{info['model']}, Android {info['android']}, SoC {info['soc_vendor']}"
    if info["soc_vendor"] == "unknown":
        return check("device", "warn", detail,
                     "SoC 제조사를 알 수 없습니다. 앱 로그 검사는 쓸 수 있지만 HAL 검사(source hal)는 적용되지 않습니다."), info
    return check("device", "ok", detail), info


def check_camera_app(device):
    profile = SCENARIOS[DEFAULT_SCENARIO].profile
    try:
        version = device.package_version(profile.package)
    except RunError as exc:
        return check("camera_app", "fail", f"{exc.code}: {exc}", next_action(exc.code))
    if version is None:
        return check("camera_app", "fail", f"{profile.package} 없음", next_action("CAMERA_APP_NOT_FOUND"))
    if profile.verifies(version):
        return check("camera_app", "ok", f"{version} (검증된 버전)")
    context = {"scenario": DEFAULT_SCENARIO, "device": {"camera_version": version},
               "profile": profile.summary(version)}
    return check("camera_app", "warn", f"{version} (검증 목록: {', '.join(profile.verified_versions)})",
                 next_action("UNSUPPORTED_CAMERA_VERSION", context))


def check_phone_state(device):
    profile = SCENARIOS[DEFAULT_SCENARIO].profile
    try:
        measured = preflight.measure(device, profile.package)
    except RunError as exc:
        return check("phone_state", "fail", f"{exc.code}: {exc}", next_action(exc.code))
    free = measured["free_bytes"]
    foreground = measured["foreground"]
    detail = (f"화면 {measured['screen']}, 잠금 {measured['lock']}, 여유 공간 "
              + (f"{free // 1048576}MB" if free is not None else "unknown")
              + f", 앞 화면 {foreground['package'] or 'unknown'}")
    # Doctor does not require an idle phone, so the foreground is judged separately below.
    failed = preflight.verdict({key: value for key, value in measured.items() if key != "foreground"})
    if failed:
        return check("phone_state", "fail", detail, next_action(failed[0]))
    if foreground["state"] == "busy":
        return check("phone_state", "warn", detail, next_action("PRECHECK_DEVICE_BUSY", {"preflight": {"foreground": foreground}}))
    return check("phone_state", "ok", detail)


def run_doctor(args):
    """Returns (result, exit_code); exit 0 only when ready. `result` is {"checks": [...], "ready": bool, "next_action": str}."""
    checks = [check_python()]
    adb_check = check_adb()
    checks.append(adb_check)
    serial = device = app = None
    if adb_check["status"] == "fail":
        checks += [skipped(name, "adb") for name in ("devices", "device", "camera_app", "phone_state")]
    else:
        devices_check, serial = check_devices(args.device)
        checks.append(devices_check)
        if serial is None:
            checks += [skipped(name, "devices") for name in ("device", "camera_app", "phone_state")]
        else:
            device = Device(serial, "doctor")
            device_check, _ = check_device(device)
            checks.append(device_check)
            if device_check["status"] == "fail":
                checks += [skipped(name, "device") for name in ("camera_app", "phone_state")]
            else:
                app = check_camera_app(device)
                checks += [app, check_phone_state(device)]
    by_name = {item["name"]: item for item in checks}
    ready = all(item["status"] in ("ok", "warn") for item in checks) and by_name["camera_app"]["status"] == "ok"
    result = {"checks": checks, "ready": ready, "next_action": final_action(checks, ready, serial, args.device)}
    return result, 0 if ready else 1


def final_action(checks, ready, serial, requested):
    if ready:
        suffix = f" --device {serial}" if requested else ""
        busy = next((item for item in checks if item["name"] == "phone_state" and item["status"] == "warn"), None)
        prefix = "단말 앞 화면에 다른 앱이 있습니다. 쓰는 사람이 없는지 확인한 뒤 " if busy else ""
        return f"{prefix}`{FIRST_RUN}{suffix}`를 실행합니다."
    # A failure comes first; otherwise the warnings that keep `run` from working (several phones, unverified app).
    blocking = next(item for item in checks if item["status"] == "fail" or
                    (item["status"] == "warn" and item["name"] in ("devices", "camera_app")))
    return blocking["next_action"]


def render_text(result):
    lines = ["camera-test doctor (읽기 전용: 화면 조작, 앱 실행, 녹화를 하지 않습니다)", ""]
    for item in result["checks"]:
        lines.append(f"{MARKERS[item['status']]} {item['name']}: {item['detail']}")
        if "next_action" in item:
            lines.append(f"       -> {item['next_action']}")
    lines.append("")
    if result["ready"]:
        lines.append("결과: run을 실행할 준비가 되었습니다.")
    else:
        lines.append("결과: 아직 run이 동작하지 않을 것으로 예상합니다.")
    # A failing check already printed its guidance under "->"; repeating it adds nothing.
    if result["next_action"] not in {item.get("next_action") for item in result["checks"]}:
        lines.append(f"다음: {result['next_action']}")
    return "\n".join(lines)
