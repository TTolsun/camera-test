"""verify-build: repeat a scenario on the installed app build and hand a human a decision.

Every iteration is a normal `run` (through the injected `run_once`), so ADB is reached only
there. Nothing here edits code or the built-in profile: a `promote` decision writes a
proposal folder that a person reviews and applies.
"""

from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import statistics
import tempfile
from types import SimpleNamespace
from uuid import uuid4

from . import screens
from .adb import Device, RunError, select_device
from .profiles import parse_profile
from .scenarios import SCENARIOS, read_profile_file, resolve_profile


REPORT_SCHEMA_VERSION = 1
STATE_SCHEMA_VERSION = 1
METRICS = ("record_start_latency_ms", "record_stop_latency_ms", "video_ready_ms")
REGRESSION_RATIO = 0.25  # A median must grow by more than 25% ...
REGRESSION_MIN_MS = 100  # ... and by more than this many ms to count as a regression.
MAX_CONSECUTIVE_AUTOMATION = 2
DECISION_EXIT_CODES = {"promote": 0, "baseline_ok": 0, "needs_profile_fix": 1, "product_failure": 1,
                       "flaky": 1, "environment_blocked": 3}
NEXT_STEPS_BY_DECISION = {
    "promote": "프로필과 골든 제안을 검토하고 승인합니다.",
    "baseline_ok": "조치가 필요 없습니다. 지표는 이후 비교용 기준(--baseline)으로 씁니다.",
    "needs_profile_fix": "앱 프로필(선택자·문구·로그 패턴)을 고쳐야 합니다.",
    "product_failure": "녹화·저장 동작이 실제로 실패했습니다. 앱 동작 문제로 조사합니다.",
    "flaky": "통과와 실패가 섞였고 원인을 분류하지 못했습니다. 로그를 보고 횟수를 늘려 다시 확인합니다.",
    "environment_blocked": "단말 환경 문제로 중단했습니다. 단말 상태를 고친 뒤 다시 실행합니다.",
}


def now_utc():
    return datetime.now(timezone.utc)


def read_json_file(path, code, what):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise RunError(code, f"{what} could not be read: {exc}") from exc
    if not isinstance(data, dict):
        raise RunError(code, f"{what} must be a JSON object.")
    return data


def write_json_atomic(path, data):
    """Write through a temp file in the same folder and replace, so readers never see a half-written file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            file.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def read_state(path):
    """Saved batches as {"devices": {hardware serial: {scenario: entry}}}. A missing or damaged file means none."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data.get("devices"), dict) else {"devices": {}}
    except (OSError, ValueError, AttributeError):
        return {"devices": {}}


def state_entry(state, hardware, scenario):
    entry = state["devices"].get(hardware, {}).get(scenario)
    return entry if isinstance(entry, dict) else None


def update_state(path, hardware, scenario, entry):
    state = read_state(path)
    state["schema_version"] = STATE_SCHEMA_VERSION
    state["devices"].setdefault(hardware, {})[scenario] = entry
    write_json_atomic(path, state)


def already_handled(entry, version):
    """An environment-blocked batch verified nothing about the build, so it is retried."""
    return bool(entry) and entry.get("app_version") == version and entry.get("decision") != "environment_blocked"


def iteration_args(args, serial, batch):
    return SimpleNamespace(
        scenario=args.scenario, device=serial, duration=args.duration, output_dir=batch,
        pull_video=False, verify_media=True, check=None, checks_file=None, profile_file=args.profile_file,
        capture_screens=True, golden=args.golden, cleanup_video=True, allow_unverified_version=True,
        require_idle=True,
    )


def passed(result, exit_code):
    return exit_code == 0 and result.get("scenario_status") == "PASS"


def describe_run(result, exit_code):
    ok = passed(result, exit_code)
    code = result.get("error", {}).get("code") or ("LOG_CAPTURE_FAILED" if result.get("log_capture_error") else None)
    return {
        "run_id": result["run_id"],
        "status": "PASS" if ok else "FAIL",
        "failure_class": None if ok else result.get("failure_class", "unknown"),
        "error_code": None if ok else code,
        "result": result.get("artifacts", {}).get("result"),
    }


def number(value):
    return int(value) if value == int(value) else round(value, 1)


def metric_stats(results):
    """min/median/max per metric over the passing runs that recorded it."""
    stats = {}
    for name in METRICS:
        values = [result["metrics"][name] for result in results if name in result.get("metrics", {})]
        if values:
            stats[name] = {"min": min(values), "median": number(statistics.median(values)),
                           "max": max(values), "samples": len(values)}
    return stats


def media_summary(results):
    """Distinct codec, resolution, fps (1 decimal) and audio presence across runs that produced an MP4."""
    infos = [result["media"] for result in results if "tracks" in result.get("media", {})]
    if not infos:
        return {}
    videos = [track for info in infos for track in info["tracks"] if track["type"] == "video"]
    durations = [info["duration_s"] for info in infos]
    return {
        "files": len(infos),
        "video_codecs": sorted({track["codec"] for track in videos if track["codec"]}),
        "resolutions": sorted({f"{track['width']}x{track['height']}" for track in videos if track["width"]}),
        "fps": sorted({round(track["fps"], 1) for track in videos if track["fps"]}),
        "audio": sorted({any(track["type"] == "audio" for track in info["tracks"]) for info in infos}),
        "duration_s": {"min": min(durations), "max": max(durations)},
    }


def drift_summary(results):
    """Worst severity and the union of selector and label findings across all checkpoints of all runs."""
    worst, missing, changed = "none", [], []
    for result in results:
        drift = result.get("drift")
        if not drift:
            continue
        if screens.SEVERITY_ORDER.index(drift["severity"]) > screens.SEVERITY_ORDER.index(worst):
            worst = drift["severity"]
        for checkpoint in drift["checkpoints"].values():
            for item in checkpoint.get("profile_selectors_missing", []):
                if item not in missing:
                    missing.append(item)
            for item in checkpoint.get("changed_labels", []):
                if item not in changed:
                    changed.append(item)
    return {"worst": worst, "profile_selectors_missing": missing, "changed_labels": changed}


def decide(verified, records, worst_drift, stopped):
    """The decision a human acts on. See the README table for the rules."""
    failed = [record for record in records if record["status"] != "PASS"]
    classes = Counter(record["failure_class"] for record in failed)
    if stopped == "environment":
        return "environment_blocked"
    if classes["automation"] or worst_drift == "breaking":
        return "needs_profile_fix"
    if classes["product"]:
        return "product_failure"
    if not failed:
        return "baseline_ok" if verified else "promote"
    return "flaky"


def compare_baseline(path, metrics):
    """Compare metric medians with an earlier report. A regression grew by more than 25% and more than 100 ms."""
    previous = read_json_file(path, "INVALID_BASELINE", "Baseline report")
    old_metrics = previous.get("metrics")
    if not isinstance(old_metrics, dict):
        raise RunError("INVALID_BASELINE", "Baseline report has no metrics.")
    compared, regressions = {}, []
    for name, now in metrics.items():
        try:
            before = old_metrics[name]["median"]
            growth = now["median"] - before
        except (KeyError, TypeError):
            continue
        regression = growth > REGRESSION_MIN_MS and now["median"] > before * (1 + REGRESSION_RATIO)
        compared[name] = {"previous_median": before, "median": now["median"], "change_ms": number(growth),
                          "regression": regression}
        if regression:
            regressions.append(name)
    return {"report": str(path), "app_version": previous.get("app_version"), "metrics": compared,
            "regressions": regressions}


def write_proposal(batch, profile, version, records, scenario):
    """Write proposal/profile.json (validated with the profile loader) and a golden set merged from all passing runs."""
    folder = batch / "proposal"
    data = profile.to_dict()
    if version not in data["verified_versions"]:
        data["verified_versions"].append(version)
    parse_profile(data, "proposal", template=SCENARIOS[scenario].profile)
    write_json_atomic(folder / "profile.json", data)
    read_profile_file(folder / "profile.json")
    proposal = {"profile": str(folder / "profile.json"), "golden": None, "golden_from": None}
    # The golden is the union of checkpoints over all passing runs: the first run holding a checkpoint wins,
    # so runs that start in different modes still cover both start states.
    for record in records:
        run_dir = Path(record["result"]).parent if record["result"] else None
        if record["status"] == "PASS" and run_dir and (run_dir / "screens").is_dir():
            try:
                screens.promote(run_dir, folder / "golden", merge=proposal["golden"] is not None)
            except RunError:
                continue
            proposal["golden"] = str(folder / "golden")
            proposal["golden_from"] = proposal["golden_from"] or record["run_id"]
    return proposal


def build_report(context, results, records, stopped, finished):
    """Aggregate the iterations into report.json content (without decision-dependent files)."""
    passed_results = [result for result, record in zip(results, records) if record["status"] == "PASS"]
    run_count = len(records)
    pass_count = len(passed_results)
    failed = [record for record in records if record["status"] != "PASS"]
    drift = drift_summary(results)
    warnings = sorted({code for result in results for code in result.get("warnings", [])})
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "tool_version": context["tool_version"],
        "scenario": context["scenario"],
        "batch_id": context["batch"].name,
        "finished": finished.isoformat(timespec="seconds"),
        "device": context["device"],
        "app_version": context["version"],
        "profile": {"source": context["profile"].source, "verified": context["verified"]},
        "verified": context["verified"],
        "iterations": {"requested": context["repeat"], "run": run_count, "passed": pass_count,
                       "pass_rate": round(pass_count / run_count, 3) if run_count else 0.0,
                       "intermittent": bool(pass_count and failed)},
        "stopped_early": stopped,
        "failure_classes": dict(Counter(record["failure_class"] for record in failed)),
        "error_codes": dict(Counter(record["error_code"] or "UNKNOWN" for record in failed)),
        "ui_retries": sum(result.get("ui_retries", 0) for result in results),
        "metrics": metric_stats(passed_results),
        "media": media_summary(results),
        "drift": drift,
        "warnings": warnings,
        "runs": records,
    }


def render_markdown(report, batch):
    """Short Korean report for a human: decision, numbers, then exact next commands."""
    decision, it = report["decision"], report["iterations"]
    scenario, version = report["scenario"], report["app_version"]
    pyz = "python camera-test.pyz"
    lines = [
        f"# 카메라 앱 빌드 검증: {version}", "",
        f"**결정: `{decision}`** — {NEXT_STEPS_BY_DECISION[decision]}", "",
        "## 결과",
        f"- 시나리오 {scenario}, 기기 {report['device'].get('model') or report['device']['serial']}, "
        f"프로필 {report['profile']['source']}(검증 목록 포함: {'예' if report['verified'] else '아니요'})",
        f"- 반복 {it['run']}/{it['requested']}회, 통과 {it['passed']}회(통과율 {it['pass_rate']:.0%})"
        + (f", 조기 중단: {report['stopped_early']}" if report["stopped_early"] else ""),
    ]
    if report["failure_classes"]:
        lines.append(f"- 실패 분류 {report['failure_classes']}, 오류 코드 {report['error_codes']}")
        if it["intermittent"]:
            lines.append("- 통과와 실패가 섞였습니다(간헐적 실패).")
    drift = report["drift"]
    lines.append(f"- 화면 변화(drift) 최악 등급 `{drift['worst']}`, 사라진 프로필 선택자 {len(drift['profile_selectors_missing'])}개, "
                 f"바뀐 문구 {len(drift['changed_labels'])}개")
    for name, stats in report["metrics"].items():
        lines.append(f"- {name}: 최소 {stats['min']} / 중앙값 {stats['median']} / 최대 {stats['max']} ms")
    if report["media"]:
        media = report["media"]
        lines.append(f"- 영상: 코덱 {media['video_codecs']}, 해상도 {media['resolutions']}, fps {media['fps']}, "
                     f"오디오 {media['audio']}, 길이 {media['duration_s']['min']}~{media['duration_s']['max']} s")
    if report.get("baseline", {}).get("regressions"):
        lines.append(f"- 기준 대비 지표 악화(`METRIC_REGRESSION`): {', '.join(report['baseline']['regressions'])}")
    lines += ["", "## 다음 명령", "", "```powershell"]
    proposal = report.get("proposal")
    if decision == "promote" and proposal:
        lines += [
            f"{pyz} profile validate {proposal['profile']}",
            f"{pyz} run {scenario} --profile-file {proposal['profile']}"
            + (f" --golden {proposal['golden']}" if proposal["golden"] else ""),
        ]
        lines += ["```", "",
                  "승인하면 다음 순서로 팀 전체에 반영합니다.",
                  f"1. `{proposal['profile']}`의 `verified_versions` 변화를 `camera_runner/scenarios/galaxy_portrait_video.py`의 `PROFILE`에 옮깁니다.",
                  "2. `python build.py camera-test.pyz`로 다시 빌드하고 커밋합니다."
                  + (f" 골든은 `{proposal['golden']}`를 `golden/{version}`으로 복사해 보관합니다." if proposal["golden"] else "")]
    elif decision == "needs_profile_fix":
        first = next((run for run in report["runs"] if run["status"] != "PASS"), None)
        if first and first["result"]:
            lines.append(f"Get-Content {first['result']}   # diagnosis.phase, diagnosis.ui.absent, diagnosis.record_state_log")
        lines += [f"{pyz} profile show {scenario} --output galaxy-camera.json",
                  f"{pyz} profile validate galaxy-camera.json",
                  f"{pyz} verify-build {scenario} --profile-file galaxy-camera.json --golden <golden 폴더>",
                  "```", "", "`diagnosis`와 `drift`의 후보 ID는 이름이 비슷할 뿐이므로 `failure-ui.xml`에서 실제 컨트롤인지 확인한 뒤 반영합니다."]
    elif decision == "baseline_ok":
        lines += [f"{pyz} verify-build {scenario} --baseline {batch / 'report.json'}   # 다음 빌드 비교용", "```"]
    else:
        failing = [run["result"] for run in report["runs"] if run["status"] != "PASS" and run["result"]]
        lines += [f"Get-Content {path}" for path in failing[:3]] or ["# 실행 결과 파일이 없습니다."]
        lines += ["```", "", "단말 상태(잠금, 화면 꺼짐, 다른 앱 사용 중, 저장 공간)를 확인한 뒤 같은 명령을 다시 실행합니다."
                  if decision == "environment_blocked" else "`failure.png`와 `logcat.txt`를 확인하고 앱 동작 문제로 보고합니다."]
    lines += ["", f"- 전체 데이터: `{batch / 'report.json'}`", "- 이 도구는 코드와 내장 프로필을 수정하지 않습니다. 제안은 사람이 승인한 뒤 적용합니다.", ""]
    return "\n".join(lines)


def verify_build(args, run_once, tool_version):
    """Run the batch. Returns (one-line summary, exit code). `run_once(args) -> (result, exit_code)`."""
    scenario = args.scenario
    profile = resolve_profile(scenario, args.profile_file)
    if args.golden:
        screens.load_golden(args.golden, scenario)
    baseline_path = args.baseline
    if baseline_path:
        read_json_file(baseline_path, "INVALID_BASELINE", "Baseline report")  # Fail before touching the phone.
    serial = select_device(args.device)
    device = Device(serial, uuid4().hex[:12])
    hardware = device.shell("getprop", "ro.serialno") or serial
    version = device.package_version(profile.package)
    if version is None:
        raise RunError("CAMERA_APP_NOT_FOUND", f"{SCENARIOS[scenario].app_name} ({profile.package}) is not installed.")

    if args.if_changed:
        entry = state_entry(read_state(args.state_file), hardware, scenario)
        if already_handled(entry, version):
            return {"status": "unchanged", "decision": "unchanged", "app_version": version,
                    "previous_decision": entry.get("decision"), "report": entry.get("report")}, 0

    batch = args.output_dir.resolve() / f"{now_utc():%Y%m%d-%H%M%S}-{uuid4().hex[:6]}"
    batch.mkdir(parents=True)
    results, records, stopped, automation_streak = [], [], None, 0
    for _ in range(args.repeat):
        result, exit_code = run_once(iteration_args(args, serial, batch))
        record = describe_run(result, exit_code)
        results.append(result)
        records.append(record)
        if record["failure_class"] == "environment":
            stopped = "environment"
            break
        automation_streak = automation_streak + 1 if record["failure_class"] == "automation" else 0
        if automation_streak >= MAX_CONSECUTIVE_AUTOMATION and len(records) < args.repeat:
            stopped = "automation"
            break

    info = next((result["device"] for result in results if "device" in result), {})
    context = {"tool_version": tool_version, "scenario": scenario, "batch": batch, "version": version, "repeat": args.repeat,
               "profile": profile, "verified": profile.verifies(version),
               "device": {"serial": serial, "hardware_serial": hardware, "model": info.get("model")}}
    finished = now_utc()
    report = build_report(context, results, records, stopped, finished)
    report["decision"] = decide(context["verified"], records, report["drift"]["worst"], stopped)
    if baseline_path:
        report["baseline"] = compare_baseline(baseline_path, report["metrics"])
        if report["baseline"]["regressions"]:
            report["warnings"] = sorted({*report["warnings"], "METRIC_REGRESSION"})
    report["proposal"] = write_proposal(batch, profile, version, records, scenario) if report["decision"] == "promote" else None
    report["artifacts"] = {"report": str(batch / "report.json"), "markdown": str(batch / "report.md")}
    write_json_atomic(batch / "report.json", report)
    (batch / "report.md").write_text(render_markdown(report, batch), encoding="utf-8")
    if args.state_file:
        update_state(args.state_file, hardware, scenario, {
            "app_version": version, "decision": report["decision"], "report": report["artifacts"]["report"],
            "finished": report["finished"]})
    summary = {"status": "completed", "decision": report["decision"], "pass_rate": report["iterations"]["pass_rate"],
               "app_version": version, "report": report["artifacts"]["report"]}
    return summary, DECISION_EXIT_CODES[report["decision"]]
